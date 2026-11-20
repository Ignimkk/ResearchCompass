"""1단계 완료 기준: 모든 엔터티의 스키마, 추가만 가능한 표, 제약 조건."""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, delete, inspect, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rcompass.models import (
    APPEND_ONLY_TABLES,
    Base,
    Baseline,
    BaselineChange,
    BaselineChangeKind,
    BaselineRun,
    Decision,
    DecisionType,
    Diagram,
    DiagramBasis,
    DiagramKind,
    Direction,
    Experiment,
    ExperimentKind,
    FileObject,
    Problem,
    Project,
    Run,
    RunExclusion,
    StorageKind,
    SuccessCriterion,
)


def test_migration_matches_models(engine: Engine) -> None:
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_all_model_tables_exist(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= tables


# ---------------------------------------------------------------- 기본 그래프


@pytest.fixture
def graph(session: Session) -> dict:
    """프로젝트 → 문제 → 기준선 측정 실험 E0/실행 → B0 → 일반 실험과 결정."""
    project = Project(title="QA 평가 오답 줄이기", question="오답을 18→12개 이하로 줄일 수 있는가?")
    session.add(project)
    session.flush()
    problem = Problem(project_id=project.id, short_id="P1", title="장문 지문 오답", why="오답 18 중 7이 장문")
    e0 = Experiment(project_id=project.id, short_id="E0", kind=ExperimentKind.BASELINE_MEASUREMENT, title="기준선 측정")
    session.add_all([problem, e0])
    session.flush()
    r0 = Run(experiment_id=e0.id, short_id="R1", label="seed=1")
    b0 = Baseline(project_id=project.id, short_id="B0", label="현행 프롬프트 v1", source_experiment_id=e0.id)
    session.add_all([r0, b0])
    session.flush()
    change = BaselineChange(project_id=project.id, kind=BaselineChangeKind.INITIAL, to_baseline_id=b0.id)
    session.add(change)
    session.flush()
    snapshot = BaselineRun(baseline_id=b0.id, run_id=r0.id, added_by_change_id=change.id)
    e1 = Experiment(
        project_id=project.id,
        short_id="E1",
        problem_id=problem.id,
        based_on_baseline_id=b0.id,
        title="few-shot 예시 3개",
        hypothesis="예시를 넣으면 장문 오답이 준다",
        decision_rule="평균 ≤ 17이면 채택",
    )
    session.add_all([snapshot, e1])
    session.flush()
    decision = Decision(project_id=project.id, short_id="D1", experiment_id=e1.id, type=DecisionType.REJECT, reason="오답 +1")
    exclusion = RunExclusion(run_id=r0.id, reason="로그 손상")
    session.add_all([decision, exclusion])
    session.commit()
    return {
        "project": project, "problem": problem, "e0": e0, "e1": e1, "run": r0,
        "baseline": b0, "change": change, "decision": decision, "exclusion": exclusion,
    }


APPEND_ONLY_CASES = [
    ("decisions", "decision", "reason", "고친 이유"),
    ("baselines", "baseline", "label", "고친 이름"),
    ("baseline_changes", "change", "kind", "snapshot_update"),
    ("run_exclusions", "exclusion", "reason", "고친 이유"),
]


def test_append_only_tables_cover_all_cases() -> None:
    assert {case[0] for case in APPEND_ONLY_CASES} | {"baseline_runs"} == set(APPEND_ONLY_TABLES)


@pytest.mark.parametrize(("table", "key", "column", "value"), APPEND_ONLY_CASES)
def test_append_only_rows_cannot_be_updated_or_deleted(
    session: Session, graph: dict, table: str, key: str, column: str, value: str
) -> None:
    model = type(graph[key])
    row_id = graph[key].id
    with pytest.raises(IntegrityError, match="추가만"):
        session.execute(update(model).where(model.id == row_id).values({column: value}))
    session.rollback()
    with pytest.raises(IntegrityError, match="추가만"):
        session.execute(delete(model).where(model.id == row_id))
    session.rollback()


def test_baseline_snapshot_is_append_only(session: Session, graph: dict) -> None:
    with pytest.raises(IntegrityError, match="추가만"):
        session.execute(delete(BaselineRun).where(BaselineRun.baseline_id == graph["baseline"].id))
    session.rollback()


def test_append_only_tables_still_accept_inserts(session: Session, graph: dict) -> None:
    session.add(
        Decision(
            project_id=graph["project"].id,
            short_id="D2",
            experiment_id=graph["e1"].id,
            type=DecisionType.HOLD,
            reason="재검토",
        )
    )
    session.commit()


def test_mutable_tables_can_be_updated(session: Session, graph: dict) -> None:
    session.execute(update(Project).where(Project.id == graph["project"].id).values(next_action="응답 시간 측정"))
    session.commit()


# ---------------------------------------------------------------- 제약 조건


def _assert_rejected(session: Session, row: Base) -> None:
    session.add(row)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_problem_requires_reason(session: Session, graph: dict) -> None:
    _assert_rejected(session, Problem(project_id=graph["project"].id, short_id="P9", title="t", why="   "))


@pytest.mark.parametrize("missing", ["hypothesis", "decision_rule", "problem_id"])
def test_trial_requires_problem_hypothesis_and_rule(session: Session, graph: dict, missing: str) -> None:
    values = dict(
        project_id=graph["project"].id,
        short_id="E9",
        problem_id=graph["problem"].id,
        title="t",
        hypothesis="h",
        decision_rule="r",
    )
    values[missing] = None
    _assert_rejected(session, Experiment(**values))


def test_baseline_measurement_needs_no_problem_or_hypothesis(session: Session, graph: dict) -> None:
    session.add(
        Experiment(project_id=graph["project"].id, short_id="E8", kind=ExperimentKind.BASELINE_MEASUREMENT, title="재측정")
    )
    session.commit()


def test_revoke_requires_target_and_only_revoke_has_target(session: Session, graph: dict) -> None:
    base = dict(project_id=graph["project"].id, experiment_id=graph["e1"].id, reason="r")
    _assert_rejected(session, Decision(short_id="D7", type=DecisionType.REVOKE, **base))
    _assert_rejected(
        session, Decision(short_id="D8", type=DecisionType.REJECT, revokes_decision_id=graph["decision"].id, **base)
    )


def test_adopt_change_requires_decision(session: Session, graph: dict) -> None:
    _assert_rejected(
        session,
        BaselineChange(project_id=graph["project"].id, kind=BaselineChangeKind.ADOPT, to_baseline_id=graph["baseline"].id),
    )


def test_run_exclusion_requires_reason_and_is_unique(session: Session, graph: dict) -> None:
    _assert_rejected(session, RunExclusion(run_id=graph["run"].id, reason="again"))


def test_min_runs_must_be_positive(session: Session, graph: dict) -> None:
    _assert_rejected(
        session,
        SuccessCriterion(
            project_id=graph["project"].id, short_id="C1", name="오답 수", metric_key="wrong_count",
            direction=Direction.LOWER, target=12, min_runs=0,
        ),
    )


def test_code_backed_diagram_requires_full_commit(session: Session, graph: dict) -> None:
    html = FileObject(project_id=graph["project"].id, short_id="F1", storage=StorageKind.ATTACHED, rel_path="files/x", sha256="0" * 64)
    session.add(html)
    session.flush()
    _assert_rejected(
        session,
        Diagram(
            project_id=graph["project"].id, short_id="G1", kind=DiagramKind.RESEARCH_REPO,
            basis=DiagramBasis.CODE_BACKED, title="qa-bot 구조", file_id=html.id, commit_sha="a1b2c3d",
        ),
    )


def test_attached_file_requires_path_and_hash(session: Session, graph: dict) -> None:
    _assert_rejected(session, FileObject(project_id=graph["project"].id, short_id="F2", storage=StorageKind.ATTACHED))


def test_enum_check_rejects_unknown_value(session: Session, graph: dict) -> None:
    with pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE problems SET status = 'someday' WHERE id = :id"), {"id": graph["problem"].id}
        )
    session.rollback()


def test_foreign_keys_are_enforced(session: Session) -> None:
    _assert_rejected(session, Problem(project_id=999, short_id="P1", title="t", why="w"))
