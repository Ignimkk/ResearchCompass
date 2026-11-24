"""성공 기준 판정과 목표 달성 요약 (여정 5).

목표 달성은 '현재 기준선'의 스냅샷 실행만으로 판정합니다. 작업 진행(실험 수 등)과 섞지 않습니다.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.models import Baseline, BaselineRun, MetricResult, Project, SuccessCriterion
from rcompass.services.aggregate import Aggregated, aggregate, format_aggregated, meets_target
from rcompass.services.projects import get_criteria


class CriterionState(str, enum.Enum):
    MET = "met"
    NOT_MET = "not_met"
    INSUFFICIENT = "insufficient"  # 실행 수가 min_runs보다 적음
    UNMEASURED = "unmeasured"


STATE_LABELS = {
    CriterionState.MET: "달성",
    CriterionState.NOT_MET: "미달",
    CriterionState.INSUFFICIENT: "근거 부족",
    CriterionState.UNMEASURED: "미측정",
}


@dataclass(frozen=True)
class CriterionEvaluation:
    criterion: SuccessCriterion
    baseline: Baseline | None
    aggregated: Aggregated | None
    state: CriterionState

    @property
    def state_label(self) -> str:
        return STATE_LABELS[self.state]

    @property
    def display_value(self) -> str:
        if self.aggregated is None:
            return "—"
        return format_aggregated(self.aggregated, self.criterion.aggregate)


def criterion_run_values(session: Session, run_ids, criterion: SuccessCriterion) -> dict[int, float]:
    """실행마다 이 기준의 지표 값 하나(같은 실행에 여러 번 기록했다면 마지막 기록).

    ``run_ids``는 id 목록이나 id를 고르는 select입니다. 기준에 평가 데이터 버전이 있으면 그 버전으로 잰 값만 셉니다.
    """
    query = (
        select(MetricResult.run_id, MetricResult.value)
        .where(MetricResult.run_id.in_(run_ids), MetricResult.metric_key == criterion.metric_key)
        .order_by(MetricResult.run_id, MetricResult.id)
    )
    if criterion.eval_dataset_version_id is not None:
        query = query.where(MetricResult.dataset_version_id == criterion.eval_dataset_version_id)
    per_run: dict[int, float] = {}
    for run_id, value in session.execute(query):
        per_run[run_id] = value
    return per_run


def baseline_values(session: Session, baseline_id: int, criterion: SuccessCriterion) -> list[float]:
    """기준선 스냅샷 실행에서 이 기준의 지표 값을 실행마다 하나씩 모읍니다."""
    snapshot = select(BaselineRun.run_id).where(BaselineRun.baseline_id == baseline_id)
    return list(criterion_run_values(session, snapshot, criterion).values())


def state_of(agg: Aggregated | None, criterion: SuccessCriterion) -> CriterionState:
    if agg is None:
        return CriterionState.UNMEASURED
    if agg.n < criterion.min_runs:
        return CriterionState.INSUFFICIENT
    if meets_target(agg.value, criterion.target, criterion.direction):
        return CriterionState.MET
    return CriterionState.NOT_MET


def evaluate(session: Session, criterion: SuccessCriterion, baseline: Baseline | None) -> CriterionEvaluation:
    values = baseline_values(session, baseline.id, criterion) if baseline else []
    agg = aggregate(values, criterion.aggregate, criterion.direction)
    return CriterionEvaluation(criterion=criterion, baseline=baseline, aggregated=agg, state=state_of(agg, criterion))


def evaluate_project(session: Session, project: Project) -> list[CriterionEvaluation]:
    baseline = session.get(Baseline, project.current_baseline_id) if project.current_baseline_id else None
    return [evaluate(session, criterion, baseline) for criterion in get_criteria(session, project.id)]


def goal_summary(evaluations: list[CriterionEvaluation]) -> dict[CriterionState, int]:
    counts = {state: 0 for state in CriterionState}
    for evaluation in evaluations:
        counts[evaluation.state] += 1
    return counts
