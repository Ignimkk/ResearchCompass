"""3단계 서비스: 문제 트리 (여정 2)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from rcompass.models import Direction, ProblemStatus, Project
from rcompass.services import ValidationError
from rcompass.services.problems import (
    add_problem,
    change_status,
    parked_problems,
    problem_path,
    problem_tree,
    set_focus,
)
from rcompass.services.projects import CriterionInput, ProjectInput, create_project


def _chain(session: Session, project: Project, depth: int):
    parent = None
    created = []
    for level in range(1, depth + 1):
        parent = add_problem(session, project, title=f"깊이 {level}", why=f"이유 {level}",
                             parent_short_id=parent.short_id if parent else None)
        created.append(parent)
    return created


def test_root_and_child_problems_get_sequential_ids(session: Session, project: Project) -> None:
    p1 = add_problem(session, project, title="오답 유형 분석", why="개선 방향 결정")
    p2 = add_problem(session, project, title="장문 지문 오답", why="18개 중 7개가 장문", parent_short_id="P1")
    assert (p1.short_id, p1.parent_id) == ("P1", None)
    assert (p2.short_id, p2.parent_id) == ("P2", p1.id)


@pytest.mark.parametrize(("title", "why", "field"), [("", "이유", "title"), ("제목", "   ", "why")])
def test_title_and_reason_are_required(session: Session, project: Project, title: str, why: str, field: str) -> None:
    with pytest.raises(ValidationError) as error:
        add_problem(session, project, title=title, why=why)
    assert field in error.value.errors


def test_parent_must_belong_to_same_project(session: Session, project: Project) -> None:
    other = create_project(session, ProjectInput(
        title="다른 연구", question="q", baseline_label="b",
        criteria=[CriterionInput(name="x", metric_key="x", direction=Direction.LOWER, target=1)],
    ))
    foreign = add_problem(session, other, title="남의 문제", why="w")
    with pytest.raises(ValidationError) as error:
        add_problem(session, project, title="t", why="w", parent_short_id=foreign.short_id + "9")
    assert "parent" in error.value.errors
    # 같은 짧은 ID(P1)라도 다른 프로젝트의 문제와 섞이지 않습니다.
    mine = add_problem(session, project, title="내 문제", why="w")
    child = add_problem(session, project, title="하위", why="w", parent_short_id="P1")
    assert child.parent_id == mine.id != foreign.id


def test_path_at_depth_four(session: Session, project: Project) -> None:
    chain = _chain(session, project, 4)
    path = problem_path(session, chain[-1])
    assert [p.title for p in path] == ["깊이 1", "깊이 2", "깊이 3", "깊이 4"]


def test_tree_nesting_and_order(session: Session, project: Project) -> None:
    add_problem(session, project, title="A", why="w")
    add_problem(session, project, title="B", why="w")
    add_problem(session, project, title="A-1", why="w", parent_short_id="P1")
    add_problem(session, project, title="A-2", why="w", parent_short_id="P1")
    tree = problem_tree(session, project.id)
    assert [n.problem.title for n in tree] == ["A", "B"]
    assert [n.problem.title for n in tree[0].children] == ["A-1", "A-2"]
    assert tree[0].experiment_count == 0


def test_parked_ideas_and_resuming(session: Session, project: Project) -> None:
    idea = add_problem(session, project, title="모델 교체", why="더 큰 모델이면?",
                       status=ProblemStatus.PARKED, revisit_condition="프롬프트 개선이 한계에 도달하면")
    assert parked_problems(session, project.id) == [idea]
    change_status(session, project, idea, ProblemStatus.ACTIVE)
    assert parked_problems(session, project.id) == []
    assert idea.revisit_condition == "프롬프트 개선이 한계에 도달하면"  # 재개해도 조건 기록은 남김


def test_revisit_condition_can_be_cleared_or_replaced(session: Session, project: Project) -> None:
    idea = add_problem(session, project, title="모델 교체", why="w", status=ProblemStatus.PARKED,
                       revisit_condition="예전 조건")
    change_status(session, project, idea, ProblemStatus.PARKED, "새 조건")
    assert idea.revisit_condition == "새 조건"
    change_status(session, project, idea, ProblemStatus.PARKED, "  ")
    assert idea.revisit_condition is None


def test_focus_only_on_active_and_cleared_when_status_changes(session: Session, project: Project) -> None:
    active = add_problem(session, project, title="장문", why="w")
    parked = add_problem(session, project, title="아이디어", why="w", status=ProblemStatus.PARKED)
    with pytest.raises(ValidationError):
        set_focus(session, project, parked)
    set_focus(session, project, active)
    assert project.focus_problem_id == active.id
    change_status(session, project, active, ProblemStatus.RESOLVED)
    assert project.focus_problem_id is None


def test_clearing_focus(session: Session, project: Project) -> None:
    active = add_problem(session, project, title="장문", why="w")
    set_focus(session, project, active)
    set_focus(session, project, None)
    assert project.focus_problem_id is None
