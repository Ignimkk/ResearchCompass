"""구조도: 목록, 등록, 상세(sandbox iframe), HTML 내려주기.

올린 HTML은 남이 만든 것일 수도 있으므로 앱과 같은 출처(origin)로 실행되지 않게 합니다.
- 상세 화면의 iframe에 ``sandbox``를 붙이고 ``allow-same-origin``은 주지 않습니다.
- HTML 응답 자체에도 CSP ``sandbox``를 붙입니다. 주소를 새 탭에서 직접 열어도 불투명 출처로 실행되어,
  앱의 화면을 읽거나 상태를 바꾸는 요청(Origin 검사 통과)을 보낼 수 없습니다.
- ``connect-src 'none'``으로 스크립트의 네트워크 요청을 막고, ``frame-ancestors 'self'``로 앱 안에서만
  틀에 넣을 수 있게 합니다. 코드 근거의 SRC 링크(GitHub 등)는 새 창으로 열리도록 팝업만 허용합니다.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.models import Baseline, Diagram, DiagramBasis, DiagramKind, Experiment, FileObject, Problem, Project
from rcompass.services import ValidationError
from rcompass.services.diagrams import (
    BASIS_LABELS,
    KIND_LABELS,
    IrHints,
    diagram_html_path,
    freshness,
    get_diagram,
    list_diagrams,
    read_ir_hints,
    register_diagram,
    related_path_list,
    repo_href,
    source_link,
)
from rcompass.services.files import (
    FileTooLarge,
    StoredBlob,
    blob_path,
    discard_if_unreferenced,
    format_size,
    store_stream,
)
from rcompass.web.context import Crumb, project_context
from rcompass.web.deps import get_session, project_or_404
from rcompass.web.templating import templates

router = APIRouter(prefix="/projects/{project_id}")
SessionDep = Annotated[Session, Depends(get_session)]
FormText = Annotated[str, Form()]

# iframe의 sandbox 속성과 응답 CSP의 sandbox 지시어는 같은 권한만 줍니다(allow-same-origin, allow-forms,
# allow-top-navigation 없음). allow-downloads는 Archify의 이미지·SVG 내보내기 버튼을 위해 둡니다.
SANDBOX_FLAGS = "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads"
DIAGRAM_HEADERS = {
    "Content-Security-Policy": (
        f"sandbox {SANDBOX_FLAGS}; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; worker-src 'none'; "
        "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-store",
}


def _diagram_or_404(session: Session, project: Project, short_id: str) -> Diagram:
    diagram = get_diagram(session, project.id, short_id)
    if diagram is None:
        raise HTTPException(status_code=404, detail="구조도를 찾을 수 없습니다.")
    return diagram


def _link_choices(session: Session, project: Project) -> dict:
    def rows(model):
        return list(session.scalars(select(model).where(model.project_id == project.id).order_by(model.id)))

    return {"problems": rows(Problem), "experiments": rows(Experiment), "baselines": rows(Baseline)}


def _render_list(request: Request, session: Session, project: Project, *, values: dict | None = None,
                 errors: dict | None = None, status_code: int = 200) -> HTMLResponse:
    context = project_context(session, project, tail=[Crumb("구조도")], section="diagrams")
    diagrams = list_diagrams(session, project.id)
    return templates.TemplateResponse(
        request, "diagrams.html",
        {**context, **_link_choices(session, project), "diagrams": diagrams,
         "freshness": {d.id: freshness(session, project, d) for d in diagrams},
         "kind_labels": KIND_LABELS, "basis_labels": BASIS_LABELS,
         "kinds": list(DiagramKind), "bases": list(DiagramBasis),
         "values": values or {}, "errors": errors or {}},
        status_code=status_code,
    )


@router.get("/diagrams", response_class=HTMLResponse)
def diagrams_page(request: Request, project_id: int, session: SessionDep) -> HTMLResponse:
    return _render_list(request, session, project_or_404(session, project_id))


@router.post("/diagrams")
def register_diagram_route(
    request: Request, project_id: int, session: SessionDep,
    title: FormText = "", kind: FormText = "", basis: FormText = "", repo_url: FormText = "",
    commit_sha: FormText = "", related_paths: FormText = "", explanation: FormText = "",
    problem: FormText = "", experiment: FormText = "", baseline: FormText = "",
    html: Annotated[UploadFile | None, File()] = None,
    json_ir: Annotated[UploadFile | None, File()] = None,
) -> Response:
    settings = request.app.state.settings
    values = {"title": title, "kind": kind, "basis": basis, "repo_url": repo_url, "commit_sha": commit_sha,
              "related_paths": related_paths, "explanation": explanation, "problem": problem,
              "experiment": experiment, "baseline": baseline}
    errors: dict[str, str] = {}
    try:
        diagram_kind = DiagramKind(kind)
    except ValueError:
        diagram_kind = None
        errors["kind"] = "구조도 종류를 고르세요."
    try:
        diagram_basis = DiagramBasis(basis)
    except ValueError:
        diagram_basis = None
        errors["basis"] = "근거 구분을 고르세요."

    # 내용은 DB를 건드리기 전에 저장합니다. 복사·해시 계산 동안 DB 쓰기 잠금을 잡지 않기 위해서입니다.
    blobs: list[StoredBlob] = []
    html_blob = ir_blob = None
    hints = None
    if html is not None and html.filename:
        try:
            html_blob = store_stream(settings.data_dir, html.file, settings.max_attachment_bytes)
            blobs.append(html_blob)
        except FileTooLarge as error:
            errors["html"] = str(error)
    if json_ir is not None and json_ir.filename:
        try:
            ir_blob = store_stream(settings.data_dir, json_ir.file, settings.max_attachment_bytes)
            blobs.append(ir_blob)
            hints = read_ir_hints(blob_path(settings.data_dir, ir_blob.rel_path))
        except FileTooLarge as error:
            errors["json_ir"] = str(error)
        except ValidationError as error:
            errors.update(error.errors)

    def discard() -> None:
        session.rollback()
        for blob in blobs:
            discard_if_unreferenced(session, settings.data_dir, blob)

    try:
        project = project_or_404(session, project_id)
        if not errors:
            try:
                diagram = register_diagram(
                    session, project, title=title, kind=diagram_kind, basis=diagram_basis,
                    html_blob=html_blob, html_name=html.filename if html_blob else None,
                    ir_blob=ir_blob, ir_name=json_ir.filename if ir_blob else None, ir_hints=hints or IrHints(),
                    repo_url=repo_url, commit_sha=commit_sha, related_paths=related_paths,
                    explanation=explanation, problem=problem, experiment=experiment, baseline=baseline)
            except ValidationError as error:
                errors = error.errors
    except BaseException:  # 404 등: 방금 저장한 내용이 고아로 남지 않게 정리
        discard()
        raise
    if errors:
        discard()
        if blobs and "html" not in errors:
            errors["html"] = "입력을 고친 뒤 파일을 다시 골라 주세요."
        return _render_list(request, session, project, values=values, errors=errors, status_code=422)
    session.commit()
    return RedirectResponse(f"/projects/{project.id}/diagrams/{diagram.short_id}", status_code=303)


@router.get("/diagrams/{short_id}", response_class=HTMLResponse)
def diagram_page(request: Request, project_id: int, short_id: str, session: SessionDep) -> HTMLResponse:
    project = project_or_404(session, project_id)
    diagram = _diagram_or_404(session, project, short_id)
    data_dir = request.app.state.settings.data_dir
    html_file = session.get(FileObject, diagram.file_id)
    ir_file = session.get(FileObject, diagram.json_ir_file_id) if diagram.json_ir_file_id else None
    paths = related_path_list(diagram)
    context = project_context(
        session, project,
        tail=[Crumb("구조도", f"/projects/{project.id}/diagrams"), Crumb(f"{diagram.short_id} {diagram.title}")],
        section="diagrams")
    return templates.TemplateResponse(
        request, "diagram_detail.html",
        {**context, "diagram": diagram, "html_file": html_file, "ir_file": ir_file,
         "available": diagram_html_path(session, data_dir, diagram) is not None,
         "paths": [(path, source_link(diagram, path)) for path in paths],
         "freshness": freshness(session, project, diagram),
         "problem": session.get(Problem, diagram.linked_problem_id) if diagram.linked_problem_id else None,
         "experiment": session.get(Experiment, diagram.linked_experiment_id) if diagram.linked_experiment_id else None,
         "baseline_link": session.get(Baseline, diagram.linked_baseline_id) if diagram.linked_baseline_id else None,
         "repo_href": repo_href(diagram), "kind_labels": KIND_LABELS, "basis_labels": BASIS_LABELS, "sandbox_flags": SANDBOX_FLAGS,
         "size": format_size(html_file.size_bytes if html_file else None)},
    )


@router.get("/diagrams/{short_id}/view")
def diagram_view(request: Request, project_id: int, short_id: str, session: SessionDep) -> Response:
    """구조도 HTML. CSP sandbox로 불투명 출처에서만 실행됩니다."""
    project = project_or_404(session, project_id)
    diagram = _diagram_or_404(session, project, short_id)
    path = diagram_html_path(session, request.app.state.settings.data_dir, diagram)
    if path is None:
        raise HTTPException(status_code=404, detail="저장된 구조도 파일이 없습니다. 백업에서 복구하세요.")
    return FileResponse(path, media_type="text/html; charset=utf-8", headers=DIAGRAM_HEADERS,
                        content_disposition_type="inline")
