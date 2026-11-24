"""Claude Code 인계 Markdown 보기·복사·내려받기와 결과 블록 붙여넣기(미리보기 → 확인하고 기록)."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from rcompass.models import Experiment, Project
from rcompass.services import ValidationError
from rcompass.services.datasets import list_versions
from rcompass.services.handoff import HandoffOptions, build_handoff
from rcompass.services.result_block import Preview, confirm_results, prepare_preview
from rcompass.web.context import Crumb, project_context
from rcompass.web.deps import experiment_or_404, get_session, project_or_404
from rcompass.web.routes.experiments import experiment_context, experiment_url, render_detail
from rcompass.web.templating import templates

router = APIRouter(prefix="/projects/{project_id}")
SessionDep = Annotated[Session, Depends(get_session)]
FormText = Annotated[str, Form()]
PREVIEW_FIELDS = ("results", "dataset_version", "code", "config", "note")


def _handoff_text(session: Session, project: Project, experiment: str) -> str:
    short_id = experiment.strip().upper()
    target = experiment_or_404(session, project, short_id) if short_id else None
    return build_handoff(session, project, HandoffOptions(today=date.today(), experiment=target))


# ---------------------------------------------------------------- 인계


@router.get("/handoff", response_class=HTMLResponse)
def handoff_page(request: Request, project_id: int, session: SessionDep, experiment: str = "") -> HTMLResponse:
    project = project_or_404(session, project_id)
    text = _handoff_text(session, project, experiment)
    context = project_context(session, project, tail=[Crumb("Claude Code 인계")], section="home")
    return templates.TemplateResponse(request, "handoff.html", {
        **context, "markdown": text, "line_count": text.count("\n"), "experiment": experiment,
    })


@router.get("/handoff.md")
def handoff_download(project_id: int, session: SessionDep, experiment: str = "") -> Response:
    project = project_or_404(session, project_id)
    text = _handoff_text(session, project, experiment)
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="rcompass-handoff-{project.id}.md"',
        "X-Content-Type-Options": "nosniff",
    })


# ---------------------------------------------------------------- 결과 블록


def _render_preview(request: Request, session: Session, project: Project, experiment: Experiment, *,
                    values: dict, preview: Preview | None = None, errors: dict | None = None,
                    status_code: int = 200) -> HTMLResponse:
    context = experiment_context(session, project, experiment)
    *before, current = context["crumbs"]
    context["crumbs"] = [*before, Crumb(current.label, experiment_url(project, experiment)), Crumb("결과 블록 미리보기")]
    return templates.TemplateResponse(request, "result_block_preview.html", {
        **context, "experiment": experiment, "preview": preview, "values": values, "errors": errors or {},
        "versions": list_versions(session, project.id),
    }, status_code=status_code)


@router.post("/experiments/{short_id}/result-block")
def preview_result_block(request: Request, project_id: int, short_id: str, session: SessionDep,
                         block: FormText = "") -> Response:
    """붙여넣은 블록을 해석해 미리보기(초안)를 보여줍니다. DB에는 쓰지 않습니다."""
    project = project_or_404(session, project_id)
    experiment = experiment_or_404(session, project, short_id)
    try:
        preview = prepare_preview(session, project, experiment, block)
    except ValidationError as error:
        session.rollback()
        return render_detail(request, session, project, experiment, errors={"block": error.errors["block"]},
                             values={"block": block}, status_code=422)
    values = {
        "results": preview.table,
        "dataset_version": str(preview.dataset.version.id) if preview.dataset else "",
        "code": preview.block.code or "",
        "config": preview.block.config or "",
        "note": preview.block.note or "",
        "files": preview.block.files,
    }
    response = _render_preview(request, session, project, experiment, preview=preview, values=values)
    session.rollback()  # 읽기만 했으므로 잠금을 바로 풉니다.
    return response


@router.post("/experiments/{short_id}/result-block/confirm")
async def confirm_result_block(request: Request, project_id: int, short_id: str) -> Response:
    """연구자가 확인(필요하면 수정)한 값을 실행별로 기록합니다."""
    form = await request.form()
    values: dict = {key: str(form.get(key, "")) for key in PREVIEW_FIELDS}
    values["files"] = [str(f) for f in form.getlist("files")]

    def save() -> Response:
        with request.app.state.sessions() as session:
            project = project_or_404(session, project_id)
            experiment = experiment_or_404(session, project, short_id)
            try:
                raw = values["dataset_version"].strip()
                version_id = int(raw) if raw else None
                if version_id is not None and version_id not in {v.version.id for v in list_versions(session, project.id)}:
                    raise ValidationError({"results": "선택한 데이터 버전을 찾을 수 없습니다."})
                outcome = confirm_results(session, project, experiment, table_text=values["results"],
                                          dataset_version_id=version_id, code=values["code"], config=values["config"])
            except (ValueError, ValidationError) as error:
                session.rollback()
                errors = (error.errors if isinstance(error, ValidationError)
                          else {"results": "데이터 버전 선택이 올바르지 않습니다."})
                return _render_preview(request, session, project, experiment, values=values, errors=errors,
                                       status_code=422)
            session.commit()
            params = {"saved": "claude"}
            if outcome.snapshot_baseline:
                params["snapshot"] = outcome.snapshot_baseline.short_id
            if outcome.warnings:
                params["warn"] = outcome.warnings[0]
            return RedirectResponse(f"{experiment_url(project, experiment)}?{urlencode(params)}#results",
                                    status_code=303)

    return await run_in_threadpool(save)
