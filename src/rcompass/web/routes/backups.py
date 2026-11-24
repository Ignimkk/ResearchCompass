"""백업 화면: 지금 백업, 백업 목록, 내려받기.

복구는 명령줄(`rcompass restore`)에서만 합니다. 웹앱이 zip 업로드를 받아 데이터를 덮어쓰지 않게 하고,
실행 중인 서버의 DB 파일을 바꾸는 일도 피하기 위해서입니다.
"""

from __future__ import annotations

import zipfile
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from rcompass.services.backup import (
    BACKUP_NAME_RE,
    BackupError,
    Manifest,
    backup_file,
    backups_dir,
    create_backup,
    list_backups,
    read_manifest,
)
from rcompass.services.files import format_size
from rcompass.web.templating import templates

router = APIRouter()


def _manifest_of(request: Request, name: str) -> Manifest | None:
    path = backup_file(request.app.state.settings.data_dir, name)
    if path is None:
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            return read_manifest(archive)
    except (BackupError, zipfile.BadZipFile, OSError):
        return None


def _render(request: Request, *, created: str = "", error: str = "", status_code: int = 200) -> HTMLResponse:
    data_dir = request.app.state.settings.data_dir
    created = created if BACKUP_NAME_RE.match(created) else ""
    return templates.TemplateResponse(request, "backups.html", {
        "backups": list_backups(data_dir),
        "backups_dir": backups_dir(data_dir),
        "data_dir": data_dir,
        "created": created,
        "manifest": _manifest_of(request, created) if created else None,
        "error": error,
        "format_size": format_size,
    }, status_code=status_code)


@router.get("/backups", response_class=HTMLResponse)
def backups_page(request: Request, created: str = "") -> HTMLResponse:
    return _render(request, created=created)


@router.post("/backups")
async def make_backup(request: Request) -> Response:
    data_dir = request.app.state.settings.data_dir
    try:
        # VACUUM INTO와 파일 복사는 오래 걸릴 수 있으므로 이벤트 루프 밖에서 합니다.
        result = await run_in_threadpool(create_backup, data_dir)
    except BackupError as error:
        return _render(request, error=str(error), status_code=500)
    return RedirectResponse(f"/backups?{urlencode({'created': result.path.name})}", status_code=303)


@router.get("/backups/{name}")
def download_backup(request: Request, name: str) -> Response:
    path = backup_file(request.app.state.settings.data_dir, name)
    if path is None:
        raise HTTPException(status_code=404, detail="백업 파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type="application/zip", filename=name,
                        headers={"X-Content-Type-Options": "nosniff"})
