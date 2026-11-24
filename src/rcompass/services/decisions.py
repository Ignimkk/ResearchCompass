"""결정(채택·기각·보류·채택 철회)과 기준선 전이 (계획서 4절 "결정에 따른 상태 변화").

규칙
- 결정과 기준선 변경은 추가만 합니다. 예전 판단은 지우지 않고 새 결정(철회)으로 뒤집습니다.
- 채택하면 새 기준선을 만들고, 그 순간 집계에 들어간 실행을 스냅샷(BaselineRun)으로 고정합니다.
- 채택에는 재현성 관문이 있습니다: 데이터 버전, 코드 버전, 설정, 기준별 최소 실행 수(min_runs).
  기각과 보류는 빠진 항목을 경고만 하고 저장합니다.
- 철회하면 기본으로 철회되는 기준선의 부모(철회되지 않은 가장 가까운 조상)로 돌아갑니다.
  철회하는 기준선이 지금 현재 기준선이 아니면(그 위에 다른 기준선이 채택된 경우 등) 복귀 대상을 직접 고릅니다.
- 앱 기록(DB)만 바꿉니다. 연구 저장소의 Git이나 파일은 건드리지 않습니다.

모든 함수는 호출한 쪽이 커밋합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from rcompass.ids import next_short_id
from rcompass.models import (
    Baseline,
    BaselineChange,
    BaselineChangeKind,
    BaselineRun,
    Decision,
    DecisionType,
    Experiment,
    ExperimentKind,
    ExperimentOutcome,
    FileKind,
    FileObject,
    MetricResult,
    Project,
    Run,
    RunExclusion,
    SuccessCriterion,
)
from rcompass.services import ValidationError
from rcompass.services.aggregate import Aggregated, aggregate, decimals_of, format_number
from rcompass.services.experiments import input_versions
from rcompass.services.progress import CriterionState, STATE_LABELS, baseline_values, criterion_run_values, state_of
from rcompass.services.projects import get_criteria

DECISION_LABELS = {
    DecisionType.ADOPT: "채택",
    DecisionType.REJECT: "기각",
    DecisionType.HOLD: "보류",
    DecisionType.REVOKE: "채택 철회",
}

OUTCOME_FOR = {
    DecisionType.ADOPT: ExperimentOutcome.ADOPTED,
    DecisionType.REJECT: ExperimentOutcome.REJECTED,
    DecisionType.HOLD: ExperimentOutcome.HELD,
}


def _clean(text: str | None) -> str | None:
    return (text or "").strip() or None


# ---------------------------------------------------------------- 어떤 결정을 내릴 수 있나


def allowed_decisions(experiment: Experiment) -> list[DecisionType]:
    """지금 이 실험에 내릴 수 있는 결정. 보류한 실험은 다시 판정할 수 있고, 채택한 실험은 철회만 됩니다."""
    if experiment.kind is not ExperimentKind.TRIAL:
        return []
    if experiment.outcome is None:
        return [DecisionType.ADOPT, DecisionType.REJECT, DecisionType.HOLD]
    if experiment.outcome is ExperimentOutcome.HELD:
        return [DecisionType.ADOPT, DecisionType.REJECT]
    if experiment.outcome is ExperimentOutcome.ADOPTED:
        return [DecisionType.REVOKE]
    return []  # 기각·철회는 마지막 판단입니다. 다시 시도하려면 새 실험을 만듭니다.


def _not_allowed_message(experiment: Experiment, decision_type: DecisionType) -> str:
    if experiment.kind is not ExperimentKind.TRIAL:
        return "기준선 측정 실험은 판정하지 않습니다."
    if experiment.outcome is ExperimentOutcome.ADOPTED:
        return "이미 채택한 실험입니다. 판단을 바꾸려면 채택 철회를 하세요."
    if experiment.outcome is ExperimentOutcome.REJECTED:
        return "이미 기각한 실험입니다. 다시 시도하려면 새 실험을 만드세요."
    if experiment.outcome is ExperimentOutcome.REVOKED:
        return "채택을 철회한 실험입니다. 다시 시도하려면 새 실험을 만드세요."
    if decision_type is DecisionType.REVOKE:
        return "채택한 실험만 철회할 수 있습니다."
    return "이미 보류한 실험입니다. 채택하거나 기각하세요."


# ---------------------------------------------------------------- 재현성 관문


def included_run_ids(session: Session, experiment: Experiment) -> list[int]:
    """집계에 들어가는 실행: 제외하지 않았고 수치가 하나 이상 있는 실행."""
    return list(session.scalars(
        select(Run.id)
        .where(Run.experiment_id == experiment.id,
               ~exists().where(RunExclusion.run_id == Run.id),
               exists().where(MetricResult.run_id == Run.id))
        .order_by(Run.id)
    ))


@dataclass(frozen=True)
class MissingItem:
    label: str
    anchor: str  # 실험 화면에서 채우러 갈 구역(id)


@dataclass
class Gate:
    missing: list[MissingItem] = field(default_factory=list)  # 채택을 막는 항목
    warnings: list[str] = field(default_factory=list)  # 막지는 않지만 알아야 할 것

    @property
    def ok(self) -> bool:
        return not self.missing

    @property
    def record_gaps(self) -> list[MissingItem]:
        """재현 기록(데이터, 코드, 설정)만. 기각·보류 때 경고로 보여줍니다."""
        return [item for item in self.missing if item.anchor == "inputs"]


def _has_config(session: Session, experiment: Experiment) -> bool:
    if _clean(experiment.config_text):
        return True
    return bool(session.scalar(select(exists().where(
        FileObject.produced_by_experiment_id == experiment.id, FileObject.kind == FileKind.CONFIG))))


def adoption_gate(session: Session, project: Project, experiment: Experiment) -> Gate:
    gate = Gate()
    if not input_versions(session, experiment):
        gate.missing.append(MissingItem("입력 데이터 버전", "inputs"))
    if not _clean(experiment.code_ref):
        gate.missing.append(MissingItem("코드 버전(커밋 SHA 또는 메모)", "inputs"))
    if not _has_config(session, experiment):
        gate.missing.append(MissingItem("설정(텍스트로 적거나 설정 파일 첨부)", "inputs"))

    run_ids = included_run_ids(session, experiment)
    current = session.get(Baseline, project.current_baseline_id) if project.current_baseline_id else None
    measured = 0
    for criterion in get_criteria(session, project.id):
        n = len(criterion_run_values(session, run_ids, criterion)) if run_ids else 0
        if n:
            measured += 1
            if n < criterion.min_runs:
                gate.missing.append(MissingItem(
                    f"{criterion.short_id} {criterion.name}: 실행 {n}/{criterion.min_runs}", "results"))
        elif current is not None and baseline_values(session, current.id, criterion):
            gate.warnings.append(
                f"{criterion.short_id} {criterion.name}은(는) 이 실험에서 재지 않아, 새 기준선에서는 미측정이 됩니다.")
    if not measured:
        gate.missing.append(MissingItem("성공 기준 지표의 결과", "results"))
    if current is not None and experiment.based_on_baseline_id not in (None, current.id):
        start = session.get(Baseline, experiment.based_on_baseline_id)
        gate.warnings.append(
            f"이 실험은 {start.short_id}에서 출발했지만 현재 기준선은 {current.short_id}입니다. "
            f"채택하면 {current.short_id}의 다음 기준선이 됩니다.")
    return gate


# ---------------------------------------------------------------- 채택, 기각, 보류


def decide(
    session: Session,
    project: Project,
    experiment: Experiment,
    decision_type: DecisionType,
    *,
    reason: str,
    baseline_label: str | None = None,
    revisit_condition: str | None = None,
) -> Decision:
    """채택, 기각, 보류를 기록합니다. 채택이면 새 기준선과 실행 스냅샷을 만들고 현재 기준선을 옮깁니다."""
    if decision_type is DecisionType.REVOKE:
        raise ValueError("철회는 revoke_adoption을 쓰세요.")
    if decision_type not in allowed_decisions(experiment):
        raise ValidationError({"decision": _not_allowed_message(experiment, decision_type)})
    errors: dict[str, str] = {}
    if not _clean(reason):
        errors["reason"] = "이유를 한 줄 적어 주세요. 나중에 왜 그렇게 판단했는지 기억하기 위해서입니다."
    if decision_type is DecisionType.ADOPT:
        gate = adoption_gate(session, project, experiment)
        if not gate.ok:
            errors["decision"] = "채택하려면 먼저 채우세요: " + ", ".join(item.label for item in gate.missing)
    if errors:
        raise ValidationError(errors)

    decision = Decision(
        project_id=project.id,
        short_id=next_short_id(session, project.id, "D"),
        experiment_id=experiment.id,
        type=decision_type,
        reason=_clean(reason),
        revisit_condition=_clean(revisit_condition) if decision_type is DecisionType.HOLD else None,
    )
    session.add(decision)
    session.flush()

    if decision_type is DecisionType.ADOPT:
        previous = project.current_baseline_id
        baseline = Baseline(
            project_id=project.id,
            short_id=next_short_id(session, project.id, "B"),
            label=_clean(baseline_label) or experiment.title,
            source_experiment_id=experiment.id,
            parent_baseline_id=previous,
        )
        session.add(baseline)
        session.flush()
        change = BaselineChange(project_id=project.id, kind=BaselineChangeKind.ADOPT,
                                from_baseline_id=previous, to_baseline_id=baseline.id, decision_id=decision.id)
        session.add(change)
        session.flush()
        for run_id in included_run_ids(session, experiment):
            session.add(BaselineRun(baseline_id=baseline.id, run_id=run_id, added_by_change_id=change.id))
        project.current_baseline_id = baseline.id
    experiment.outcome = OUTCOME_FOR[decision_type]
    session.flush()
    return decision


# ---------------------------------------------------------------- 채택 철회


def revoked_baseline_ids(session: Session, project_id: int) -> set[int]:
    """채택이 철회된 기준선."""
    revoked_adopts = select(Decision.revokes_decision_id).where(
        Decision.project_id == project_id, Decision.type == DecisionType.REVOKE)
    return set(session.scalars(
        select(BaselineChange.to_baseline_id).where(
            BaselineChange.kind == BaselineChangeKind.ADOPT, BaselineChange.decision_id.in_(revoked_adopts))
    ))


def _active_adoption(session: Session, experiment: Experiment) -> Decision | None:
    revoked = select(Decision.revokes_decision_id).where(Decision.revokes_decision_id.is_not(None))
    return session.scalars(
        select(Decision).where(Decision.experiment_id == experiment.id, Decision.type == DecisionType.ADOPT,
                               Decision.id.not_in(revoked))
        .order_by(Decision.id.desc())
    ).first()


def _adopted_baseline(session: Session, decision: Decision) -> Baseline:
    change = session.scalars(select(BaselineChange).where(
        BaselineChange.kind == BaselineChangeKind.ADOPT, BaselineChange.decision_id == decision.id)).one()
    return session.get(Baseline, change.to_baseline_id)


def _ancestors(session: Session, baseline: Baseline) -> list[Baseline]:
    chain, seen = [], {baseline.id}
    parent_id = baseline.parent_baseline_id
    while parent_id is not None and parent_id not in seen:
        parent = session.get(Baseline, parent_id)
        chain.append(parent)
        seen.add(parent_id)
        parent_id = parent.parent_baseline_id
    return chain


@dataclass
class RevokePlan:
    adoption: Decision
    baseline: Baseline  # 철회되는 기준선
    current: Baseline
    targets: list[Baseline]  # 돌아갈 수 있는 기준선(철회된 것 제외)
    default: Baseline | None  # None이면 직접 골라야 합니다
    ancestors_of_current: list[Baseline] = field(default_factory=list)

    @property
    def is_current(self) -> bool:
        return self.baseline.id == self.current.id

    @property
    def warning(self) -> str | None:
        if self.is_current:
            return None
        on_top = self.baseline.id in {b.id for b in self.ancestors_of_current}
        if on_top:
            return (f"{self.current.short_id}이(가) {self.baseline.short_id} 위에 있습니다. "
                    f"철회한 뒤 어느 기준선을 현재 기준선으로 둘지 직접 고르세요.")
        return (f"{self.baseline.short_id}은(는) 지금 현재 기준선이 아닙니다(현재 {self.current.short_id}). "
                "철회한 뒤 현재 기준선으로 둘 기준선을 직접 고르세요.")


def revoke_plan(session: Session, project: Project, experiment: Experiment) -> RevokePlan | None:
    if experiment.outcome is not ExperimentOutcome.ADOPTED:
        return None
    adoption = _active_adoption(session, experiment)
    if adoption is None:
        return None
    baseline = _adopted_baseline(session, adoption)
    current = session.get(Baseline, project.current_baseline_id)
    revoked = revoked_baseline_ids(session, project.id)
    targets = list(session.scalars(
        select(Baseline).where(Baseline.project_id == project.id, Baseline.id != baseline.id,
                               Baseline.id.not_in(revoked))
        .order_by(Baseline.id)
    ))
    default = None
    if current.id == baseline.id:
        default = next((b for b in _ancestors(session, baseline) if b.id not in revoked), None)
    return RevokePlan(adoption=adoption, baseline=baseline, current=current, targets=targets, default=default,
                      ancestors_of_current=_ancestors(session, current))


def revoke_adoption(
    session: Session,
    project: Project,
    experiment: Experiment,
    *,
    reason: str,
    target_short_id: str | None = None,
) -> Decision:
    """채택을 철회합니다. 원래 채택 결정과 기준선은 그대로 남고, 현재 기준선만 복귀 대상으로 옮깁니다."""
    plan = revoke_plan(session, project, experiment)
    if plan is None:
        raise ValidationError({"decision": _not_allowed_message(experiment, DecisionType.REVOKE)})
    errors: dict[str, str] = {}
    if not _clean(reason):
        errors["revoke_reason"] = "철회하는 이유를 적어 주세요. 예: 평가셋 일부가 예시에 섞임"
    target = plan.default
    if _clean(target_short_id):
        target = next((b for b in plan.targets if b.short_id == target_short_id.strip()), None)
        if target is None:
            errors["revoke_target"] = "고른 기준선으로는 돌아갈 수 없습니다."
    elif target is None:
        errors["revoke_target"] = "철회한 뒤 현재 기준선으로 둘 기준선을 고르세요."
    if errors:
        raise ValidationError(errors)

    decision = Decision(
        project_id=project.id,
        short_id=next_short_id(session, project.id, "D"),
        experiment_id=experiment.id,
        type=DecisionType.REVOKE,
        reason=_clean(reason),
        revokes_decision_id=plan.adoption.id,
    )
    session.add(decision)
    session.flush()
    session.add(BaselineChange(project_id=project.id, kind=BaselineChangeKind.REVOKE,
                               from_baseline_id=plan.current.id, to_baseline_id=target.id, decision_id=decision.id))
    project.current_baseline_id = target.id
    experiment.outcome = ExperimentOutcome.REVOKED
    session.flush()
    return decision


# ---------------------------------------------------------------- 결정 이력


@dataclass
class DecisionRow:
    decision: Decision
    revoked_by: Decision | None = None  # 이 채택을 뒤집은 철회
    baseline: Baseline | None = None  # 채택으로 만든 기준선
    moved_to: Baseline | None = None  # 이 결정으로 옮겨 간 현재 기준선(채택·철회)
    moved_from: Baseline | None = None

    @property
    def label(self) -> str:
        return DECISION_LABELS[self.decision.type]


def decision_history(session: Session, experiment: Experiment) -> list[DecisionRow]:
    decisions = list(session.scalars(
        select(Decision).where(Decision.experiment_id == experiment.id).order_by(Decision.id)))
    rows = {d.id: DecisionRow(d) for d in decisions}
    changes = session.scalars(select(BaselineChange).where(BaselineChange.decision_id.in_(list(rows))))
    for change in changes:
        row = rows[change.decision_id]
        row.moved_to = session.get(Baseline, change.to_baseline_id)
        row.moved_from = session.get(Baseline, change.from_baseline_id) if change.from_baseline_id else None
        if change.kind is BaselineChangeKind.ADOPT:
            row.baseline = row.moved_to
    for decision in decisions:
        if decision.revokes_decision_id in rows:
            rows[decision.revokes_decision_id].revoked_by = decision
    return list(rows.values())


def excluded_snapshot_runs(session: Session, baseline: Baseline) -> list[str]:
    """기준선 근거인데 나중에 제외한 실행에 대한 경고. 기준선 수치는 그대로라 사람이 다시 판단해야 합니다."""
    rows = session.execute(
        select(Experiment.short_id, Run.short_id, Run.label, RunExclusion.reason)
        .join(BaselineRun, BaselineRun.run_id == Run.id)
        .join(RunExclusion, RunExclusion.run_id == Run.id)
        .join(Experiment, Experiment.id == Run.experiment_id)
        .where(BaselineRun.baseline_id == baseline.id)
        .order_by(Run.id)
    )
    return [
        f"기준선 {baseline.short_id}의 근거 실행 {e}/{r}({label})이(가) 제외되었습니다(이유: {reason}). "
        "기준선 수치는 그대로이니, 철회하거나 다시 판정할지 검토하세요."
        for e, r, label, reason in rows
    ]


# ---------------------------------------------------------------- 판정 패널: 기준선 대비 비교


@dataclass
class Comparison:
    criterion: SuccessCriterion
    baseline: Baseline | None  # 비교 대상(출발 기준선)
    base: Aggregated | None
    trial: Aggregated | None
    replaced_start: Baseline | None = None  # 출발 기준선이 철회되어 대신 현재 기준선과 비교한 경우 그 출발 기준선
    base_points: list[tuple[str, float]] = field(default_factory=list)  # (실행 이름, 값)
    trial_points: list[tuple[str, float]] = field(default_factory=list)

    @property
    def state(self) -> CriterionState:
        return state_of(self.trial, self.criterion)

    @property
    def state_label(self) -> str:
        return STATE_LABELS[self.state]

    @property
    def delta(self) -> float | None:
        if self.base is None or self.trial is None:
            return None
        return self.trial.value - self.base.value

    @property
    def delta_display(self) -> str:
        delta = self.delta
        if delta is None:
            return ""
        values = self.base.values + self.trial.values
        places = max(decimals_of(values), 1 if max(self.base.n, self.trial.n) > 1 else 0)
        if round(delta, places) == 0:
            return "차이 없음"
        sign = "+" if delta > 0 else "−"
        better = (delta < 0) == (self.criterion.direction.value == "lower")
        return f"{sign}{format_number(abs(delta), places)} ({'좋아짐' if better else '나빠짐'})"

    @property
    def overlaps(self) -> bool:
        """두 범위(최소~최대)가 겹치면 차이가 실행 간 변동 안에 있을 수 있습니다. 실행이 하나씩뿐이면 판단하지 않습니다."""
        if self.base is None or self.trial is None or max(self.base.n, self.trial.n) < 2:
            return False
        return self.trial.minimum <= self.base.maximum and self.base.minimum <= self.trial.maximum


def _run_names(session: Session, run_ids) -> dict[int, str]:
    rows = session.execute(
        select(Run.id, Experiment.short_id, Run.short_id, Run.label)
        .join(Experiment, Experiment.id == Run.experiment_id).where(Run.id.in_(list(run_ids)))
    )
    return {run_id: f"{e}/{r} {label}" for run_id, e, r, label in rows}


def judgment(session: Session, project: Project, experiment: Experiment) -> list[Comparison]:
    """성공 기준마다 출발 기준선과 이 실험(제외 뺀 실행)의 수치를 나란히 놓습니다. 둘 다 없는 기준은 뺍니다.

    출발 기준선이 철회되었으면 현재 기준선과 비교하고 ``replaced_start``에 원래 출발 기준선을 남깁니다.
    """
    baseline_id = experiment.based_on_baseline_id or project.current_baseline_id
    replaced_start = None
    if baseline_id in revoked_baseline_ids(session, project.id):
        # 출발 기준선의 채택이 철회되었으면 그 수치는 더 이상 기준이 아니므로 현재 기준선과 비교합니다.
        replaced_start = session.get(Baseline, baseline_id)
        baseline_id = project.current_baseline_id
    baseline = session.get(Baseline, baseline_id) if baseline_id else None
    run_ids = included_run_ids(session, experiment)
    snapshot = select(BaselineRun.run_id).where(BaselineRun.baseline_id == baseline.id) if baseline else None
    comparisons = []
    for criterion in get_criteria(session, project.id):
        trial = criterion_run_values(session, run_ids, criterion) if run_ids else {}
        base = criterion_run_values(session, snapshot, criterion) if baseline else {}
        if not trial and not base:
            continue
        names = _run_names(session, [*trial, *base])
        comparisons.append(Comparison(
            criterion=criterion,
            baseline=baseline,
            replaced_start=replaced_start,
            base=aggregate(list(base.values()), criterion.aggregate, criterion.direction),
            trial=aggregate(list(trial.values()), criterion.aggregate, criterion.direction),
            base_points=[(names[i], v) for i, v in base.items()],
            trial_points=[(names[i], v) for i, v in trial.items()],
        ))
    return comparisons
