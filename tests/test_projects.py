"""2단계: 프로젝트, 성공 기준, 기준선 0 (여정 1)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
    Run,
)
from rcompass.services import ValidationError
from rcompass.services.aggregate import Aggregated, aggregate, format_aggregated
from rcompass.services.progress import CriterionState, evaluate_project, goal_summary
from rcompass.services.projects import CriterionInput, ProjectInput, create_project


def _input(**overrides) -> ProjectInput:
    values = dict(
        title="QA 평가 오답 줄이기",
        question="동일 평가 100문항에서 오답을 18→12개 이하로 줄일 수 있는가?",
        baseline_label="현행 프롬프트 v1",
        criteria=[
            CriterionInput(name="오답 수", metric_key="wrong_count", direction=Direction.LOWER, target=12,
                           unit="개", min_runs=3, baseline_values=[17, 18, 19]),
            CriterionInput(name="평균 응답 시간", metric_key="avg_latency_s", direction=Direction.LOWER, target=2.0,
                           unit="초"),
        ],
    )
    values.update(overrides)
    return ProjectInput(**values)


def test_create_project_builds_e0_b0_and_snapshot(session: Session) -> None:
    project = create_project(session, _input())
    session.commit()

    e0 = session.scalars(select(Experiment).where(Experiment.project_id == project.id)).one()
    assert (e0.short_id, e0.kind, e0.status) == ("E0", ExperimentKind.BASELINE_MEASUREMENT, ExperimentStatus.DONE)
    b0 = session.get(Baseline, project.current_baseline_id)
    assert (b0.short_id, b0.label, b0.source_experiment_id) == ("B0", "현행 프롬프트 v1", e0.id)
    change = session.scalars(select(BaselineChange)).one()
    assert (change.kind, change.to_baseline_id, change.from_baseline_id) == (BaselineChangeKind.INITIAL, b0.id, None)

    runs = session.scalars(select(Run).where(Run.experiment_id == e0.id).order_by(Run.id)).all()
    assert [r.short_id for r in runs] == ["R1", "R2", "R3"]
    snapshot = session.scalars(select(BaselineRun.run_id).where(BaselineRun.baseline_id == b0.id)).all()
    assert sorted(snapshot) == [r.id for r in runs]
    values = session.scalars(select(MetricResult.value).where(MetricResult.metric_key == "wrong_count")).all()
    assert sorted(values) == [17, 18, 19]


def test_criteria_get_sequential_ids(session: Session) -> None:
    project = create_project(session, _input())
    evaluations = evaluate_project(session, project)
    assert [e.criterion.short_id for e in evaluations] == ["C1", "C2"]


def test_states_with_and_without_baseline_values(session: Session) -> None:
    project = create_project(session, _input())
    by_key = {e.criterion.metric_key: e for e in evaluate_project(session, project)}
    assert by_key["wrong_count"].state is CriterionState.NOT_MET
    assert by_key["wrong_count"].display_value == "18.0 ± 1.0 (n=3)"
    assert by_key["avg_latency_s"].state is CriterionState.UNMEASURED
    assert by_key["avg_latency_s"].display_value == "—"


def test_only_required_input_creates_project_with_everything_unmeasured(session: Session) -> None:
    data = _input(criteria=[CriterionInput(name="오답 수", metric_key="wrong_count",
                                           direction=Direction.LOWER, target=12)])
    project = create_project(session, data)
    session.commit()
    e0 = session.scalars(select(Experiment)).one()
    assert e0.status is ExperimentStatus.PLANNED
    assert session.scalar(select(func.count()).select_from(Run)) == 0
    summary = goal_summary(evaluate_project(session, project))
    assert summary[CriterionState.UNMEASURED] == 1


def test_fewer_runs_than_required_is_insufficient(session: Session) -> None:
    data = _input(criteria=[CriterionInput(name="오답 수", metric_key="wrong_count", direction=Direction.LOWER,
                                           target=12, min_runs=3, baseline_values=[11, 10])])
    evaluation = evaluate_project(session, create_project(session, data))[0]
    assert evaluation.state is CriterionState.INSUFFICIENT


def test_met_when_aggregate_meets_target(session: Session) -> None:
    data = _input(criteria=[CriterionInput(name="정확도", metric_key="accuracy", direction=Direction.HIGHER,
                                           target=0.9, baseline_values=[0.91, 0.93])])
    evaluation = evaluate_project(session, create_project(session, data))[0]
    assert evaluation.state is CriterionState.MET


def test_criteria_with_different_run_counts(session: Session) -> None:
    data = _input(criteria=[
        CriterionInput(name="a", metric_key="a", direction=Direction.LOWER, target=1, baseline_values=[1, 2, 3]),
        CriterionInput(name="b", metric_key="b", direction=Direction.LOWER, target=1, baseline_values=[5]),
    ])
    by_key = {e.criterion.metric_key: e for e in evaluate_project(session, create_project(session, data))}
    assert by_key["a"].aggregated.n == 3
    assert by_key["b"].aggregated.n == 1


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"title": "  "}, "title"),
        ({"question": ""}, "question"),
        ({"baseline_label": ""}, "baseline_label"),
        ({"criteria": []}, "criteria"),
    ],
)
def test_required_fields(session: Session, overrides: dict, field: str) -> None:
    with pytest.raises(ValidationError) as error:
        create_project(session, _input(**overrides))
    assert field in error.value.errors


@pytest.mark.parametrize("key", ["", "오답", "1count", "has space"])
def test_metric_key_must_be_ascii_identifier(session: Session, key: str) -> None:
    data = _input(criteria=[CriterionInput(name="x", metric_key=key, direction=Direction.LOWER, target=1)])
    with pytest.raises(ValidationError) as error:
        create_project(session, data)
    assert "criteria-0-metric_key" in error.value.errors


def test_duplicate_metric_keys_rejected(session: Session) -> None:
    data = _input(criteria=[
        CriterionInput(name="a", metric_key="k", direction=Direction.LOWER, target=1),
        CriterionInput(name="b", metric_key="k", direction=Direction.LOWER, target=1),
    ])
    with pytest.raises(ValidationError) as error:
        create_project(session, data)
    assert "criteria-1-metric_key" in error.value.errors


# ---------------------------------------------------------------- 집계


def test_aggregate_methods() -> None:
    assert aggregate([17, 18, 19], Aggregate.MEAN, Direction.LOWER).value == 18
    assert aggregate([10, 11, 30], Aggregate.MEDIAN, Direction.LOWER).value == 11
    assert aggregate([10, 11, 12], Aggregate.WORST, Direction.LOWER).value == 12
    assert aggregate([0.8, 0.9], Aggregate.WORST, Direction.HIGHER).value == 0.8
    assert aggregate([], Aggregate.MEAN, Direction.LOWER) is None


@pytest.mark.parametrize(
    ("values", "method", "expected"),
    [
        ([18], Aggregate.MEAN, "18"),
        ([1.25], Aggregate.MEAN, "1.25"),
        ([11, 12, 10], Aggregate.MEAN, "11.0 ± 1.0 (n=3)"),
        ([1.82, 1.9], Aggregate.MEAN, "1.86 ± 0.06 (n=2)"),
        ([10, 11, 30], Aggregate.MEDIAN, "11.0 (중앙값, n=3)"),
        ([0.0004], Aggregate.MEAN, "0.0004"),
        ([0.0004, 0.0006], Aggregate.MEAN, "0.0005 ± 0.0001 (n=2)"),
    ],
)
def test_format_aggregated(values: list[float], method: Aggregate, expected: str) -> None:
    agg: Aggregated = aggregate(values, method, Direction.LOWER)
    assert format_aggregated(agg, method) == expected
