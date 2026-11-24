"""4단계 서비스: 실험, 실행, 결과 수치 (여정 3)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.models import (
    Aggregate,
    Baseline,
    BaselineChange,
    BaselineChangeKind,
    BaselineRun,
    Experiment,
    ExperimentStatus,
    MetricResult,
    Project,
)
from rcompass.services import ValidationError
from rcompass.services.datasets import add_dataset_version, list_versions
from rcompass.services.experiments import create_experiment, input_versions, update_inputs, update_plan
from rcompass.services.problems import add_problem
from rcompass.services.progress import CriterionState, evaluate_project
from rcompass.services.runs import exclude_run, experiment_results, get_run, parse_results_table, record_results


@pytest.fixture
def trial(session: Session, project: Project) -> Experiment:
    add_problem(session, project, title="장문 지문 오답", why="18개 중 7개가 장문")
    experiment = create_experiment(session, project, problem_short_id="P1", title="few-shot 예시 3개",
                                   hypothesis="예시를 넣으면 장문 오답이 준다", decision_rule="평균 ≤ 17이면 채택")
    session.commit()
    return experiment


def _e0(session: Session, project: Project) -> Experiment:
    baseline = session.get(Baseline, project.current_baseline_id)
    return session.get(Experiment, baseline.source_experiment_id)


# ---------------------------------------------------------------- 실험


def test_trial_starts_from_current_baseline_and_gets_e1(trial: Experiment, project: Project) -> None:
    assert trial.short_id == "E1"  # E0은 기준선 측정 실험
    assert trial.based_on_baseline_id == project.current_baseline_id
    assert trial.status is ExperimentStatus.PLANNED


@pytest.mark.parametrize("missing", ["title", "hypothesis", "decision_rule"])
def test_hypothesis_and_rule_required(session: Session, project: Project, missing: str) -> None:
    add_problem(session, project, title="p", why="w")
    values = {"title": "t", "hypothesis": "h", "decision_rule": "r", missing: "  "}
    with pytest.raises(ValidationError) as error:
        create_experiment(session, project, problem_short_id="P1", **values)
    assert missing in error.value.errors


def test_experiment_needs_existing_problem(session: Session, project: Project) -> None:
    with pytest.raises(ValidationError) as error:
        create_experiment(session, project, problem_short_id="P9", title="t", hypothesis="h", decision_rule="r")
    assert "problem" in error.value.errors


def test_plan_edit_before_results_is_recorded_without_confirmation(session: Session, trial: Experiment) -> None:
    revisions = update_plan(session, trial, title="few-shot 3개", hypothesis="새 가설", decision_rule="평균 ≤ 17이면 채택")
    assert [(r.field, r.old_value, r.new_value) for r in revisions] == [
        ("hypothesis", "예시를 넣으면 장문 오답이 준다", "새 가설")]
    assert trial.title == "few-shot 3개"


def test_plan_edit_after_results_requires_confirmation(session: Session, project: Project, trial: Experiment) -> None:
    record_results(session, project, trial, parse_results_table("label\twrong_count\nseed=1\t19"))
    with pytest.raises(ValidationError) as error:
        update_plan(session, trial, title=trial.title, hypothesis=trial.hypothesis, decision_rule="평균 ≤ 19이면 채택")
    assert "confirm" in error.value.errors
    update_plan(session, trial, title=trial.title, hypothesis=trial.hypothesis, decision_rule="평균 ≤ 19이면 채택",
                confirm_after_results=True)
    assert trial.decision_rule == "평균 ≤ 19이면 채택"


def test_inputs_and_dataset_versions(session: Session, project: Project, trial: Experiment) -> None:
    view = add_dataset_version(session, project, dataset_name="eval-100", version_label="v1.0",
                               external_uri="D:/data/eval100_v1.jsonl", source="내부 QA 수집")
    update_inputs(session, trial, dataset_version_ids=[view.version.id, view.version.id],
                  code_ref="3f2a9c1", config_text="shots: 3")
    assert [v.label for v in input_versions(session, trial)] == ["DS1 eval-100 v1.0"]
    assert (trial.code_ref, trial.config_text) == ("3f2a9c1", "shots: 3")
    update_inputs(session, trial, dataset_version_ids=[], code_ref="", config_text="")
    assert input_versions(session, trial) == [] and trial.code_ref is None


def test_dataset_version_rules(session: Session, project: Project) -> None:
    add_dataset_version(session, project, dataset_name="eval-100", version_label="v1.0", external_uri="a")
    second = add_dataset_version(session, project, dataset_name="eval-100", version_label="v1.1", external_uri="b")
    assert second.dataset.short_id == "DS1"  # 같은 이름이면 같은 데이터셋의 새 버전
    with pytest.raises(ValidationError) as error:
        add_dataset_version(session, project, dataset_name="eval-100", version_label="v1.0", external_uri="c")
    assert "version_label" in error.value.errors
    with pytest.raises(ValidationError) as error:
        add_dataset_version(session, project, dataset_name="x", version_label="v1", external_uri="", sha256="zz")
    assert {"external_uri", "sha256"} <= set(error.value.errors)
    assert len(list_versions(session, project.id)) == 2


# ---------------------------------------------------------------- 결과 표


@pytest.mark.parametrize(
    "text",
    [
        "label\twrong_count\tavg_latency_s\nseed=1\t19\t1.8\nseed=2\t18\t1.9",
        "label,wrong_count,avg_latency_s\nseed=1,19,1.8\nseed=2,18,1.9",
        "label  wrong_count  avg_latency_s\nseed=1  19  1.8\n\nseed=2  18  1.9\n",
    ],
)
def test_parse_table_formats(text: str) -> None:
    table = parse_results_table(text)
    assert table.metrics == ["wrong_count", "avg_latency_s"]
    assert [(r.label, r.values) for r in table.rows] == [
        ("seed=1", {"wrong_count": 19, "avg_latency_s": 1.8}),
        ("seed=2", {"wrong_count": 18, "avg_latency_s": 1.9}),
    ]


def test_parse_table_without_label_column_and_blank_cells() -> None:
    table = parse_results_table("wrong_count,avg_latency_s\n19,\n18,1.9")
    assert [r.label for r in table.rows] == [None, None]
    assert table.rows[0].values == {"wrong_count": 19}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("label\twrong_count", "첫 줄에 지표 이름"),
        ("label\t오답\nseed=1\t1", "지표 이름으로 쓸 수 없습니다"),
        ("label\tx\nseed=1\tabc", "2번째 줄: 'abc'은(는) 숫자가 아닙니다"),
        ("label\tx\nseed=1\t1\t2", "칸이 3개입니다"),
        ("label\tx\na\t1\na\t2", "두 번 있습니다"),
        ("label\tx\na\tnan", "기록할 수 없는 값"),
    ],
)
def test_parse_table_errors_point_to_lines(text: str, message: str) -> None:
    with pytest.raises(ValidationError) as error:
        parse_results_table(text)
    assert message in error.value.errors["results"]


def test_record_creates_runs_links_criteria_and_aggregates(session: Session, project: Project,
                                                           trial: Experiment) -> None:
    table = parse_results_table("label\twrong_count\tf1\nseed=1\t18\t0.71\nseed=2\t19\t0.70\nseed=3\t20\t0.69")
    outcome = record_results(session, project, trial, table)
    assert [r.short_id for r in outcome.created] == ["R1", "R2", "R3"]
    assert trial.status is ExperimentStatus.DONE
    linked = session.scalars(select(MetricResult).where(MetricResult.metric_key == "wrong_count",
                                                       MetricResult.criterion_id.is_not(None))).all()
    assert len([m for m in linked if m.run_id in {r.id for r in outcome.created}]) == 3

    view = experiment_results(session, trial)
    by_key = {m.key: m for m in view.metrics}
    assert list(by_key) == ["wrong_count", "f1"]  # 성공 기준 지표 먼저
    assert by_key["wrong_count"].display == "19.0 ± 1.0 (n=3)"
    assert by_key["wrong_count"].range_display == "18 ~ 20"


def test_same_label_updates_value_instead_of_new_run(session: Session, project: Project, trial: Experiment) -> None:
    record_results(session, project, trial, parse_results_table("label\twrong_count\nseed=1\t19"))
    outcome = record_results(session, project, trial, parse_results_table("label\twrong_count\nseed=1\t18\nseed=2\t17"))
    assert [r.label for r in outcome.created] == ["seed=2"]
    view = experiment_results(session, trial)
    assert [row.values["wrong_count"] for row in view.rows] == [18, 17]


def test_single_run_shows_plain_value(session: Session, project: Project, trial: Experiment) -> None:
    record_results(session, project, trial, parse_results_table("wrong_count\n19"))
    view = experiment_results(session, trial)
    assert view.rows[0].run.label == "실행 1"
    assert view.metrics[0].display == "19" and view.metrics[0].range_display == ""


def test_exclusion_removes_run_from_aggregate(session: Session, project: Project, trial: Experiment) -> None:
    record_results(session, project, trial, parse_results_table("label\twrong_count\na\t18\nb\t19\nc\t20"))
    with pytest.raises(ValidationError):
        exclude_run(session, get_run(session, trial, "R3"), " ")
    exclude_run(session, get_run(session, trial, "R3"), "로그 손상")
    view = experiment_results(session, trial)
    assert view.included_count == 2
    assert view.metrics[0].display == "18.5 ± 0.7 (n=2)"
    with pytest.raises(ValidationError):
        exclude_run(session, get_run(session, trial, "R3"), "다시")


def test_aggregate_uses_criterion_method(session: Session, project: Project, trial: Experiment) -> None:
    from rcompass.models import SuccessCriterion

    criterion = session.scalars(select(SuccessCriterion).where(SuccessCriterion.metric_key == "wrong_count")).one()
    criterion.aggregate = Aggregate.WORST
    record_results(session, project, trial, parse_results_table("label\twrong_count\na\t11\nb\t12\nc\t10"))
    assert experiment_results(session, trial).metrics[0].display == "12.0 (최악값, n=3)"


# ---------------------------------------------------------------- 기준선 근거 보호와 B0 스냅샷


def test_baseline_runs_are_locked(session: Session, project: Project) -> None:
    e0 = _e0(session, project)
    with pytest.raises(ValidationError) as error:
        record_results(session, project, e0, parse_results_table("label\twrong_count\n실행 1\t16"))
    assert "기준선 B0의 근거라" in error.value.errors["results"]
    # 같은 값을 다시 적는 것은 허용(변경 없음)
    record_results(session, project, e0, parse_results_table("label\twrong_count\n실행 1\t17"))


def test_new_e0_runs_extend_b0_snapshot_and_change_state(session: Session, project: Project) -> None:
    e0 = _e0(session, project)
    outcome = record_results(session, project, e0, parse_results_table("label\twrong_count\tavg_latency_s\nseed=4\t18\t2.4"))
    assert outcome.snapshot_baseline.short_id == "B0"
    change = session.scalars(select(BaselineChange).where(
        BaselineChange.kind == BaselineChangeKind.SNAPSHOT_UPDATE)).one()
    assert change.from_baseline_id == change.to_baseline_id == project.current_baseline_id
    assert session.scalars(select(BaselineRun).where(BaselineRun.added_by_change_id == change.id)).one()
    by_key = {e.criterion.metric_key: e for e in evaluate_project(session, project)}
    assert by_key["avg_latency_s"].state is CriterionState.NOT_MET  # 미측정 → 미달(2.4 > 2.0)
    assert by_key["wrong_count"].aggregated.n == 4


def test_e0_runs_do_not_touch_b0_when_not_current(session: Session, project: Project, trial: Experiment) -> None:
    from rcompass.models import DecisionType
    from rcompass.services.decisions import decide

    version = add_dataset_version(session, project, dataset_name="eval", version_label="v1", external_uri="D:/e").version
    update_inputs(session, trial, dataset_version_ids=[version.id], code_ref="a1b2c3d", config_text="c.yaml")
    record_results(session, project, trial, parse_results_table("label\twrong_count\na\t11\nb\t12\nc\t10"))
    decide(session, project, trial, DecisionType.ADOPT, reason="목표 달성")  # 현재 기준선이 B1로 옮겨 감
    e0 = _e0_by_id(session, project)
    outcome = record_results(session, project, e0, parse_results_table("label\twrong_count\nseed=4\t18"))
    assert outcome.snapshot_baseline is None and "현재 기준선이 아니어서" in outcome.warnings[0]


def _e0_by_id(session: Session, project: Project) -> Experiment:
    return session.scalars(select(Experiment).where(Experiment.project_id == project.id,
                                                    Experiment.short_id == "E0")).one()


def test_locked_run_metadata_is_not_rewritten(session: Session, project: Project) -> None:
    view = add_dataset_version(session, project, dataset_name="eval-100", version_label="v2", external_uri="p")
    e0 = _e0(session, project)
    record_results(session, project, e0, parse_results_table("label\twrong_count\n실행 1\t17\nseed=4\t18"),
                   dataset_version_id=view.version.id)
    run1 = get_run(session, e0, "R1")
    metric = session.scalars(select(MetricResult).where(MetricResult.run_id == run1.id)).one()
    assert metric.dataset_version_id is None  # 기준선 근거의 데이터 버전은 그대로


def test_auto_labels_never_collide(session: Session, project: Project, trial: Experiment) -> None:
    record_results(session, project, trial, parse_results_table("label\twrong_count\n실행 3\t1"))
    record_results(session, project, trial, parse_results_table("wrong_count\n2"))
    record_results(session, project, trial, parse_results_table("wrong_count\n3"))
    labels = [row.run.label for row in experiment_results(session, trial).rows]
    assert labels == ["실행 3", "실행 2", "실행 3 (R3)"]


def test_duplicate_run_label_rejected_by_database(session: Session, trial: Experiment) -> None:
    from sqlalchemy.exc import IntegrityError

    from rcompass.models import Run

    session.add_all([Run(experiment_id=trial.id, short_id="R1", label="a"),
                     Run(experiment_id=trial.id, short_id="R2", label="a")])
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_blank_values_do_not_mark_experiment_done(session: Session, project: Project, trial: Experiment) -> None:
    record_results(session, project, trial, parse_results_table("label\twrong_count\nseed=1\t-"))
    assert trial.status is ExperimentStatus.PLANNED


def test_run_cap_counts_existing_runs(session: Session, project: Project, trial: Experiment) -> None:
    rows = "\n".join(["1"] * 60)
    record_results(session, project, trial, parse_results_table("wrong_count\n" + rows))
    with pytest.raises(ValidationError) as error:
        record_results(session, project, trial, parse_results_table("wrong_count\n" + rows))
    assert "100개까지" in error.value.errors["results"]


def test_trial_runs_never_join_snapshots(session: Session, project: Project, trial: Experiment) -> None:
    outcome = record_results(session, project, trial, parse_results_table("label\twrong_count\na\t11"))
    assert outcome.snapshot_baseline is None
    assert experiment_results(session, trial).rows[0].baselines == []
