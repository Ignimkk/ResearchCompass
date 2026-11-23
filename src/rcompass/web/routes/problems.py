"""문제 트리, 문제 상세, 하위 문제 추가, 상태 변경, 집중 문제 지정."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from rcompass.models import Problem, ProblemStatus, Project
from rcompass.services import ValidationError
from rcompass.services.experiments import problem_experiments
from rcompass.services.problems import (
    OUTCOME_SYMBOLS,
    STATUS_LABELS,
    add_problem,
    change_status,
    child_problems,
    get_problem,
    parked_problems,
    problem_path,
    problem_tree,
    set_focus,
)
from rcompass.services.projects import get_project
from rcompass.web.context import Crumb, problem_url, project_context
from rcompass.web.deps import get_session
from rcompass.web.templating import templates

router = APIRouter(prefix="/projects/{project_id}")
SessionDep = Annotated[Session, Depends(get_session)]
# 폼 필드는 모두 기본값을 두어, 빠진 필드가 FastAPI 422(영문)가 아니라 한국어 검증 오류가 되게 합니다.
FormText = Annotated[str, Form()]


def _project_or_404(session: Session, project_id: int) -> Project:
    project = get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    return project


def _problem_or_404(session: Session, project: Project, short_id: str) -> Problem:
    problem = get_problem(session, project.id, short_id)
    if problem is None:
        raise HTTPException(status_code=404, detail="문제를 찾을 수 없습니다.")
    return problem


def _common() -> dict:
    return {"status_labels": STATUS_LABELS, "statuses": list(ProblemStatus), "outcome_symbols": OUTCOME_SYMBOLS}


def _render_new(request: Request, session: Session, project: Project, parent: Problem | None,
                values: dict, errors: dict, status_code: int = 200) -> HTMLResponse:
    path = problem_path(session, parent) if parent else []
    context = project_context(session, project, path=path, tail=[Crumb("새 하위 문제")], section="problems",
                              return_to_question=True)
    return templates.TemplateResponse(
        request,
        "problem_new.html",
        {**context, **_common(), "parent": parent, "values": values, "errors": errors},
        status_code=status_code,
    )


@router.get("/problems", response_class=HTMLResponse)
def tree_page(request: Request, project_id: int, session: SessionDep) -> HTMLResponse:
    project = _project_or_404(session, project_id)
    context = project_context(session, project, tail=[Crumb("문제 트리")], section="problems")
    return templates.TemplateResponse(
        request,
        "problem_tree.html",
        {**context, **_common(), "tree": problem_tree(session, project.id),
         "parked": parked_problems(session, project.id)},
    )


@router.get("/problems/new", response_class=HTMLResponse)
def new_problem(request: Request, project_id: int, session: SessionDep, parent: str = "") -> HTMLResponse:
    project = _project_or_404(session, project_id)
    parent_problem = _problem_or_404(session, project, parent) if parent else None
    return _render_new(request, session, project, parent_problem, {"title": "", "why": "", "parked": ""}, {})


@router.post("/problems")
def create_problem(
    request: Request,
    project_id: int,
    session: SessionDep,
    title: FormText = "",
    why: FormText = "",
    parent: FormText = "",
    parked: FormText = "",
    revisit_condition: FormText = "",
) -> Response:
    project = _project_or_404(session, project_id)
    # 상위 문제가 없으면(오래된 탭 등) 폼을 다시 보여주지 않고 404로 알립니다.
    # 다시 보여주면 숨은 parent 값이 빠져 연구 질문 바로 아래에 잘못 만들어질 수 있습니다.
    parent_problem = _problem_or_404(session, project, parent) if parent else None
    status = ProblemStatus.PARKED if parked else ProblemStatus.ACTIVE
    try:
        problem = add_problem(session, project, title=title, why=why, parent_short_id=parent or None,
                              status=status, revisit_condition=revisit_condition)
    except ValidationError as error:
        session.rollback()
        values = {"title": title, "why": why, "parked": parked, "revisit_condition": revisit_condition}
        return _render_new(request, session, project, parent_problem, values, error.errors, status_code=422)
    session.commit()
    return RedirectResponse(f"/projects/{project.id}/problems#{problem.short_id}", status_code=303)


@router.get("/problems/{short_id}", response_class=HTMLResponse)
def problem_page(request: Request, project_id: int, short_id: str, session: SessionDep) -> HTMLResponse:
    project = _project_or_404(session, project_id)
    problem = _problem_or_404(session, project, short_id)
    context = project_context(session, project, path=problem_path(session, problem), section="problems")
    return templates.TemplateResponse(
        request,
        "problem_detail.html",
        {**context, **_common(), "problem": problem, "children": child_problems(session, problem),
         "is_focus": project.focus_problem_id == problem.id,
         "experiments": problem_experiments(session, problem)},
    )


@router.post("/problems/{short_id}/status")
def update_status(
    project_id: int,
    short_id: str,
    session: SessionDep,
    status: FormText = "",
    revisit_condition: FormText = "",
) -> Response:
    project = _project_or_404(session, project_id)
    problem = _problem_or_404(session, project, short_id)
    try:
        new_status = ProblemStatus(status)
    except ValueError:
        raise HTTPException(status_code=400, detail="알 수 없는 상태입니다.") from None
    change_status(session, project, problem, new_status, revisit_condition)
    session.commit()
    return RedirectResponse(problem_url(project, problem), status_code=303)


@router.post("/focus")
def update_focus(project_id: int, session: SessionDep, problem: FormText = "") -> Response:
    project = _project_or_404(session, project_id)
    target = _problem_or_404(session, project, problem) if problem else None
    try:
        set_focus(session, project, target)
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=error.errors["focus"]) from None
    session.commit()
    return RedirectResponse(f"/projects/{project.id}", status_code=303)
