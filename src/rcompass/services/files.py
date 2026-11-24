"""첨부 파일 저장 (계획서 4절 '자료와 재현성').

- 작은 첨부 파일은 데이터 디렉터리의 ``files/sha256/ab/<sha256>``에 내용 주소 방식으로 저장합니다.
  같은 내용은 한 번만 저장되고, 사용자가 준 파일 이름은 경로에 쓰지 않습니다(경로 조작 방지).
- 대용량 데이터는 복사하지 않고 위치와 지문(sha256)만 기록합니다(데이터 버전의 외부 참조).
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import BinaryIO
from urllib.parse import urlsplit
from urllib.request import url2pathname

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from rcompass.ids import next_short_id
from rcompass.models import Experiment, FileKind, FileObject, Project, Run, StorageKind, file_derivations
from rcompass.services import ValidationError

CHUNK = 1024 * 1024
BLOB_PREFIX = "files/sha256"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
KIND_LABELS = {
    FileKind.FIGURE: "그래프·그림",
    FileKind.TABLE: "표",
    FileKind.CONFIG: "설정",
    FileKind.LOG: "로그",
    FileKind.DATA: "데이터",
    FileKind.DIAGRAM: "구조도",
    FileKind.OTHER: "기타",
}
UPLOAD_KINDS = [FileKind.FIGURE, FileKind.TABLE, FileKind.CONFIG, FileKind.LOG, FileKind.OTHER]

# 브라우저에서 바로 보여줘도 안전한 형식. 나머지(SVG, HTML, PDF 등)는 다운로드로만 내려줍니다.
INLINE_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_EXTENSIONS = {".txt", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
                   ".log", ".md", ".py", ".sh"}
PREVIEW_BYTES = 64 * 1024
PREVIEW_LINES = 200


class FileTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"파일이 {limit // (1024 * 1024)}MB보다 큽니다.")
        self.limit = limit


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    size: int
    rel_path: str


def blob_rel_path(digest: str) -> str:
    return f"{BLOB_PREFIX}/{digest[:2]}/{digest}"


def blob_path(data_dir: Path, rel_path: str) -> Path:
    """DB에 적힌 상대 경로를 실제 경로로 바꿉니다. 저장소 밖을 가리키면 거부합니다."""
    parts = rel_path.split("/")
    if (len(parts) != 4 or "/".join(parts[:2]) != BLOB_PREFIX or not SHA256_RE.match(parts[3])
            or parts[2] != parts[3][:2]):
        raise ValueError("첨부 파일 경로가 올바르지 않습니다.")
    return data_dir.joinpath(*parts)


def store_stream(data_dir: Path, source: BinaryIO, max_bytes: int) -> StoredBlob:
    """스트림을 임시 파일로 받으면서 해시를 계산하고, 상한을 넘으면 즉시 중단합니다."""
    tmp_dir = data_dir / "files" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with tmp_path.open("wb") as out:
            while chunk := source.read(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise FileTooLarge(max_bytes)
                digest.update(chunk)
                out.write(chunk)
        sha = digest.hexdigest()
        rel = blob_rel_path(sha)
        target = blob_path(data_dir, rel)
        if target.exists():
            tmp_path.unlink()  # 같은 내용이 이미 있음
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, target)
        return StoredBlob(sha, size, rel)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def discard_if_unreferenced(session: Session, data_dir: Path, blob: StoredBlob) -> None:
    """저장에 실패해 되돌린 뒤, 어떤 파일 기록도 가리키지 않는 내용을 지웁니다(고아 파일 방지)."""
    used = session.scalars(select(FileObject.id).where(FileObject.sha256 == blob.sha256).limit(1)).first()
    if used is None:
        blob_path(data_dir, blob.rel_path).unlink(missing_ok=True)


def safe_original_name(name: str | None) -> str:
    """경로 부분과 제어 문자를 뺀 표시용 파일 이름."""
    base = PurePath((name or "").replace("\\", "/")).name
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()[:200]
    return base or "file"


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def add_file(
    session: Session,
    project: Project,
    blob: StoredBlob,
    *,
    original_name: str,
    kind: FileKind = FileKind.OTHER,
    caption: str | None = None,
    experiment: Experiment | None = None,
    run: Run | None = None,
    derived_from_version_ids: list[int] | None = None,
) -> FileObject:
    """저장한 내용을 파일 기록으로 남깁니다.

    기록은 매번 새로 만듭니다(같은 내용이라도 실행·종류·설명이 다를 수 있음). 디스크의 내용은
    sha256 주소라서 같은 내용이면 한 번만 저장됩니다.
    """
    if run is not None and (experiment is None or run.experiment_id != experiment.id):
        raise ValidationError({"run": "실행은 이 실험의 실행이어야 합니다."})
    name = safe_original_name(original_name)
    record = FileObject(
        project_id=project.id,
        short_id=next_short_id(session, project.id, "F"),
        kind=kind,
        storage=StorageKind.ATTACHED,
        sha256=blob.sha256,
        rel_path=blob.rel_path,
        original_name=name,
        mime=guess_mime(name),
        size_bytes=blob.size,
        caption=(caption or "").strip() or None,
        produced_by_experiment_id=experiment.id if experiment else None,
        produced_by_run_id=run.id if run else None,
    )
    session.add(record)
    session.flush()
    for version_id in dict.fromkeys(derived_from_version_ids or []):
        session.execute(insert(file_derivations).values(file_id=record.id, dataset_version_id=version_id))
    session.flush()
    return record


def get_file(session: Session, project_id: int, short_id: str) -> FileObject | None:
    return session.scalars(
        select(FileObject).where(FileObject.project_id == project_id, FileObject.short_id == short_id)
    ).one_or_none()


def list_files(session: Session, project_id: int, *, experiment: Experiment | None = None) -> list[FileObject]:
    query = select(FileObject).where(FileObject.project_id == project_id)
    if experiment is not None:
        query = query.where(FileObject.produced_by_experiment_id == experiment.id)
    return list(session.scalars(query.order_by(FileObject.id)))


# ---------------------------------------------------------------- 보기


def preview_kind(record: FileObject) -> str:
    """'image'(바로 보기), 'text'(앞부분 미리보기), 'none'(다운로드만)."""
    if record.mime in INLINE_IMAGE_TYPES:
        return "image"
    if PurePath(record.original_name or "").suffix.lower() in TEXT_EXTENSIONS:
        return "text"
    return "none"


def blob_exists(data_dir: Path, record: FileObject) -> bool:
    if not record.rel_path:
        return False
    try:
        return blob_path(data_dir, record.rel_path).is_file()
    except ValueError:
        return False


def text_preview(data_dir: Path, record: FileObject) -> tuple[str, bool] | None:
    """앞부분 텍스트와 잘렸는지 여부. 저장된 파일을 읽을 수 없으면 None."""
    try:
        with blob_path(data_dir, record.rel_path).open("rb") as handle:
            head = handle.read(PREVIEW_BYTES + 1)
    except (ValueError, OSError):
        return None
    truncated = len(head) > PREVIEW_BYTES
    lines = head[:PREVIEW_BYTES].decode("utf-8", errors="replace").splitlines()
    if len(lines) > PREVIEW_LINES:
        lines, truncated = lines[:PREVIEW_LINES], True
    return "\n".join(lines), truncated


def format_size(size: int | None) -> str:
    if size is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


# ---------------------------------------------------------------- 외부 참조 지문


@dataclass(frozen=True)
class Fingerprint:
    sha256: str
    size: int


def local_path_of(uri: str) -> Path | None:
    """지문을 계산할 수 있는 로컬·NAS 절대 경로면 Path, 아니면 None.

    - ``C:/data/x``, ``/data/x``, UNC 경로(``//nas/share/x``), ``~/x`` 같은 절대 경로
    - ``file:///C:/data/x``, ``file://nas/share/x``(UNC)
    URL(http, s3, gs, hf 등)과 상대 경로는 None입니다. 상대 경로는 서버의 현재 폴더(소스 저장소일 수 있음)를
    기준으로 엉뚱한 파일을 읽게 되므로 계산하지 않습니다.
    """
    text = (uri or "").strip()
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme == "file":
        path = url2pathname(parts.path)
        if parts.netloc and parts.netloc.lower() != "localhost":
            unc_prefix = "\\\\" if os.name == "nt" else "//"  # 실제 값: \\ (Windows) 또는 //
            path = f"{unc_prefix}{parts.netloc}{path}"
        candidate = Path(path)
    elif scheme == "" or (len(scheme) == 1 and os.name == "nt"):  # 드라이브 문자(C:)는 scheme처럼 보임
        candidate = Path(text).expanduser()
    else:
        return None
    return candidate if candidate.is_absolute() else None


def fingerprint_file(path: Path) -> Fingerprint:
    """사용자가 요청할 때만, 읽기 전용으로 sha256을 계산합니다."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return Fingerprint(digest.hexdigest(), size)
