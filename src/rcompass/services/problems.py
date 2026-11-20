"""문제 트리 (여정 2: 하위 문제를 상위 문제에 연결하고, 푸는 이유를 한 줄 남김)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rcompass.ids import next_short_id
from rcompass.models import Experiment, ExperimentKind, ExperimentOutcome, Problem, ProblemStatus, Project
from rcompass.services import ValidationError

STATUS_LABELS = {
    ProblemStatus.ACTIVE: "지금 조사",
    ProblemStatus.PARKED: "나중에 검토",
    ProblemStatus.RESOLVED: "해결",
    ProblemStatus.DROPPED: "중단",
}
MAX_DEPTH = 50  # 경로 계산 안전장치(순환 참조 방지)


@dataclass
class ProblemNode:
    problem: Problem
    children: list[ProblemNode] = field(default_factory=list)
    outcomes: Counter = field(default_factory=Counter)  # ExperimentOutcome → 개수 (outcome 없는 실험은 None)

    @property
    def experiment_count(self) -> int:
        return sum(self.outcomes.values())


def _blank(text: str | None) -> bool:
    return text is None or not text.strip()


def get_problem(session: Session, project_id: int, short_id: str) -> Problem | None:
    return session.scalars(
        select(Problem).where(Problem.project_id == project_id, Problem.short_id == short_id)
    ).one_or_none()


def add_problem(
    session: Session,
    project: Project,
    *,
    title: str,
    why: str,
    parent_short_id: str | None = None,
    status: ProblemStatus = ProblemStatus.ACTIVE,
    revisit_condition: str | None = None,
) -> Problem:
    """하위 문제를 추가합니다. ``parent_short_id``가 없으면 연구 질문 바로 아래에 붙습니다. 호출한 쪽이 커밋합니다."""
    errors: dict[str, str] = {}
    if _blank(title):
        errors["title"] = "문제 제목을 입력하세요."
    if _blank(why):
        errors["why"] = "이 문제를 푸는 이유를 한 줄 적어 주세요. 나중에 원래 질문과의 연결을 잃지 않게 해 줍니다."
    parent = None
    if parent_short_id:
        parent = get_problem(session, project.id, parent_short_id)
        if parent is None:
            errors["parent"] = "상위 문제를 찾을 수 없습니다."
    if errors:
        raise ValidationError(errors)

    problem = Problem(
        project_id=project.id,
        short_id=next_short_id(session, project.id, "P"),
        parent_id=parent.id if parent else None,
        title=title.strip(),
        why=why.strip(),
        status=status,
        revisit_condition=(revisit_condition or "").strip() or None,
    )
    session.add(problem)
    session.flush()
    return problem


def problem_path(session: Session, problem: Problem) -> list[Problem]:
    """연구 질문 바로 아래 문제부터 ``problem``까지의 경로."""
    path = [problem]
    seen = {problem.id}
    current = problem
    while current.parent_id is not None and len(path) < MAX_DEPTH:
        parent = session.get(Problem, current.parent_id)
        if parent is None or parent.id in seen:
            break
        path.append(parent)
        seen.add(parent.id)
        current = parent
    return list(reversed(path))


def _outcome_counts(session: Session, project_id: int) -> dict[int, Counter]:
    rows = session.execute(
        select(Experiment.problem_id, Experiment.outcome, func.count())
        .where(Experiment.project_id == project_id, Experiment.kind == ExperimentKind.TRIAL)
        .group_by(Experiment.problem_id, Experiment.outcome)
    )
    counts: dict[int, Counter] = {}
    for problem_id, outcome, count in rows:
        counts.setdefault(problem_id, Counter())[outcome] += count
    return counts


def problem_tree(session: Session, project_id: int) -> list[ProblemNode]:
    """연구 질문 바로 아래 문제들(뿌리)과 그 하위 트리. 같은 단계에서는 만든 순서대로."""
    problems = session.scalars(
        select(Problem).where(Problem.project_id == project_id).order_by(Problem.id)
    ).all()
    counts = _outcome_counts(session, project_id)
    nodes = {p.id: ProblemNode(problem=p, outcomes=counts.get(p.id, Counter())) for p in problems}
    roots: list[ProblemNode] = []
    for problem in problems:
        node = nodes[problem.id]
        parent = nodes.get(problem.parent_id) if problem.parent_id is not None else None
        (parent.children if parent else roots).append(node)
    return roots


def parked_problems(session: Session, project_id: int) -> list[Problem]:
    return list(
        session.scalars(
            select(Problem)
            .where(Problem.project_id == project_id, Problem.status == ProblemStatus.PARKED)
            .order_by(Problem.id)
        )
    )


def child_problems(session: Session, problem: Problem) -> list[Problem]:
    return list(session.scalars(select(Problem).where(Problem.parent_id == problem.id).order_by(Problem.id)))


def change_status(
    session: Session,
    project: Project,
    problem: Problem,
    status: ProblemStatus,
    revisit_condition: str | None = None,
) -> None:
    """상태를 바꿉니다. 집중 문제가 더는 '지금 조사'가 아니면 집중 지정을 해제합니다.

    '나중에 검토'로 바꿀 때는 재개 조건을 입력값으로 덮어씁니다(빈 값이면 지움). 다른 상태로 바꿀 때는
    재개 조건 기록을 그대로 둡니다.
    """
    problem.status = status
    if status is ProblemStatus.PARKED:
        problem.revisit_condition = (revisit_condition or "").strip() or None
    if status is not ProblemStatus.ACTIVE and project.focus_problem_id == problem.id:
        project.focus_problem_id = None
    session.flush()


def inactive_ancestors(path: list[Problem]) -> list[Problem]:
    """경로의 상위 문제 중 '지금 조사'가 아닌 것. 집중 문제가 중단·해결된 가지 아래에 있는지 알려줄 때 씁니다."""
    return [p for p in path[:-1] if p.status is not ProblemStatus.ACTIVE]


def set_focus(session: Session, project: Project, problem: Problem | None) -> None:
    if problem is not None and problem.status is not ProblemStatus.ACTIVE:
        raise ValidationError({"focus": "'지금 조사' 상태인 문제만 집중 문제로 지정할 수 있습니다."})
    project.focus_problem_id = problem.id if problem else None
    session.flush()


OUTCOME_SYMBOLS = {
    ExperimentOutcome.ADOPTED: ("✔", "채택"),
    ExperimentOutcome.REJECTED: ("✘", "기각"),
    ExperimentOutcome.HELD: ("⏸", "보류"),
    ExperimentOutcome.REVOKED: ("↺", "철회"),
}
