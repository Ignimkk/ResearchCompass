"""실행(Run)과 결과 수치 기록.

결과는 '실행 × 지표' 표로 붙여넣습니다. 첫 줄은 머리글이고, 첫 칸이 label(또는 실행)이면 실행 이름 열입니다.

    label   wrong_count   avg_latency_s
    seed=1  19            1.8
    seed=2  18            1.9

규칙
- 같은 이름의 실행이 있으면 그 실행의 값을 고치고, 없으면 새 실행(R1, R2, …)을 만듭니다.
- 기준선의 근거(스냅샷)인 실행의 값은 바꿀 수 없습니다. 기준선 수치가 몰래 바뀌지 않게 하기 위해서입니다.
- 기준선 측정 실험(E0)에 새 실행을 기록하면, B0이 현재 기준선일 때 B0 스냅샷을 넓힙니다(snapshot_update).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.ids import next_short_id
from rcompass.models import (
    Aggregate,
    Baseline,
    BaselineChange,
    BaselineChangeKind,
    BaselineRun,
    Direction,
    Experiment,
    ExperimentKind,
    ExperimentStatus,
    MetricResult,
    Project,
    Provenance,
    Run,
    RunExclusion,
    RunStatus,
    SuccessCriterion,
    utcnow,
)
from rcompass.services import ValidationError
from rcompass.services.aggregate import Aggregated, aggregate, format_aggregated, format_plain
from rcompass.services.projects import MAX_RUNS, METRIC_KEY_PATTERN, get_criteria

LABEL_HEADERS = {"label", "run", "실행", "라벨", "이름"}


@dataclass
class ParsedRow:
    line: int
    label: str | None  # None이면 자동 이름
    values: dict[str, float]


@dataclass
class ParsedTable:
    metrics: list[str]
    rows: list[ParsedRow]


def _split(line: str, delimiter: str | None) -> list[str]:
    cells = line.split(delimiter) if delimiter else re.split(r"\s+", line.strip())
    return [cell.strip() for cell in cells]


def parse_results_table(text: str) -> ParsedTable:
    """붙여넣은 표를 해석합니다. 탭, 쉼표, 공백 구분을 받습니다. 오류는 줄 번호와 함께 알려줍니다."""
    lines = [(number, line) for number, line in enumerate((text or "").splitlines(), start=1) if line.strip()]
    if len(lines) < 2:
        raise ValidationError({"results": "첫 줄에 지표 이름(머리글), 다음 줄부터 실행별 값을 적어 주세요."})
    header_line = lines[0][1]
    delimiter = "\t" if "\t" in header_line else ("," if "," in header_line else None)
    header = _split(header_line, delimiter)
    has_label = header[0].lower() in LABEL_HEADERS
    metrics = header[1:] if has_label else header

    problems: list[str] = []
    if not metrics:
        problems.append(f"{lines[0][0]}번째 줄: 지표 이름이 없습니다.")
    for key in metrics:
        if not METRIC_KEY_PATTERN.match(key):
            problems.append(f"{lines[0][0]}번째 줄: '{key}'는 지표 이름으로 쓸 수 없습니다(영문으로 시작, 영문·숫자·_ . -).")
    if len(set(metrics)) != len(metrics):
        problems.append(f"{lines[0][0]}번째 줄: 같은 지표 이름이 두 번 있습니다.")
    if len(lines) - 1 > MAX_RUNS:
        problems.append(f"실행은 한 번에 {MAX_RUNS}개까지 기록할 수 있습니다.")

    rows: list[ParsedRow] = []
    seen_labels: set[str] = set()
    for number, line in lines[1:]:
        cells = _split(line, delimiter)
        if len(cells) != len(header):
            problems.append(f"{number}번째 줄: 칸이 {len(cells)}개입니다. 머리글처럼 {len(header)}칸이어야 합니다.")
            continue
        label = cells[0] if has_label else None
        if has_label and not label:
            problems.append(f"{number}번째 줄: 실행 이름이 비어 있습니다.")
        if label and label in seen_labels:
            problems.append(f"{number}번째 줄: 실행 이름 '{label}'이(가) 표에 두 번 있습니다.")
        if label:
            seen_labels.add(label)
        values: dict[str, float] = {}
        for key, cell in zip(metrics, cells[1:] if has_label else cells):
            if not cell or cell == "-":
                continue  # 이 실행에서 재지 않은 지표
            try:
                value = float(cell)
            except ValueError:
                problems.append(f"{number}번째 줄: '{cell}'은(는) 숫자가 아닙니다.")
                continue
            if not math.isfinite(value):
                problems.append(f"{number}번째 줄: '{cell}'은(는) 기록할 수 없는 값입니다.")
                continue
            values[key] = value
        rows.append(ParsedRow(number, label, values))
    if problems:
        raise ValidationError({"results": "\n".join(problems)})
    return ParsedTable(metrics, rows)


# ---------------------------------------------------------------- 기록


def _runs(session: Session, experiment: Experiment) -> list[Run]:
    return list(session.scalars(select(Run).where(Run.experiment_id == experiment.id).order_by(Run.id)))


def _snapshot_baselines(session: Session, run_ids: list[int]) -> dict[int, list[str]]:
    """실행 id → 그 실행을 근거로 쓰는 기준선 짧은 ID 목록."""
    if not run_ids:
        return {}
    rows = session.execute(
        select(BaselineRun.run_id, Baseline.short_id)
        .join(Baseline, Baseline.id == BaselineRun.baseline_id)
        .where(BaselineRun.run_id.in_(run_ids))
        .order_by(Baseline.id)
    )
    result: dict[int, list[str]] = {}
    for run_id, short_id in rows:
        result.setdefault(run_id, []).append(short_id)
    return result


@dataclass
class RecordOutcome:
    created: list[Run] = field(default_factory=list)
    updated: list[Run] = field(default_factory=list)
    snapshot_baseline: Baseline | None = None  # E0 기록으로 스냅샷을 넓힌 기준선
    warnings: list[str] = field(default_factory=list)


def record_results(
    session: Session,
    project: Project,
    experiment: Experiment,
    table: ParsedTable,
    *,
    dataset_version_id: int | None = None,
    provenance: Provenance = Provenance.MANUAL,
) -> RecordOutcome:
    """표의 값을 실행별로 기록합니다. 검증을 모두 통과해야 아무것이라도 씁니다. 호출한 쪽이 커밋합니다."""
    existing = {run.label: run for run in _runs(session, experiment)}
    locked = _snapshot_baselines(session, [run.id for run in existing.values()])
    current_values = _values_by_run(session, [run.id for run in existing.values()])

    problems = []
    for row in table.rows:
        run = existing.get(row.label) if row.label else None
        if run is None or run.id not in locked:
            continue
        for key, value in row.values.items():
            if current_values.get(run.id, {}).get(key) != value:
                problems.append(
                    f"{row.line}번째 줄: {run.short_id}({run.label})은(는) 기준선 {', '.join(locked[run.id])}의 근거라 "
                    f"{key} 값을 바꿀 수 없습니다. 새 실행 이름으로 기록하세요."
                )
    new_rows = sum(1 for row in table.rows if not row.label or row.label not in existing)
    if len(existing) + new_rows > MAX_RUNS:
        problems.append(f"실험 하나에는 실행을 {MAX_RUNS}개까지 기록할 수 있습니다(지금 {len(existing)}개).")
    if problems:
        raise ValidationError({"results": "\n".join(problems)})

    criteria = {c.metric_key: c for c in get_criteria(session, project.id)}
    outcome = RecordOutcome()
    wrote_value = False
    for row in table.rows:
        run = existing.get(row.label) if row.label else None
        if run is not None and run.id in locked:
            continue  # 기준선 근거: 값이 같다는 것만 확인했고, 데이터 버전 등 어떤 것도 다시 쓰지 않습니다.
        if run is None:
            short_id = next_short_id(session, project.id, "R", scope=f"R:{experiment.id}")
            label = row.label or _auto_label(short_id, existing)
            run = Run(experiment_id=experiment.id, short_id=short_id, label=label,
                      status=RunStatus.DONE, finished_at=utcnow())
            session.add(run)
            session.flush()
            existing[run.label] = run
            outcome.created.append(run)
        elif row.values:
            outcome.updated.append(run)
            run.status = RunStatus.DONE
        for key, value in row.values.items():
            wrote_value = True
            result = session.scalars(
                select(MetricResult).where(MetricResult.run_id == run.id, MetricResult.metric_key == key)
            ).first()
            criterion = criteria.get(key)
            if result is None:
                result = MetricResult(run_id=run.id, metric_key=key)
                session.add(result)
            result.value = value
            result.unit = criterion.unit if criterion else None
            result.criterion_id = criterion.id if criterion else None
            result.dataset_version_id = dataset_version_id
            result.provenance = provenance
    if wrote_value:
        experiment.status = ExperimentStatus.DONE
    session.flush()

    if experiment.kind is ExperimentKind.BASELINE_MEASUREMENT and outcome.created:
        _extend_baseline_snapshot(session, project, experiment, outcome)
    return outcome


def _auto_label(short_id: str, existing: dict[str, Run]) -> str:
    """이름 없이 붙여넣은 실행의 이름. 사용자가 붙인 이름과 겹치면 짧은 ID를 덧붙입니다."""
    label = f"실행 {short_id[1:]}"
    return label if label not in existing else f"{label} ({short_id})"


def _extend_baseline_snapshot(session: Session, project: Project, experiment: Experiment,
                              outcome: RecordOutcome) -> None:
    baseline = session.scalars(select(Baseline).where(Baseline.source_experiment_id == experiment.id)).first()
    if baseline is None:
        return
    if baseline.id != project.current_baseline_id:
        outcome.warnings.append(
            f"기준선 {baseline.short_id}은(는) 지금 현재 기준선이 아니어서, 새 실행을 {baseline.short_id}의 근거에 넣지 않았습니다."
        )
        return
    change = BaselineChange(project_id=project.id, kind=BaselineChangeKind.SNAPSHOT_UPDATE,
                            from_baseline_id=baseline.id, to_baseline_id=baseline.id)
    session.add(change)
    session.flush()
    for run in outcome.created:
        session.add(BaselineRun(baseline_id=baseline.id, run_id=run.id, added_by_change_id=change.id))
    session.flush()
    outcome.snapshot_baseline = baseline


def exclude_run(session: Session, run: Run, reason: str) -> RunExclusion:
    """실행을 집계에서 뺍니다(추가만 가능한 기록). 기준선 스냅샷은 그대로입니다."""
    if not (reason or "").strip():
        raise ValidationError({"reason": "제외하는 이유를 적어 주세요. 예: 로그 손상"})
    if session.scalars(select(RunExclusion).where(RunExclusion.run_id == run.id)).first():
        raise ValidationError({"reason": f"{run.short_id}은(는) 이미 제외했습니다."})
    exclusion = RunExclusion(run_id=run.id, reason=reason.strip())
    session.add(exclusion)
    session.flush()
    return exclusion


# ---------------------------------------------------------------- 보기


def _values_by_run(session: Session, run_ids: list[int]) -> dict[int, dict[str, float]]:
    if not run_ids:
        return {}
    values: dict[int, dict[str, float]] = {}
    for run_id, key, value in session.execute(
        select(MetricResult.run_id, MetricResult.metric_key, MetricResult.value)
        .where(MetricResult.run_id.in_(run_ids)).order_by(MetricResult.id)
    ):
        values.setdefault(run_id, {})[key] = value
    return values


@dataclass
class RunRow:
    run: Run
    values: dict[str, float]
    exclusion: RunExclusion | None
    baselines: list[str]  # 이 실행을 근거로 쓰는 기준선

    def display(self, key: str) -> str:
        return format_plain(self.values[key]) if key in self.values else "—"


@dataclass
class MetricSummary:
    key: str
    criterion: SuccessCriterion | None
    aggregated: Aggregated | None  # 제외하지 않은 실행만

    @property
    def method(self) -> Aggregate:
        return self.criterion.aggregate if self.criterion else Aggregate.MEAN

    @property
    def display(self) -> str:
        return format_aggregated(self.aggregated, self.method) if self.aggregated else "—"

    @property
    def range_display(self) -> str:
        if not self.aggregated or self.aggregated.n < 2:
            return ""
        return f"{format_plain(self.aggregated.minimum)} ~ {format_plain(self.aggregated.maximum)}"


@dataclass
class ResultsView:
    rows: list[RunRow]
    metrics: list[MetricSummary]

    @property
    def included_count(self) -> int:
        return sum(1 for row in self.rows if row.exclusion is None)


def experiment_results(session: Session, experiment: Experiment) -> ResultsView:
    runs = _runs(session, experiment)
    run_ids = [run.id for run in runs]
    values = _values_by_run(session, run_ids)
    exclusions = {e.run_id: e for e in session.scalars(select(RunExclusion).where(RunExclusion.run_id.in_(run_ids)))}
    locked = _snapshot_baselines(session, run_ids)
    rows = [RunRow(run, values.get(run.id, {}), exclusions.get(run.id), locked.get(run.id, [])) for run in runs]

    criteria = get_criteria(session, experiment.project_id)
    by_key = {c.metric_key: c for c in criteria}
    present = {key for row in rows for key in row.values}
    ordered = [c.metric_key for c in criteria if c.metric_key in present] + sorted(present - set(by_key))
    summaries = []
    for key in ordered:
        criterion = by_key.get(key)
        included = [row.values[key] for row in rows if row.exclusion is None and key in row.values]
        method = criterion.aggregate if criterion else Aggregate.MEAN
        direction = criterion.direction if criterion else Direction.LOWER
        summaries.append(MetricSummary(key, criterion, aggregate(included, method, direction)))
    return ResultsView(rows, summaries)


def get_run(session: Session, experiment: Experiment, short_id: str) -> Run | None:
    return session.scalars(
        select(Run).where(Run.experiment_id == experiment.id, Run.short_id == short_id)
    ).one_or_none()
