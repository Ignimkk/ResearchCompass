"""홈(프로젝트 목록), 새 연구 만들기, 프로젝트 홈."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from rcompass.config import is_inside, source_checkout_root
from rcompass.models import Aggregate, Direction, Experiment, Problem
from rcompass.services import ValidationError
from rcompass.services.problems import STATUS_LABELS as PROBLEM_STATUS_LABELS
from rcompass.services.problems import inactive_ancestors, parked_problems, problem_path
from rcompass.services.progress import STATE_LABELS, CriterionState, evaluate_project, goal_summary
from rcompass.services.projects import (
    CriterionInput,
    ProjectInput,
    create_project,
    get_project,
    list_projects,
    validate_project_input,
)
from rcompass.web.context import project_context
from rcompass.web.deps import get_session
from rcompass.web.templating import templates

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]

CRITERION_FIELDS = ("name", "metric_key", "direction", "target", "unit", "aggregate", "min_runs", "baseline_values")
MAX_CRITERIA_ROWS = 20
DEFAULT_ROWS = 2
_ROW_FIELD = re.compile(r"^criteria-(\d+)-([a-z_]+)$")


def _empty_row() -> dict[str, str]:
    return {"name": "", "metric_key": "", "direction": Direction.LOWER.value, "target": "", "unit": "",
            "aggregate": Aggregate.MEAN.value, "min_runs": "1", "baseline_values": ""}


def _parse_number(text: str) -> float | None:
    try:
        return float(text.strip())
    except ValueError:
        return None


def _parse_values(text: str) -> list[float] | None:
    """'17, 18, 19'나 '17 18 19'처럼 쉼표·공백·세미콜론으로 구분한 실행별 값."""
    parts = [part for part in re.split(r"[,\s;]+", text.strip()) if part]
    values = [_parse_number(part) for part in parts]
    return None if any(v is None for v in values) else values  # type: ignore[return-value]


def _is_blank_row(row: dict[str, str]) -> bool:
    return not any(row[key].strip() for key in ("name", "metric_key", "target", "unit", "baseline_values"))


def _parse_form(form) -> ParsedForm:
    """폼 원문을 기준 행 목록과 서비스 입력으로 바꾸고, 필드별 오류를 모읍니다."""
    values = {key: str(form.get(key, "")) for key in ("title", "question", "baseline_label", "next_action")}
    rows_by_index: dict[int, dict[str, str]] = {}
    for key, value in form.multi_items():
        match = _ROW_FIELD.match(key)
        if match and match.group(2) in CRITERION_FIELDS and int(match.group(1)) < MAX_CRITERIA_ROWS:
            rows_by_index.setdefault(int(match.group(1)), _empty_row())[match.group(2)] = str(value)
    rows = [rows_by_index[i] for i in sorted(rows_by_index)] or [_empty_row()]

    errors: dict[str, str] = {}
    criteria: list[CriterionInput] = []
    row_of_criterion: list[int] = []
    for index, row in enumerate(rows):
        if _is_blank_row(row):
            continue
        target = _parse_number(row["target"])
        if target is None:
            errors[f"criteria-{index}-target"] = "목표값을 숫자로 입력하세요."
        min_runs_text = row["min_runs"].strip() or "1"
        # isdigit()는 '²' 같은 유니코드 숫자도 참이라 ASCII 숫자만 받습니다. 범위는 서비스가 검사합니다.
        min_runs = int(min_runs_text) if min_runs_text.isascii() and min_runs_text.isdigit() else 0
        baseline_values = _parse_values(row["baseline_values"])
        if baseline_values is None:
            errors[f"criteria-{index}-baseline_values"] = "기준선 값은 숫자를 쉼표로 구분해 입력하세요. 예: 17, 18, 19"
        try:
            direction = Direction(row["direction"])
        except ValueError:
            direction = Direction.LOWER
            errors[f"criteria-{index}-direction"] = "방향을 선택하세요."
        try:
            aggregate = Aggregate(row["aggregate"])
        except ValueError:
            aggregate = Aggregate.MEAN
            errors[f"criteria-{index}-aggregate"] = "집계 방식을 선택하세요."
        criteria.append(
            CriterionInput(
                name=row["name"],
                metric_key=row["metric_key"],
                direction=direction,
                target=target if target is not None else 0.0,
                unit=row["unit"],
                aggregate=aggregate,
                min_runs=min_runs,
                baseline_values=baseline_values or [],
            )
        )
        row_of_criterion.append(index)

    data = ProjectInput(
        title=values["title"],
        question=values["question"],
        baseline_label=values["baseline_label"],
        next_action=values["next_action"],
        criteria=criteria,
    )
    for key, message in _to_row_keys(validate_project_input(data), row_of_criterion).items():
        errors.setdefault(key, message)
    return ParsedForm(values, rows, None if errors else data, errors, row_of_criterion)


@dataclass
class ParsedForm:
    values: dict[str, str]
    rows: list[dict[str, str]]
    data: ProjectInput | None  # 오류가 있으면 None
    errors: dict[str, str]
    row_of_criterion: list[int]  # 서비스 입력의 기준 순서 → 화면 행 번호


def _to_row_keys(errors: dict[str, str], row_of_criterion: list[int]) -> dict[str, str]:
    """서비스 오류의 기준 번호(빈 행을 뺀 순서)를 화면의 행 번호로 바꿉니다."""
    mapped: dict[str, str] = {}
    for key, message in errors.items():
        match = re.match(r"^criteria-(\d+)-(.+)$", key)
        if match and int(match.group(1)) < len(row_of_criterion):
            key = f"criteria-{row_of_criterion[int(match.group(1))]}-{match.group(2)}"
        mapped[key] = message
    return mapped


def _render_new(request: Request, values: dict, rows: list[dict[str, str]], errors: dict[str, str],
                status_code: int = 200) -> HTMLResponse:
    while len(rows) < DEFAULT_ROWS:
        rows.append(_empty_row())
    return templates.TemplateResponse(
        request,
        "project_new.html",
        {"values": values, "rows": rows, "errors": errors, "directions": list(Direction),
         "aggregates": list(Aggregate), "max_rows": MAX_CRITERIA_ROWS},
        status_code=status_code,
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: SessionDep) -> HTMLResponse:
    settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "projects": list_projects(session),
            "data_dir": str(settings.data_dir),
            "data_dir_inside_checkout": is_inside(settings.data_dir, source_checkout_root()),
        },
    )


@router.get("/projects/new", response_class=HTMLResponse)
def new_project(request: Request) -> HTMLResponse:
    values = {"title": "", "question": "", "baseline_label": "", "next_action": ""}
    return _render_new(request, values, [_empty_row()], {})


@router.post("/projects")
async def create_project_route(request: Request) -> Response:
    # 기준 행 수가 가변이라 폼은 async로 읽고, 막히는 DB 작업은 스레드풀에서 합니다.
    # (이벤트 루프에서 DB 잠금을 기다리면 다른 요청이 잠금을 풀지 못해 서버 전체가 멈춥니다.)
    parsed = _parse_form(await request.form())
    if parsed.data is None:
        return _render_new(request, parsed.values, parsed.rows, parsed.errors, status_code=422)
    data = parsed.data

    def save() -> int:
        with request.app.state.sessions() as session:
            project = create_project(session, data)
            session.commit()
            return project.id

    try:
        project_id = await run_in_threadpool(save)
    except ValidationError as error:  # 폼 검증 뒤에 서비스 검증이 다시 실패한 경우
        errors = _to_row_keys(error.errors, parsed.row_of_criterion)
        return _render_new(request, parsed.values, parsed.rows, errors, status_code=422)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_home(request: Request, project_id: int, session: SessionDep) -> HTMLResponse:
    project = get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    evaluations = evaluate_project(session, project)
    context = project_context(session, project, section="home")
    baseline = context["baseline"]
    baseline_experiment = session.get(Experiment, baseline.source_experiment_id) if baseline else None
    focus = session.get(Problem, project.focus_problem_id) if project.focus_problem_id else None
    focus_path = problem_path(session, focus) if focus else []
    return templates.TemplateResponse(
        request,
        "project_home.html",
        {
            **context,
            "evaluations": evaluations,
            "summary": goal_summary(evaluations),
            "states": list(CriterionState),
            "state_labels": STATE_LABELS,
            "baseline_experiment": baseline_experiment,
            "focus": focus,
            "focus_path": focus_path,
            "focus_blockers": inactive_ancestors(focus_path),
            "problem_status_labels": PROBLEM_STATUS_LABELS,
            "parked_count": len(parked_problems(session, project.id)),
        },
    )
