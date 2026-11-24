"""6단계: 결정과 기준선 전이 (계획서 4절 상태 표, 7절 시나리오 3~5)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from rcompass.models import (
    Baseline,
    BaselineChange,
    BaselineChangeKind,
    BaselineRun,
    Decision,
    DecisionType,
    Experiment,
    ExperimentOutcome,
    Project,
)
from rcompass.services import ValidationError
from rcompass.services.datasets import add_dataset_version
from rcompass.services.decisions import (
    adoption_gate,
    allowed_decisions,
    decide,
    decision_history,
    excluded_snapshot_runs,
    judgment,
    revoke_adoption,
    revoke_plan,
)
from rcompass.services.experiments import create_experiment, update_inputs, update_plan
from rcompass.services.problems import add_problem
from rcompass.services.progress import CriterionState, evaluate_project
from rcompass.services.runs import exclude_run, get_run, parse_results_table, record_results


def _baseline(session: Session, project: Project) -> Baseline:
    return session.get(Baseline, project.current_baseline_id)


def _wrong(project_state: list) -> tuple[CriterionState, str]:
    c1 = project_state[0]
    return c1.state, c1.display_value


@pytest.fixture
def lab(session: Session, project: Project):
    """P2 장문 지문 오답과 데이터 버전 DS1 v1.0."""
    add_problem(session, project, title="장문 지문 오답", why="18개 중 7개가 장문")
    version = add_dataset_version(session, project, dataset_name="eval-100", version_label="v1.0",
                                  external_uri="D:/data/eval100_v1.jsonl").version
    session.commit()
    return version


def _trial(session: Session, project: Project, version, *, title: str, values: list[float],
           complete: bool = True) -> Experiment:
    experiment = create_experiment(session, project, problem_short_id="P1", title=title,
                                   hypothesis="h", decision_rule="평균 ≤ 17이면 채택")
    if complete:
        update_inputs(session, experiment, dataset_version_ids=[version.id], code_ref="3f2a9e1",
                      config_text="configs/x.yaml")
    rows = "\n".join(f"seed={i}\t{v}" for i, v in enumerate(values, start=1))
    record_results(session, project, experiment, parse_results_table(f"label\twrong_count\n{rows}"),
                   dataset_version_id=version.id)
    session.flush()
    return experiment


# ---------------------------------------------------------------- 기각, 보류


def test_reject_keeps_baseline_and_records_decision(session: Session, project: Project, lab) -> None:
    e1 = _trial(session, project, lab, title="few-shot 예시 3개", values=[18, 19, 20])
    before = project.current_baseline_id
    decision = decide(session, project, e1, DecisionType.REJECT, reason="평균 오답 +1, 형식 오류 2건 증가")
    assert decision.short_id == "D1" and e1.outcome is ExperimentOutcome.REJECTED
    assert project.current_baseline_id == before
    assert _wrong(evaluate_project(session, project)) == (CriterionState.NOT_MET, "18.0 ± 1.0 (n=3)")
    assert session.scalars(select(BaselineChange).where(BaselineChange.decision_id == decision.id)).all() == []
    assert allowed_decisions(e1) == []
    with pytest.raises(ValidationError, match="새 실험"):
        decide(session, project, e1, DecisionType.ADOPT, reason="다시")


def test_hold_records_revisit_condition_then_can_be_decided_again(session: Session, project: Project, lab) -> None:
    e1 = _trial(session, project, lab, title="t", values=[16, 16, 16])
    hold = decide(session, project, e1, DecisionType.HOLD, reason="재측정 필요", revisit_condition="평가셋 v2가 나오면")
    assert e1.outcome is ExperimentOutcome.HELD and hold.revisit_condition == "평가셋 v2가 나오면"
    assert allowed_decisions(e1) == [DecisionType.ADOPT, DecisionType.REJECT]
    with pytest.raises(ValidationError):
        decide(session, project, e1, DecisionType.HOLD, reason="또")
    decide(session, project, e1, DecisionType.ADOPT, reason="v2에서도 확인")
    assert e1.outcome is ExperimentOutcome.ADOPTED


def test_reason_is_required(session: Session, project: Project, lab) -> None:
    e1 = _trial(session, project, lab, title="t", values=[18, 19, 20])
    with pytest.raises(ValidationError) as error:
        decide(session, project, e1, DecisionType.REJECT, reason="  ")
    assert "reason" in error.value.errors and e1.outcome is None


def test_reject_and_hold_allowed_with_missing_records(session: Session, project: Project, lab) -> None:
    e1 = _trial(session, project, lab, title="t", values=[18], complete=False)
    gate = adoption_gate(session, project, e1)
    assert {item.anchor for item in gate.record_gaps} == {"inputs"} and len(gate.record_gaps) == 3
    decide(session, project, e1, DecisionType.REJECT, reason="중단")
    assert e1.outcome is ExperimentOutcome.REJECTED


def test_baseline_measurement_is_never_decided(session: Session, project: Project) -> None:
    e0 = session.scalars(select(Experiment).where(Experiment.short_id == "E0")).one()
    assert allowed_decisions(e0) == []
    with pytest.raises(ValidationError, match="기준선 측정"):
        decide(session, project, e0, DecisionType.ADOPT, reason="x")


# ---------------------------------------------------------------- 채택과 재현성 관문


def test_gate_blocks_until_min_runs_and_records_are_filled(session: Session, project: Project, lab) -> None:
    """시나리오 4: 실행 2개로는 막히고(실행 2/3), 데이터 버전이 없어도 막힙니다."""
    e2 = create_experiment(session, project, problem_short_id="P1", title="문단 요약 후 답변", hypothesis="h",
                           decision_rule="r")
    record_results(session, project, e2, parse_results_table("label\twrong_count\nseed=1\t11\nseed=2\t12"),
                   dataset_version_id=lab.id)
    gate = adoption_gate(session, project, e2)
    labels = [item.label for item in gate.missing]
    assert "C1 오답 수: 실행 2/3" in labels and "입력 데이터 버전" in labels
    with pytest.raises(ValidationError, match="실행 2/3"):
        decide(session, project, e2, DecisionType.ADOPT, reason="목표 달성")
    assert e2.outcome is None and session.scalars(select(Decision)).all() == []

    record_results(session, project, e2, parse_results_table("label\twrong_count\nseed=3\t10"),
                   dataset_version_id=lab.id)
    update_inputs(session, e2, dataset_version_ids=[], code_ref="a1b2c3d", config_text="configs/summarize.yaml")
    assert [i.label for i in adoption_gate(session, project, e2).missing] == ["입력 데이터 버전"]
    update_inputs(session, e2, dataset_version_ids=[lab.id], code_ref="a1b2c3d", config_text="configs/summarize.yaml")
    assert adoption_gate(session, project, e2).ok


def test_gate_counts_only_included_runs(session: Session, project: Project, lab) -> None:
    """채택 전에 제외하면 10.5 (n=2)가 되고 근거 부족으로 채택이 막힙니다."""
    e2 = _trial(session, project, lab, title="t", values=[11, 12, 10])
    exclude_run(session, get_run(session, e2, "R2"), "로그 손상")
    assert not adoption_gate(session, project, e2).ok
    comparison = judgment(session, project, e2)[0]
    assert comparison.trial.n == 2 and comparison.trial.value == 10.5
    assert comparison.state is CriterionState.INSUFFICIENT


def test_adopt_creates_baseline_snapshot_and_moves_current(session: Session, project: Project, lab) -> None:
    b0 = _baseline(session, project)
    e2 = _trial(session, project, lab, title="문단 요약 후 답변", values=[11, 12, 10])
    decision = decide(session, project, e2, DecisionType.ADOPT, reason="목표 달성, 다른 유형 악화 없음",
                      baseline_label="문단 요약 v1")
    b1 = _baseline(session, project)
    assert (b1.short_id, b1.label, b1.parent_baseline_id, b1.source_experiment_id) == ("B1", "문단 요약 v1", b0.id, e2.id)
    assert e2.outcome is ExperimentOutcome.ADOPTED
    change = session.scalars(select(BaselineChange).where(BaselineChange.decision_id == decision.id)).one()
    assert (change.kind, change.from_baseline_id, change.to_baseline_id) == (BaselineChangeKind.ADOPT, b0.id, b1.id)
    snapshot = session.scalars(select(BaselineRun.run_id).where(BaselineRun.baseline_id == b1.id)).all()
    assert len(snapshot) == 3
    evaluations = evaluate_project(session, project)
    assert _wrong(evaluations) == (CriterionState.MET, "11.0 ± 1.0 (n=3)")
    assert evaluations[1].state is CriterionState.UNMEASURED


def test_default_baseline_label_is_experiment_title(session: Session, project: Project, lab) -> None:
    e2 = _trial(session, project, lab, title="문단 요약 후 답변", values=[11, 12, 10])
    decide(session, project, e2, DecisionType.ADOPT, reason="ok", baseline_label="  ")
    assert _baseline(session, project).label == "문단 요약 후 답변"


def test_snapshot_is_fixed_after_adoption(session: Session, project: Project, lab) -> None:
    """채택 뒤 실행을 더하거나 제외해도 기준선 수치는 그대로이고, 제외는 경고로 드러납니다."""
    e2 = _trial(session, project, lab, title="t", values=[11, 12, 10])
    decide(session, project, e2, DecisionType.ADOPT, reason="ok")
    record_results(session, project, e2, parse_results_table("label\twrong_count\nseed=4\t30"),
                   dataset_version_id=lab.id)
    exclude_run(session, get_run(session, e2, "R2"), "로그 손상")
    assert _wrong(evaluate_project(session, project)) == (CriterionState.MET, "11.0 ± 1.0 (n=3)")
    warnings = excluded_snapshot_runs(session, _baseline(session, project))
    assert len(warnings) == 1 and "E1/R2" in warnings[0] and "로그 손상" in warnings[0]


def test_criterion_not_measured_by_trial_warns(session: Session, project: Project, lab) -> None:
    e0 = session.scalars(select(Experiment).where(Experiment.short_id == "E0")).one()
    record_results(session, project, e0, parse_results_table("label\tavg_latency_s\nlat=1\t1.5"))
    e2 = _trial(session, project, lab, title="t", values=[11, 12, 10])
    gate = adoption_gate(session, project, e2)
    assert gate.ok and any("C2" in w and "미측정" in w for w in gate.warnings)


def test_gate_needs_some_criterion_result(session: Session, project: Project, lab) -> None:
    e2 = create_experiment(session, project, problem_short_id="P1", title="t", hypothesis="h", decision_rule="r")
    update_inputs(session, e2, dataset_version_ids=[lab.id], code_ref="a", config_text="c")
    record_results(session, project, e2, parse_results_table("label\tother_metric\nx\t1"))
    assert [i.label for i in adoption_gate(session, project, e2).missing] == ["성공 기준 지표의 결과"]


# ---------------------------------------------------------------- 채택 철회


def test_revoke_returns_to_parent_and_keeps_history(session: Session, project: Project, lab) -> None:
    """시나리오 5: D2 철회 → B0으로 복귀, C1 미달(18), 채택·철회 두 결정과 B1 기록이 남음."""
    b0 = _baseline(session, project)
    e1 = _trial(session, project, lab, title="few-shot", values=[18, 19, 20])
    decide(session, project, e1, DecisionType.REJECT, reason="+1")
    e2 = _trial(session, project, lab, title="문단 요약", values=[11, 12, 10])
    adopt = decide(session, project, e2, DecisionType.ADOPT, reason="목표 달성")
    b1 = _baseline(session, project)
    assert adopt.short_id == "D2"

    plan = revoke_plan(session, project, e2)
    assert plan.is_current and plan.default.id == b0.id and plan.warning is None
    revoke = revoke_adoption(session, project, e2, reason="평가셋 일부가 예시에 섞임")
    assert revoke.revokes_decision_id == adopt.id and e2.outcome is ExperimentOutcome.REVOKED
    assert project.current_baseline_id == b0.id
    assert _wrong(evaluate_project(session, project)) == (CriterionState.NOT_MET, "18.0 ± 1.0 (n=3)")
    assert session.get(Baseline, b1.id) is not None  # 기준선 기록은 남음
    history = decision_history(session, e2)
    assert [row.label for row in history] == ["채택", "채택 철회"]
    assert history[0].revoked_by.id == revoke.id and history[0].baseline.id == b1.id
    assert history[1].moved_to.id == b0.id and history[1].moved_from.id == b1.id
    change = session.scalars(select(BaselineChange).where(BaselineChange.decision_id == revoke.id)).one()
    assert (change.kind, change.from_baseline_id, change.to_baseline_id) == (BaselineChangeKind.REVOKE, b1.id, b0.id)
    assert allowed_decisions(e2) == [] and revoke_plan(session, project, e2) is None


def test_revoke_reason_required(session: Session, project: Project, lab) -> None:
    e2 = _trial(session, project, lab, title="t", values=[11, 12, 10])
    decide(session, project, e2, DecisionType.ADOPT, reason="ok")
    with pytest.raises(ValidationError) as error:
        revoke_adoption(session, project, e2, reason="")
    assert "revoke_reason" in error.value.errors and e2.outcome is ExperimentOutcome.ADOPTED


def test_only_adopted_experiments_can_be_revoked(session: Session, project: Project, lab) -> None:
    e1 = _trial(session, project, lab, title="t", values=[18, 19, 20])
    with pytest.raises(ValidationError, match="채택한 실험만"):
        revoke_adoption(session, project, e1, reason="x")


def test_revoking_a_baseline_under_another_requires_explicit_target(session: Session, project: Project, lab) -> None:
    """B2가 B1 위에 있을 때 E2(B1)를 철회하면 막지 않고 경고하며 복귀 대상을 직접 고르게 합니다."""
    b0 = _baseline(session, project)
    e2 = _trial(session, project, lab, title="a", values=[11, 12, 10])
    decide(session, project, e2, DecisionType.ADOPT, reason="ok")
    b1 = _baseline(session, project)
    e3 = _trial(session, project, lab, title="b", values=[9, 9, 9])
    assert e3.based_on_baseline_id == b1.id
    decide(session, project, e3, DecisionType.ADOPT, reason="ok")
    b2 = _baseline(session, project)

    plan = revoke_plan(session, project, e2)
    assert not plan.is_current and plan.default is None
    assert plan.warning.startswith("B2이(가) B1 위에 있습니다")
    assert [b.short_id for b in plan.targets] == ["B0", "B2"]
    with pytest.raises(ValidationError) as error:
        revoke_adoption(session, project, e2, reason="누수")
    assert "revoke_target" in error.value.errors
    with pytest.raises(ValidationError):
        revoke_adoption(session, project, e2, reason="누수", target_short_id="B1")  # 철회되는 기준선 자신

    revoke_adoption(session, project, e2, reason="누수", target_short_id="B2")  # 현재 기준선 유지
    assert project.current_baseline_id == b2.id
    # 이제 B2를 철회하면 부모 B1은 철회되었으므로 B0으로 돌아갑니다.
    plan = revoke_plan(session, project, e3)
    assert plan.default.id == b0.id and [b.short_id for b in plan.targets] == ["B0"]
    revoke_adoption(session, project, e3, reason="연쇄 철회")
    assert project.current_baseline_id == b0.id


def test_last_change_matches_current_baseline_cache(session: Session, project: Project, lab) -> None:
    e2 = _trial(session, project, lab, title="a", values=[11, 12, 10])
    decide(session, project, e2, DecisionType.ADOPT, reason="ok")
    revoke_adoption(session, project, e2, reason="x")
    last = session.scalars(select(BaselineChange).where(BaselineChange.project_id == project.id)
                           .order_by(BaselineChange.id.desc())).first()
    assert last.to_baseline_id == project.current_baseline_id


def test_decisions_cannot_be_edited_or_deleted(session: Session, project: Project, lab) -> None:
    e1 = _trial(session, project, lab, title="t", values=[18, 19, 20])
    decide(session, project, e1, DecisionType.REJECT, reason="x")
    session.commit()
    decision = session.scalars(select(Decision)).one()
    decision.reason = "몰래 고침"
    with pytest.raises(DatabaseError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------- 판정 패널


def test_judgment_compares_against_start_baseline(session: Session, project: Project, lab) -> None:
    """시나리오 3: 18.0 → 19.0 (+1.0)이고 범위가 겹친다는 경고."""
    e1 = _trial(session, project, lab, title="few-shot", values=[18, 19, 20])
    [c1] = judgment(session, project, e1)
    assert c1.baseline.short_id == "B0"
    assert c1.delta_display == "+1.0 (나빠짐)" and c1.overlaps
    e2 = _trial(session, project, lab, title="요약", values=[11, 12, 10])
    [c1] = judgment(session, project, e2)
    assert c1.delta_display == "−7.0 (좋아짐)" and not c1.overlaps and c1.state is CriterionState.MET


def test_plan_is_locked_after_final_decision(session: Session, project: Project, lab) -> None:
    e1 = _trial(session, project, lab, title="t", values=[18, 19, 20])
    decide(session, project, e1, DecisionType.REJECT, reason="x")
    with pytest.raises(ValidationError, match="판정한 뒤"):
        update_plan(session, e1, title="t", hypothesis="바뀐 가설", decision_rule="r", confirm_after_results=True)


def test_inputs_are_locked_after_adoption(session: Session, project: Project, lab) -> None:
    e2 = _trial(session, project, lab, title="t", values=[11, 12, 10])
    decide(session, project, e2, DecisionType.ADOPT, reason="ok")
    with pytest.raises(ValidationError, match="채택"):
        update_inputs(session, e2, dataset_version_ids=[], code_ref="other", config_text=None)


def test_revoked_start_baseline_is_replaced_by_current(session: Session, project: Project, lab) -> None:
    """E3을 B1 위에서 만든 뒤 B1이 철회되면, E3의 판정 패널은 철회된 B1 대신 현재 기준선 B0과 비교합니다."""
    e2 = _trial(session, project, lab, title="a", values=[11, 12, 10])
    decide(session, project, e2, DecisionType.ADOPT, reason="ok")
    e3 = _trial(session, project, lab, title="b", values=[13, 13, 13])
    revoke_adoption(session, project, e2, reason="누수")
    [c1] = judgment(session, project, e3)
    assert c1.replaced_start.short_id == "B1" and c1.baseline.short_id == "B0"
    assert c1.base.value == 18.0


def test_same_values_stay_inside_their_lane(session: Session, project: Project, lab) -> None:
    from rcompass.web.charts import LANE_Y, strip_chart

    e1 = _trial(session, project, lab, title="t", values=[0] * 10)
    [c1] = judgment(session, project, e1)
    chart = strip_chart(c1, "이 실험")
    trial_lane = chart.lanes[1]
    ys = [d.y for d in trial_lane.dots]
    assert max(ys) - min(ys) <= 28.0 and len(set(ys)) == 10
    assert all(abs(y - LANE_Y[1]) <= 14 for y in ys)
