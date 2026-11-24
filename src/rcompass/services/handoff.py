"""Claude Code 인계 Markdown (계획서 5절 "Claude Code 사용 흐름"의 인계 출력 예시).

새 Claude Code 세션의 첫 메시지에 그대로 붙여넣는 60줄 안팎의 문서를 만듭니다.
- 기각·보류·철회한 접근은 지금 집중 문제의 경로와 연구 질문 바로 아래 문제에 속한 것을 모두 넣고, 나머지는 개수만 적습니다.
- 끝에 결과 보고 형식(rcompass-result 블록)을 붙여, Claude Code가 실험을 마치면 그 형식으로 보고하게 합니다.
외부 LLM 없이 DB만 읽어서 만듭니다.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.ids import BASELINE_MEASUREMENT_ID
from rcompass.models import (
    Baseline,
    BaselineChange,
    BaselineChangeKind,
    Decision,
    DecisionType,
    Direction,
    Experiment,
    ExperimentKind,
    ExperimentOutcome,
    ExperimentStatus,
    FileKind,
    Problem,
    Project,
    StorageKind,
)
from rcompass.services.aggregate import format_aggregated, format_plain
from rcompass.services.datasets import VersionView
from rcompass.services.decisions import judgment
from rcompass.services.experiments import input_versions
from rcompass.services.files import list_files
from rcompass.services.problems import problem_path
from rcompass.services.progress import evaluate_project
from rcompass.services.projects import get_criteria
from rcompass.services.runs import snapshot_metric_keys

RESULT_FENCE = "rcompass-result"
MAX_FILES = 5
MAX_TEMPLATE_RUNS = 5
AGGREGATE_WORDS = {"mean": "평균", "median": "중앙값", "worst": "최악값"}
OUTCOME_WORDS = {
    ExperimentOutcome.ADOPTED: "채택",
    ExperimentOutcome.REJECTED: "기각",
    ExperimentOutcome.HELD: "보류",
    ExperimentOutcome.REVOKED: "채택 후 철회",
}


def one_line(text: str | None, limit: int = 160) -> str:
    """여러 줄 텍스트를 한 줄로. 코드 펜스(```)가 문서를 깨뜨리지 않게 바꾸고, 길면 줄입니다."""
    line = " ".join((text or "").split()).replace("```", "'''")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _cell(text: str) -> str:
    return one_line(text).replace("|", "\\|")


def _symbol(direction: Direction) -> str:
    return "≤" if direction is Direction.LOWER else "≥"


def _version_line(view: VersionView) -> str:
    v = view.version
    meta = [x for x in (v.source, v.collected_at.isoformat() if v.collected_at else None) if x]
    where = "첨부 파일" if v.storage is StorageKind.ATTACHED else (v.external_uri or "")
    text = view.label + (f" ({', '.join(one_line(m) for m in meta)})" if meta else "")
    return f"- {text} — {one_line(where)}" if where else f"- {text}"


def dataset_ref(view: VersionView) -> str:
    """결과 블록에서 쓰는 데이터 버전 표기. 예: DS1@v1.0"""
    return f"{view.dataset.short_id}@{view.version.version_label}"


@dataclass
class HandoffOptions:
    today: date
    experiment: Experiment | None = None  # 결과 보고 형식에 넣을 실험. 없으면 자동으로 고릅니다.


def _adoption_of(session: Session, baseline: Baseline) -> Decision | None:
    change = session.scalars(select(BaselineChange).where(
        BaselineChange.to_baseline_id == baseline.id, BaselineChange.kind == BaselineChangeKind.ADOPT)).first()
    return session.get(Decision, change.decision_id) if change else None


def _decisions_by_experiment(session: Session, project: Project) -> dict[int, list[Decision]]:
    result: dict[int, list[Decision]] = {}
    for decision in session.scalars(select(Decision).where(Decision.project_id == project.id).order_by(Decision.id)):
        result.setdefault(decision.experiment_id, []).append(decision)
    return result


def _approach_line(session: Session, project: Project, experiment: Experiment, decisions: list[Decision]) -> str:
    word = OUTCOME_WORDS[experiment.outcome]
    last = decisions[-1] if decisions else None
    detail = one_line(last.reason) if last else ""
    if last and last.type is DecisionType.HOLD and last.revisit_condition:
        detail += f" (재개 조건: {one_line(last.revisit_condition)})"
    numbers = []
    for c in judgment(session, project, experiment):
        if c.trial is None:
            continue
        method = c.criterion.aggregate
        base = format_aggregated(c.base, method) if c.base else "—"
        numbers.append(f"{c.criterion.name} {base} → {format_aggregated(c.trial, method)}")
    tail = f" · {'; '.join(numbers)}" if numbers else ""
    return f"- {experiment.short_id} {one_line(experiment.title)} — **{word}**: {detail}{tail}"


def _template_experiment(session: Session, project: Project, focus: Problem | None) -> Experiment | None:
    """결과를 기다리는 실험: 집중 문제의 아직 결과 없는 실험, 없으면 프로젝트에서 가장 최근의 결과 없는 실험."""
    query = select(Experiment).where(Experiment.project_id == project.id, Experiment.status == ExperimentStatus.PLANNED,
                                     Experiment.short_id != BASELINE_MEASUREMENT_ID).order_by(Experiment.id.desc())
    if focus is not None:
        found = session.scalars(query.where(Experiment.problem_id == focus.id)).first()
        if found:
            return found
    return session.scalars(query).first()


SAFE_PLAIN = re.compile(r"^[A-Za-z0-9_./@=+\-]+$")
MAX_TEMPLATE_CONFIG = 200


def yaml_value(text: str) -> str:
    """블록에 넣을 값. 평범한 토큰은 그대로, 그 밖에는 따옴표로 감싸 ': '나 ' #'이 YAML을 깨지 않게 합니다."""
    return text if SAFE_PLAIN.match(text) else json.dumps(text, ensure_ascii=False)


def _template_field(value: str | None, placeholder: str, limit: int) -> str:
    """줄이거나 바꾸면 '다른 값'으로 기록되므로, 그대로 넣을 수 없는 값은 안내 문구로 둡니다(안내 문구는 빈 값으로 읽힘)."""
    if not value:
        return placeholder
    if "\n" in value.strip() or len(value) > limit or "```" in value:
        return "<실험에 기록한 값과 같으면 그대로 두세요>"
    return yaml_value(value.strip())


def result_block_template(session: Session, project: Project, experiment: Experiment | None) -> list[str]:
    criteria = get_criteria(session, project.id)
    sources = [experiment] if experiment else []
    keys = [c.metric_key for c in criteria]
    if experiment is not None and experiment.kind is ExperimentKind.BASELINE_MEASUREMENT:
        # 기준선 측정 실험은 그 기준선의 방식을 그대로 다시 재므로, 비어 있는 칸은 기준선 근거 실험의 값으로 채웁니다.
        measured = session.get(Baseline, experiment.based_on_baseline_id) if experiment.based_on_baseline_id else None
        if measured is not None:
            sources.append(session.get(Experiment, measured.source_experiment_id))
            # B0(E0) 밖의 측정 실험은 그 기준선이 아직 재지 않은 지표만 채울 수 있습니다.
            held = snapshot_metric_keys(session, measured)
            keys = [key for key in keys if key not in held] or keys
    inputs = next((views for views in (input_versions(session, e) for e in sources) if views), [])
    code = next((e.code_ref for e in sources if e.code_ref), None)
    config = next((e.config_text for e in sources if e.config_text), None)
    run_count = max([1, *(c.min_runs for c in criteria)])
    metrics = ", ".join(f"{key}: " for key in keys)
    lines = [f"```{RESULT_FENCE}"]
    lines.append(f"experiment: {experiment.short_id if experiment else '<앱에서 만든 실험 ID (예 E3)>'}")
    lines.append(f"dataset: {yaml_value(dataset_ref(inputs[0])) if inputs else '<데이터 버전 (예 DS1@v1.0)>'}")
    lines.append(f"code: {_template_field(code, '<commit sha>', 200)}")
    lines.append(f"config: {_template_field(config, '<경로 또는 핵심 설정>', MAX_TEMPLATE_CONFIG)}")
    lines.append("runs:")
    for index in range(1, min(run_count, MAX_TEMPLATE_RUNS) + 1):
        lines.append(f"  - {{label: seed={index}, metrics: {{{metrics}}}}}")
    lines.append("files: [<생성 파일 경로>]")
    lines.append("note: <한 줄 해석>")
    lines.append("```")
    return lines


def build_handoff(session: Session, project: Project, options: HandoffOptions) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# 연구 인계: {one_line(project.title)} ({options.today.isoformat()} 기준)")
    add("> Claude Code에게: 연구 기록 앱에서 내보낸 현재 상태입니다. 기각된 접근은 이유가 해소되지 않는 한 "
        "다시 제안하지 마세요. 코드와 Git은 요청 없이 바꾸지 마세요.")
    add("")
    add("## 연구 질문")
    add(one_line(project.question, 400))
    add("")

    baseline = session.get(Baseline, project.current_baseline_id) if project.current_baseline_id else None
    add("## 성공 기준")
    add(f"| 기준 | 목표 | 현재 기준선{' ' + baseline.short_id if baseline else ''} | 상태 |")
    add("|---|---|---|---|")
    for evaluation in evaluate_project(session, project):
        c = evaluation.criterion
        notes = [AGGREGATE_WORDS[c.aggregate.value]]
        if c.min_runs > 1:
            notes.append(f"n≥{c.min_runs}")
        if c.eval_dataset_version_id:
            notes.insert(0, "지정 평가셋")
        unit = f" {_cell(c.unit)}" if c.unit else ""
        add(f"| {_cell(c.name)} (`{c.metric_key}`, {', '.join(notes)}) | {_symbol(c.direction)} "
            f"{format_plain(c.target)}{unit} | {evaluation.display_value} | {evaluation.state_label} |")
    add("")

    add("## 현재 기준선")
    if baseline:
        source = session.get(Experiment, baseline.source_experiment_id)
        adoption = _adoption_of(session, baseline)
        origin = (f"{source.short_id}에서 {adoption.created_at.date().isoformat()} 채택" if adoption
                  else "프로젝트 시작 기준선")
        parts = [f"{baseline.short_id} {one_line(baseline.label)} ({origin})"]
        if source.code_ref:
            parts.append(f"코드 {one_line(source.code_ref, 80)}")
        if source.config_text:
            parts.append(f"설정 {one_line(source.config_text, 80)}")
        add(" · ".join(parts))
    else:
        add("없음")
    add("")

    focus = session.get(Problem, project.focus_problem_id) if project.focus_problem_id else None
    focus_path = problem_path(session, focus) if focus else []
    add("## 지금 집중할 문제")
    if focus:
        add("원래 질문 › " + " › ".join(f"{p.short_id} {one_line(p.title)}" for p in focus_path))
        add(f"이유: {one_line(focus.why)}")
    else:
        add("지정하지 않았습니다.")
    add("")

    in_scope = {p.id for p in focus_path} | set(session.scalars(
        select(Problem.id).where(Problem.project_id == project.id, Problem.parent_id.is_(None))))
    decided = list(session.scalars(
        select(Experiment).where(Experiment.project_id == project.id, Experiment.kind == ExperimentKind.TRIAL,
                                 Experiment.outcome.is_not(None)).order_by(Experiment.id)))
    decisions = _decisions_by_experiment(session, project)
    shown = [e for e in decided if e.problem_id in in_scope]
    others = Counter(OUTCOME_WORDS[e.outcome] for e in decided if e.problem_id not in in_scope)
    add("## 시도한 접근")
    for experiment in shown:
        add(_approach_line(session, project, experiment, decisions.get(experiment.id, [])))
    if not shown:
        add("아직 판정한 실험이 없습니다.")
    if others:
        summary = ", ".join(f"{word} {count}" for word, count in sorted(others.items()))
        add(f"- 그 밖의 문제에서 판정한 실험 {sum(others.values())}개({summary})는 앱에서 확인하세요.")
    add("")

    template_experiment = options.experiment or _template_experiment(session, project, focus)
    related = [session.get(Experiment, baseline.source_experiment_id)] if baseline else []
    related += [e for e in shown if e not in related]
    if template_experiment and template_experiment not in related:
        related.append(template_experiment)
    versions: dict[int, VersionView] = {}
    for experiment in related:
        for view in input_versions(session, experiment):
            versions.setdefault(view.version.id, view)
    files = [f for e in related for f in list_files(session, project.id, experiment=e)
             if f.kind in (FileKind.FIGURE, FileKind.TABLE, FileKind.CONFIG)]
    add("## 관련 자료")
    for view in versions.values():
        add(_version_line(view))
    owners = {e.id: e.short_id for e in related}
    for record in files[:MAX_FILES]:
        add(f"- {record.short_id} {one_line(record.original_name)} ← {owners[record.produced_by_experiment_id]}")
    if len(files) > MAX_FILES:
        add(f"- 그 밖의 파일 {len(files) - MAX_FILES}개는 앱의 자료 화면에서 확인하세요.")
    if not versions and not files:
        add("기록한 자료가 없습니다.")
    add("")

    add("## 다음 행동")
    add(one_line(project.next_action, 300) if project.next_action else "정하지 않았습니다.")
    add("")

    add("## 결과 보고 형식")
    if template_experiment is not None:
        kind = " (기준선 측정: 방식은 바꾸지 않고 재기만 합니다)" if template_experiment.kind is ExperimentKind.BASELINE_MEASUREMENT else ""
        add(f"대상 실험: {template_experiment.short_id} {one_line(template_experiment.title)}{kind}")
    else:
        add("대상 실험: 아직 없습니다. 연구자에게 앱에서 실험을 먼저 만들어 달라고 하세요.")
    add("실험이 끝나면 아래 블록만 출력하세요. 연구자가 확인한 뒤 기록합니다. 재지 않은 지표는 비워 두세요.")
    lines.extend(result_block_template(session, project, template_experiment))
    return "\n".join(lines) + "\n"
