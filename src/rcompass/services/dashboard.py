"""프로젝트 홈 대시보드 (여정 1, 5: 며칠 뒤 돌아왔을 때 한 화면에서 상태를 파악).

목표 달성(현재 기준선의 성공 기준 판정)과 작업 진행(실험·문제를 얼마나 처리했나)은 따로 계산합니다.
작업이 많이 진행되어도 목표에 가까워졌다는 뜻은 아니기 때문입니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from rcompass.ids import BASELINE_ZERO_ID
from rcompass.models import (
    Baseline,
    BaselineChange,
    BaselineChangeKind,
    Decision,
    Experiment,
    ExperimentKind,
    ExperimentOutcome,
    ExperimentStatus,
    MetricResult,
    Problem,
    ProblemStatus,
    Project,
    Run,
    RunExclusion,
    SuccessCriterion,
)
from rcompass.services import ValidationError
from rcompass.services.aggregate import Aggregated, aggregate, format_aggregated
from rcompass.services.decisions import DECISION_LABELS, excluded_snapshot_runs, included_run_ids
from rcompass.services.projects import MAX_NEXT_ACTION, next_action_error, one_line
from rcompass.services.progress import (
    CriterionEvaluation,
    CriterionState,
    criterion_run_values,
    evaluate_project,
    goal_summary,
)

FINAL_OUTCOMES = (ExperimentOutcome.ADOPTED, ExperimentOutcome.REJECTED, ExperimentOutcome.REVOKED)


# ---------------------------------------------------------------- 기준별 최근 시도


@dataclass(frozen=True)
class Attempt:
    """이 기준의 지표를 가장 최근에 기록한 시도 실험과 그 값."""

    experiment: Experiment
    aggregated: Aggregated

    def display(self, criterion: SuccessCriterion) -> str:
        return format_aggregated(self.aggregated, criterion.aggregate)


def latest_attempt(session: Session, project: Project, criterion: SuccessCriterion) -> Attempt | None:
    """기준선 측정(E0)을 뺀 시도 실험 중, 제외하지 않은 실행에 이 지표를 가장 나중에 기록한 실험.

    값을 다시 기록하면 결과 행을 새로 만들므로(runs.record_results) id 순서가 기록 순서입니다.
    """
    query = (
        select(Run.experiment_id)
        .join(MetricResult, MetricResult.run_id == Run.id)
        .join(Experiment, Experiment.id == Run.experiment_id)
        .where(Experiment.project_id == project.id, Experiment.kind == ExperimentKind.TRIAL,
               MetricResult.metric_key == criterion.metric_key,
               ~exists().where(RunExclusion.run_id == Run.id))
        .group_by(Run.experiment_id)
        .order_by(func.max(MetricResult.id).desc())
    )
    if criterion.eval_dataset_version_id is not None:
        query = query.where(MetricResult.dataset_version_id == criterion.eval_dataset_version_id)
    experiment_id = session.scalars(query.limit(1)).first()
    if experiment_id is None:
        return None
    experiment = session.get(Experiment, experiment_id)
    values = list(criterion_run_values(session, included_run_ids(session, experiment), criterion).values())
    agg = aggregate(values, criterion.aggregate, criterion.direction)
    return Attempt(experiment, agg) if agg else None


@dataclass(frozen=True)
class CriterionRow:
    evaluation: CriterionEvaluation
    attempt: Attempt | None

    @property
    def criterion(self) -> SuccessCriterion:
        return self.evaluation.criterion

    @property
    def attempt_display(self) -> str:
        return self.attempt.display(self.criterion) if self.attempt else ""


# ---------------------------------------------------------------- 작업 진행


@dataclass(frozen=True)
class WorkProgress:
    experiments_total: int  # 시도 실험(E0 제외)
    experiments_decided: int  # 채택·기각·철회로 판정을 마친 실험
    experiments_held: int
    awaiting: list[Experiment]  # 결과는 있지만 아직 판정하지 않은 실험
    problems_total: int  # 중단한 문제 제외
    problems_resolved: int
    problems_dropped: int


def work_progress(session: Session, project: Project) -> WorkProgress:
    trials = list(session.scalars(
        select(Experiment).where(Experiment.project_id == project.id, Experiment.kind == ExperimentKind.TRIAL)
        .order_by(Experiment.id)
    ))
    status_counts = dict(session.execute(
        select(Problem.status, func.count()).where(Problem.project_id == project.id).group_by(Problem.status)
    ).all())
    dropped = status_counts.get(ProblemStatus.DROPPED, 0)
    return WorkProgress(
        experiments_total=len(trials),
        experiments_decided=sum(1 for e in trials if e.outcome in FINAL_OUTCOMES),
        experiments_held=sum(1 for e in trials if e.outcome is ExperimentOutcome.HELD),
        awaiting=[e for e in trials if e.outcome is None and e.status is ExperimentStatus.DONE],
        problems_total=sum(status_counts.values()) - dropped,
        problems_resolved=status_counts.get(ProblemStatus.RESOLVED, 0),
        problems_dropped=dropped,
    )


# ---------------------------------------------------------------- 마지막 결정과 기준선 이력


@dataclass(frozen=True)
class DecisionSummary:
    decision: Decision
    experiment: Experiment
    moved_from: Baseline | None = None
    moved_to: Baseline | None = None

    @property
    def label(self) -> str:
        return DECISION_LABELS[self.decision.type]


def last_decision(session: Session, project: Project) -> DecisionSummary | None:
    decision = session.scalars(
        select(Decision).where(Decision.project_id == project.id).order_by(Decision.id.desc())
    ).first()
    if decision is None:
        return None
    change = session.scalars(select(BaselineChange).where(BaselineChange.decision_id == decision.id)).first()
    return DecisionSummary(
        decision=decision,
        experiment=session.get(Experiment, decision.experiment_id),
        moved_from=session.get(Baseline, change.from_baseline_id) if change and change.from_baseline_id else None,
        moved_to=session.get(Baseline, change.to_baseline_id) if change else None,
    )


CHANGE_LABELS = {
    BaselineChangeKind.INITIAL: "시작",
    BaselineChangeKind.ADOPT: "채택",
    BaselineChangeKind.REVOKE: "철회로 복귀",
    BaselineChangeKind.SNAPSHOT_UPDATE: "근거 실행 추가",
}


@dataclass(frozen=True)
class ChangeRow:
    change: BaselineChange
    to: Baseline
    decision: Decision | None

    @property
    def label(self) -> str:
        return CHANGE_LABELS[self.change.kind]


def baseline_history(session: Session, project: Project, limit: int = 6) -> tuple[list[ChangeRow], int]:
    """최근 기준선 변경(새것부터)과 전체 변경 수."""
    total = session.scalar(select(func.count()).select_from(BaselineChange)
                           .where(BaselineChange.project_id == project.id)) or 0
    changes = session.scalars(
        select(BaselineChange).where(BaselineChange.project_id == project.id)
        .order_by(BaselineChange.id.desc()).limit(limit)
    )
    rows = [ChangeRow(c, session.get(Baseline, c.to_baseline_id),
                      session.get(Decision, c.decision_id) if c.decision_id else None) for c in changes]
    return rows, total


# ---------------------------------------------------------------- 한 화면 요약


@dataclass
class Dashboard:
    rows: list[CriterionRow]
    goal: dict[CriterionState, int]
    work: WorkProgress
    last_decision: DecisionSummary | None
    history: list[ChangeRow]
    history_total: int
    warnings: list[str] = field(default_factory=list)
    on_baseline_zero: bool = False  # 현재 기준선이 B0이면 E0에 측정값을 더해 미측정을 채울 수 있습니다

    @property
    def met(self) -> int:
        return self.goal[CriterionState.MET]


def build_dashboard(session: Session, project: Project) -> Dashboard:
    evaluations = evaluate_project(session, project)
    baseline = session.get(Baseline, project.current_baseline_id) if project.current_baseline_id else None
    history, total = baseline_history(session, project)
    return Dashboard(
        rows=[CriterionRow(e, latest_attempt(session, project, e.criterion)) for e in evaluations],
        goal=goal_summary(evaluations),
        work=work_progress(session, project),
        last_decision=last_decision(session, project),
        history=history,
        history_total=total,
        warnings=excluded_snapshot_runs(session, baseline) if baseline else [],
        on_baseline_zero=baseline is not None and baseline.short_id == BASELINE_ZERO_ID,
    )


def update_next_action(session: Session, project: Project, text: str) -> None:
    """다음 행동 한 줄. 줄바꿈은 공백으로 합칩니다. 비우면 '아직 정하지 않음'으로 돌아갑니다. 호출한 쪽이 커밋합니다."""
    if message := next_action_error(text):
        raise ValidationError({"next_action": message})
    project.next_action = one_line(text)
    session.flush()

