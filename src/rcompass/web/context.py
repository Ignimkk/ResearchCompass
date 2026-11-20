"""모든 프로젝트 화면 상단의 맥락 바 데이터 (계획서 3절).

원래 질문, 현재 위치(경로), 돌아갈 곳(상위 문제 또는 연구 질문), 현재 기준선을 한 줄씩 보여줍니다.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from rcompass.models import Baseline, Problem, Project


@dataclass(frozen=True)
class Crumb:
    label: str
    url: str | None = None


def problem_url(project: Project, problem: Problem) -> str:
    return f"/projects/{project.id}/problems/{problem.short_id}"


def project_context(
    session: Session,
    project: Project,
    *,
    path: list[Problem] | None = None,
    tail: list[Crumb] | None = None,
    section: str = "home",
    return_to_question: bool = False,
) -> dict:
    """템플릿에 넘길 맥락 바 값.

    ``path``는 연구 질문 아래부터 문제까지의 경로입니다. ``tail``이 없으면 ``path``의 마지막이
    현재 위치이고, ``tail``이 있으면(예: 그 문제 아래 '새 하위 문제' 화면) ``path`` 전체가 현재 위치의 상위입니다.
    돌아갈 곳은 현재 위치의 바로 위 문제이고, 위 문제가 없으면 연구 질문(문제 트리)입니다.
    """
    path = path or []
    crumbs = [Crumb(f"{p.short_id} {p.title}", problem_url(project, p)) for p in path]
    crumbs += tail or []
    if crumbs and not tail:
        crumbs[-1] = Crumb(crumbs[-1].label)  # 현재 위치는 링크 없이

    ancestors = path if tail else path[:-1]
    if ancestors:
        parent = ancestors[-1]
        return_to = Crumb(f"상위 문제 {parent.short_id} {parent.title}", problem_url(project, parent))
    elif path or return_to_question:
        return_to = Crumb("연구 질문", f"/projects/{project.id}/problems")
    else:
        return_to = None

    baseline = session.get(Baseline, project.current_baseline_id) if project.current_baseline_id else None
    return {
        "project": project,
        "crumbs": crumbs,
        "return_to": return_to,
        "baseline": baseline,
        "section": section,
    }
