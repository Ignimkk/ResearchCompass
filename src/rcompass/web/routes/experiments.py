"""실험 만들기, 실험 상세, 가설 수정, 입력, 결과 붙여넣기, 실행 제외."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from rcompass.models import Baseline, Experiment, ExperimentKind, FileKind, Problem, Project
from rcompass.services import ValidationError
from rcompass.services.datasets import list_versions
from rcompass.services.experiments import (
    create_experiment,
    get_experiment,
    input_versions,
    revisions,
    update_inputs,
    update_plan,
)
from rcompass.services.files import (
    KIND_LABELS,
    UPLOAD_KINDS,
    FileTooLarge,
    add_file,
    discard_if_unreferenced,
    format_size,
    list_files,
    store_stream,
)
from rcompass.services.problems import get_problem, problem_path
from rcompass.services.projects import get_criteria, get_project
from rcompass.services.runs import (
    exclude_run,
    experiment_results,
    get_run,
    parse_results_table,
    record_results,
)
from rcompass.web.context import Crumb, project_context
from rcompass.web.deps import get_session
from rcompass.web.templating import templates

router = APIRouter(prefix="/projects/{project_id}")
SessionDep = Annotated[Session, Depends(get_session)]
FormText = Annotated[str, Form()]


def _project_or_404(session: Session, project_id: int) -> Project:
    project = get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    return project


def _experiment_or_404(session: Session, project: Project, short_id: str) -> Experiment:
    experiment = get_experiment(session, project.id, short_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="실험을 찾을 수 없습니다.")
    return experiment


def experiment_url(project: Project, experiment: Experiment) -> str:
    return f"/projects/{project.id}/experiments/{experiment.short_id}"


def _experiment_context(session: Session, project: Project, experiment: Experiment) -> dict:
    problem = session.get(Problem, experiment.problem_id) if experiment.problem_id else None
    path = problem_path(session, problem) if problem else []
    tail = [Crumb(f"{experiment.short_id} {experiment.title}")]
    return project_context(session, project, path=path, tail=tail, section="problems")


def _default_table_header(session: Session, project: Project) -> str:
    keys = [c.metric_key for c in get_criteria(session, project.id)]
    return "\t".join(["label", *keys])


def _render_detail(request: Request, session: Session, project: Project, experiment: Experiment, *,
                   errors: dict | None = None, values: dict | None = None, status_code: int = 200) -> HTMLResponse:
    context = _experiment_context(session, project, experiment)
    inputs = input_versions(session, experiment)
    params = request.query_params
    return templates.TemplateResponse(
        request,
        "experiment_detail.html",
        {
            **context,
            "experiment": experiment,
            "is_baseline_measurement": experiment.kind is ExperimentKind.BASELINE_MEASUREMENT,
            "start_baseline": session.get(Baseline, experiment.based_on_baseline_id)
            if experiment.based_on_baseline_id else None,
            "results": experiment_results(session, experiment),
            "revisions": revisions(session, experiment),
            "versions": list_versions(session, project.id),
            # 저장 실패로 다시 보여줄 때는 사용자가 고른 체크 상태를 유지합니다.
            "checked_ids": (values or {}).get("input_ids", {str(v.version.id) for v in inputs}),
            "inputs": inputs,
            "errors": errors or {},
            "values": values or {},
            "table_header": _default_table_header(session, project),
            "files": list_files(session, project.id, experiment=experiment),
            "kind_labels": KIND_LABELS,
            "upload_kinds": UPLOAD_KINDS,
            "format_size": format_size,
            "notice": {"snapshot": params.get("snapshot"), "warn": params.get("warn"),
                       "saved": params.get("saved")},
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------- 만들기


@router.get("/problems/{problem_short_id}/experiments/new", response_class=HTMLResponse)
def new_experiment(request: Request, project_id: int, problem_short_id: str, session: SessionDep) -> HTMLResponse:
    project = _project_or_404(session, project_id)
    problem = get_problem(session, project.id, problem_short_id)
    if problem is None:
        raise HTTPException(status_code=404, detail="문제를 찾을 수 없습니다.")
    return _render_new(request, session, project, problem, {}, {})


def _render_new(request: Request, session: Session, project: Project, problem: Problem, values: dict,
                errors: dict, status_code: int = 200) -> HTMLResponse:
    context = project_context(session, project, path=problem_path(session, problem), tail=[Crumb("새 실험")],
                              section="problems")
    return templates.TemplateResponse(
        request, "experiment_new.html",
        {**context, "problem": problem, "values": values, "errors": errors}, status_code=status_code,
    )


@router.post("/experiments")
def create_experiment_route(
    request: Request, project_id: int, session: SessionDep,
    problem: FormText = "", title: FormText = "", hypothesis: FormText = "", decision_rule: FormText = "",
    code_ref: FormText = "", config_text: FormText = "",
) -> Response:
    project = _project_or_404(session, project_id)
    owner = get_problem(session, project.id, problem) if problem else None
    if owner is None:
        raise HTTPException(status_code=404, detail="문제를 찾을 수 없습니다.")
    try:
        experiment = create_experiment(session, project, problem_short_id=problem, title=title,
                                       hypothesis=hypothesis, decision_rule=decision_rule,
                                       code_ref=code_ref, config_text=config_text)
    except ValidationError as error:
        session.rollback()
        values = {"title": title, "hypothesis": hypothesis, "decision_rule": decision_rule,
                  "code_ref": code_ref, "config_text": config_text}
        return _render_new(request, session, project, owner, values, error.errors, status_code=422)
    session.commit()
    return RedirectResponse(experiment_url(project, experiment), status_code=303)


# ---------------------------------------------------------------- 상세와 수정


@router.get("/experiments/{short_id}", response_class=HTMLResponse)
def experiment_page(request: Request, project_id: int, short_id: str, session: SessionDep) -> HTMLResponse:
    project = _project_or_404(session, project_id)
    return _render_detail(request, session, project, _experiment_or_404(session, project, short_id))


@router.post("/experiments/{short_id}/plan")
def update_plan_route(
    request: Request, project_id: int, short_id: str, session: SessionDep,
    title: FormText = "", hypothesis: FormText = "", decision_rule: FormText = "", confirm: FormText = "",
) -> Response:
    project = _project_or_404(session, project_id)
    experiment = _experiment_or_404(session, project, short_id)
    try:
        update_plan(session, experiment, title=title, hypothesis=hypothesis, decision_rule=decision_rule,
                    confirm_after_results=bool(confirm))
    except ValidationError as error:
        session.rollback()
        values = {"title": title, "hypothesis": hypothesis, "decision_rule": decision_rule, "plan_open": True}
        return _render_detail(request, session, project, experiment, errors=error.errors, values=values,
                              status_code=422)
    session.commit()
    return RedirectResponse(experiment_url(project, experiment) + "?saved=plan", status_code=303)


@router.post("/experiments/{short_id}/inputs")
async def update_inputs_route(request: Request, project_id: int, short_id: str) -> Response:
    # 데이터 버전은 여러 개를 고를 수 있어 폼을 직접 읽습니다. DB 작업은 스레드풀에서 합니다.
    form = await request.form()
    raw_ids = [str(v) for v in form.getlist("dataset_version")]
    code_ref = str(form.get("code_ref", ""))
    config_text = str(form.get("config_text", ""))

    def save() -> Response:
        with request.app.state.sessions() as session:
            project = _project_or_404(session, project_id)
            experiment = _experiment_or_404(session, project, short_id)
            try:
                ids = [int(v) for v in raw_ids if v.strip()]
                update_inputs(session, experiment, dataset_version_ids=ids, code_ref=code_ref,
                              config_text=config_text)
            except (ValueError, ValidationError) as error:
                session.rollback()
                errors = error.errors if isinstance(error, ValidationError) else {"inputs": "데이터 버전 선택이 올바르지 않습니다."}
                values = {"code_ref": code_ref, "config_text": config_text, "input_ids": set(raw_ids)}
                return _render_detail(request, session, project, experiment, errors=errors, values=values,
                                      status_code=422)
            session.commit()
            return RedirectResponse(experiment_url(project, experiment) + "?saved=inputs", status_code=303)

    return await run_in_threadpool(save)


@router.post("/experiments/{short_id}/results")
def record_results_route(
    request: Request, project_id: int, short_id: str, session: SessionDep,
    results: FormText = "", dataset_version: FormText = "",
) -> Response:
    project = _project_or_404(session, project_id)
    experiment = _experiment_or_404(session, project, short_id)
    try:
        version_id = int(dataset_version) if dataset_version.strip() else None
        valid_ids = {v.version.id for v in list_versions(session, project.id)}
        if version_id is not None and version_id not in valid_ids:
            raise ValidationError({"results": "선택한 데이터 버전을 찾을 수 없습니다."})
        outcome = record_results(session, project, experiment, parse_results_table(results),
                                 dataset_version_id=version_id)
    except (ValueError, ValidationError) as error:
        session.rollback()
        errors = error.errors if isinstance(error, ValidationError) else {"results": "데이터 버전 선택이 올바르지 않습니다."}
        return _render_detail(request, session, project, experiment, errors=errors,
                              values={"results": results, "dataset_version": dataset_version}, status_code=422)
    session.commit()
    params = {"saved": "results"}
    if outcome.snapshot_baseline:
        params["snapshot"] = outcome.snapshot_baseline.short_id
    if outcome.warnings:
        params["warn"] = outcome.warnings[0]
    return RedirectResponse(f"{experiment_url(project, experiment)}?{urlencode(params)}#results", status_code=303)


@router.post("/experiments/{short_id}/runs/{run_short_id}/exclude")
def exclude_run_route(
    request: Request, project_id: int, short_id: str, run_short_id: str, session: SessionDep,
    reason: FormText = "",
) -> Response:
    project = _project_or_404(session, project_id)
    experiment = _experiment_or_404(session, project, short_id)
    run = get_run(session, experiment, run_short_id)
    if run is None:
        raise HTTPException(status_code=404, detail="실행을 찾을 수 없습니다.")
    try:
        exclude_run(session, run, reason)
    except ValidationError as error:
        session.rollback()
        return _render_detail(request, session, project, experiment,
                              errors={f"exclude-{run.short_id}": error.errors["reason"]}, status_code=422)
    session.commit()
    return RedirectResponse(f"{experiment_url(project, experiment)}?saved=exclude#results", status_code=303)


@router.post("/experiments/{short_id}/files")
def upload_file_route(
    request: Request, project_id: int, short_id: str, session: SessionDep,
    upload: Annotated[UploadFile | None, File()] = None,
    kind: FormText = "figure", caption: FormText = "", run: FormText = "",
) -> Response:
    """결과 그래프·표·설정·로그를 실험(과 실행)에 첨부합니다. 입력 데이터 버전이 파일의 출처로 연결됩니다."""
    settings = request.app.state.settings
    problems: list[str] = []
    try:
        file_kind = FileKind(kind)
    except ValueError:
        file_kind = FileKind.OTHER
    has_upload = upload is not None and bool(upload.filename)
    if not has_upload:
        problems.append("첨부할 파일을 고르세요.")
    # 내용은 DB를 건드리기 전에 저장합니다. 복사·해시 계산 동안 DB 쓰기 잠금을 잡지 않기 위해서입니다.
    blob = None
    if has_upload:
        try:
            blob = store_stream(settings.data_dir, upload.file, settings.max_attachment_bytes)
        except FileTooLarge as error:
            problems.append(f"{error} 큰 파일은 자료 화면에서 위치(경로나 URL)로 기록하세요.")
    try:
        project = _project_or_404(session, project_id)
        experiment = _experiment_or_404(session, project, short_id)
        target_run = get_run(session, experiment, run) if run else None
        if run and target_run is None:
            problems.append("선택한 실행을 찾을 수 없습니다.")
        if blob is not None and not problems:
            try:
                add_file(session, project, blob, original_name=upload.filename, kind=file_kind, caption=caption,
                         experiment=experiment, run=target_run,
                         derived_from_version_ids=[v.version.id for v in input_versions(session, experiment)])
            except ValidationError as error:
                problems.extend(error.errors.values())
    except BaseException:  # 404 등: 방금 저장한 내용이 고아로 남지 않게 정리
        session.rollback()
        if blob is not None:
            discard_if_unreferenced(session, settings.data_dir, blob)
        raise
    if problems:
        session.rollback()
        if blob is not None:
            discard_if_unreferenced(session, settings.data_dir, blob)
        return _render_detail(request, session, project, experiment, errors={"upload": " ".join(problems)},
                              values={"caption": caption, "kind": kind, "run": run}, status_code=422)
    session.commit()
    return RedirectResponse(f"{experiment_url(project, experiment)}?saved=file#files", status_code=303)
