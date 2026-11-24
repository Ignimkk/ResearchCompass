"""자료 열람: 데이터 버전, 첨부 파일, 파일 계보, 파일 내용 내려주기, 외부 참조 지문."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.models import Experiment, FileKind, Project
from rcompass.services import ValidationError
from rcompass.services.datasets import (
    VERIFY_MESSAGES,
    VerifyResult,
    add_dataset_version,
    get_version,
    list_versions,
    apply_fingerprint,
    compute_fingerprint,
    verification_target,
    version_usage,
)
from rcompass.services.files import (
    KIND_LABELS,
    FileTooLarge,
    add_file,
    blob_exists,
    blob_path,
    discard_if_unreferenced,
    format_size,
    get_file,
    list_files,
    preview_kind,
    store_stream,
    text_preview,
)
from rcompass.services.lineage import file_lineage
from rcompass.web.context import Crumb, project_context
from rcompass.web.deps import get_session, project_or_404, safe_back
from rcompass.web.templating import templates

router = APIRouter(prefix="/projects/{project_id}")
SessionDep = Annotated[Session, Depends(get_session)]
FormText = Annotated[str, Form()]

# 첨부 파일 응답 헤더: 내용 형식 추측 금지, 문서로 열려도 스크립트 실행 금지.
FILE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
    "Cache-Control": "private, no-store",
}


def _render_materials(request: Request, session: Session, project: Project, *, values: dict | None = None,
                      errors: dict | None = None, status_code: int = 200, back: str | None = None) -> HTMLResponse:
    context = project_context(session, project, tail=[Crumb("자료")], section="materials")
    files = list_files(session, project.id)
    experiments = {e.id: e for e in session.scalars(select(Experiment).where(Experiment.project_id == project.id))}
    verify = request.query_params.get("verify")
    return templates.TemplateResponse(
        request, "datasets.html",
        {**context, "versions": list_versions(session, project.id), "usage": version_usage(session, project.id),
         "files": files, "experiments": experiments, "kind_labels": KIND_LABELS, "format_size": format_size,
         "values": values or {}, "errors": errors or {},
         "back": back if back is not None else request.query_params.get("back", ""),
         "verify_message": VERIFY_MESSAGES.get(VerifyResult(verify)) if verify in VerifyResult._value2member_map_ else None,
         "verify_mismatch": verify == VerifyResult.MISMATCH.value},
        status_code=status_code,
    )


@router.get("/datasets", response_class=HTMLResponse)
def materials_page(request: Request, project_id: int, session: SessionDep) -> HTMLResponse:
    return _render_materials(request, session, project_or_404(session, project_id))


@router.post("/datasets")
def add_dataset_route(
    request: Request, project_id: int, session: SessionDep,
    dataset_name: FormText = "", version_label: FormText = "", external_uri: FormText = "",
    source: FormText = "", collected_at: FormText = "", sha256: FormText = "", notes: FormText = "",
    back: FormText = "", attachment: Annotated[UploadFile | None, File()] = None,
) -> Response:
    settings = request.app.state.settings
    values = {"dataset_name": dataset_name, "version_label": version_label, "external_uri": external_uri,
              "source": source, "collected_at": collected_at, "sha256": sha256, "notes": notes}
    errors: dict[str, str] = {}
    collected = None
    if collected_at.strip():
        try:
            collected = date.fromisoformat(collected_at.strip())
        except ValueError:
            errors["collected_at"] = "수집일은 2026-11-02 형식으로 입력하세요."
    has_upload = attachment is not None and bool(attachment.filename)
    if has_upload and external_uri.strip():
        errors["external_uri"] = "위치와 첨부 파일 중 하나만 고르세요."
    # 첨부 내용은 DB를 건드리기 전에 저장합니다. 복사·해시 계산 동안 DB 쓰기 잠금을 잡지 않기 위해서입니다.
    blob = None
    if has_upload and not errors:
        try:
            blob = store_stream(settings.data_dir, attachment.file, settings.max_attachment_bytes)
        except FileTooLarge as error:
            errors["attachment"] = f"{error} 큰 데이터는 위치(경로나 URL)로 기록하세요."
    try:
        project = project_or_404(session, project_id)
        if not errors:
            attached = None
            if blob is not None:
                attached = add_file(session, project, blob, original_name=attachment.filename, kind=FileKind.DATA)
            try:
                add_dataset_version(session, project, dataset_name=dataset_name, version_label=version_label,
                                    external_uri=external_uri, source=source, collected_at=collected,
                                    sha256=sha256, notes=notes, attached_file=attached)
            except ValidationError as error:
                errors = error.errors
    except BaseException:
        session.rollback()
        if blob is not None:
            discard_if_unreferenced(session, settings.data_dir, blob)
        raise
    if errors:
        session.rollback()
        if blob is not None:
            discard_if_unreferenced(session, settings.data_dir, blob)
        return _render_materials(request, session, project, values=values, errors=errors, status_code=422, back=back)
    session.commit()
    # 실험 화면에서 왔으면 그 실험으로 돌아갑니다(내부 경로만 허용).
    return RedirectResponse(safe_back(project, back, f"/projects/{project.id}/datasets"), status_code=303)


@router.post("/datasets/{version_id}/verify")
def verify_version_route(project_id: int, version_id: int, session: SessionDep) -> Response:
    project = project_or_404(session, project_id)
    view = get_version(session, project.id, version_id)
    if view is None:
        raise HTTPException(status_code=404, detail="데이터 버전을 찾을 수 없습니다.")
    try:
        target = verification_target(view)
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=error.errors["verify"]) from None
    # 큰 파일의 해시 계산은 오래 걸리므로, 계산하는 동안에는 트랜잭션(쓰기 잠금)을 닫아 둡니다.
    session.rollback()
    if isinstance(target, VerifyResult):
        result = target
    else:
        fingerprint = compute_fingerprint(target)
        view = get_version(session, project.id, version_id)
        if view is None:
            raise HTTPException(status_code=404, detail="데이터 버전을 찾을 수 없습니다.")
        result = apply_fingerprint(session, view.version, fingerprint)
        session.commit()
    query = urlencode({"verify": result.value})
    return RedirectResponse(f"/projects/{project.id}/datasets?{query}#version-{version_id}", status_code=303)


# ---------------------------------------------------------------- 파일


def _file_or_404(session: Session, project: Project, short_id: str):
    record = get_file(session, project.id, short_id)
    if record is None:
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
    return record


@router.get("/files/{short_id}", response_class=HTMLResponse)
def file_page(request: Request, project_id: int, short_id: str, session: SessionDep) -> HTMLResponse:
    project = project_or_404(session, project_id)
    record = _file_or_404(session, project, short_id)
    lineage = file_lineage(session, record)
    data_dir = request.app.state.settings.data_dir
    missing = bool(record.rel_path) and not blob_exists(data_dir, record)
    kind = "none" if missing or not record.rel_path else preview_kind(record)
    preview = text_preview(data_dir, record) if kind == "text" else None
    context = project_context(session, project, path=lineage.path,
                              tail=[Crumb(f"{record.short_id} {record.original_name or record.uri}")],
                              section="materials")
    return templates.TemplateResponse(
        request, "file_detail.html",
        {**context, "record": record, "lineage": lineage, "preview_kind": kind, "preview": preview,
         "missing": missing,
         "kind_labels": KIND_LABELS, "size": format_size(record.size_bytes)},
    )


@router.get("/files/{short_id}/content")
def file_content(request: Request, project_id: int, short_id: str, session: SessionDep) -> Response:
    project = project_or_404(session, project_id)
    record = _file_or_404(session, project, short_id)
    if not record.rel_path:
        raise HTTPException(status_code=404, detail="외부 참조 파일은 앱이 보관하지 않습니다.")
    try:
        path = blob_path(request.app.state.settings.data_dir, record.rel_path)
    except ValueError:
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="저장된 파일이 없습니다. 백업에서 복구하세요.")
    kind = preview_kind(record)
    if kind == "image":
        media_type, disposition = record.mime, "inline"
    elif kind == "text":
        media_type, disposition = "text/plain; charset=utf-8", "inline"
    else:  # SVG, HTML, PDF 등은 브라우저에서 바로 열지 않고 내려받게 합니다.
        media_type, disposition = "application/octet-stream", "attachment"
    return FileResponse(path, media_type=media_type, filename=record.original_name or record.short_id,
                        content_disposition_type=disposition, headers=FILE_HEADERS)
