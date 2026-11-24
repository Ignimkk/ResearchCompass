"""백업과 복구 (계획서 5절 '백업과 복구').

백업 zip의 내용은 세 가지뿐입니다.
- ``manifest.json``: 앱 버전, 스키마 버전, 표별 레코드 수, DB와 파일별 sha256·크기, 외부 참조 목록
- ``db.sqlite``: ``VACUUM INTO``로 만든 일관된 스냅샷(서버가 켜져 있어도 됩니다)
- ``files/sha256/<앞 2자리>/<sha256>``: 기록이 가리키는 첨부 파일 내용

외부 참조(대용량 데이터)는 위치와 지문만 manifest에 남기고 내용은 넣지 않습니다.

복구는 zip을 풀지(extractall) 않습니다. manifest에 적힌 이름만 하나씩 읽고, 저장 경로는
sha256으로 직접 만들며, 크기와 sha256이 manifest와 다르면 중단합니다(zip slip, 압축 폭탄 방지).
새 데이터를 임시 폴더에 모두 검증한 뒤에야 기존 데이터와 바꿉니다.

백업과 복구는 ORM 모델 대신 SQL로 스냅샷을 읽습니다. 이전 스키마 DB(업데이트 전 자동 백업, 이전 버전 백업
복구)도 같은 코드로 다루기 위해서입니다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
import zipfile
import zlib
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from rcompass import __version__
from rcompass.config import DB_FILENAME
from rcompass.services.files import BLOB_PREFIX, CHUNK, SHA256_RE, blob_path, blob_rel_path, local_path_of

FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DB_MEMBER = "db.sqlite"
BACKUP_DIRNAME = "backups"
BACKUP_NAME_RE = re.compile(r"rcompass-backup-\d{8}-\d{4}(?:-\d{1,3})?\.zip")  # 항상 fullmatch로 씁니다
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
DB_SIDECARS = ("-wal", "-shm", "-journal")


class BackupError(Exception):
    """사용자에게 그대로 보여줄 한국어 메시지를 가진 오류."""


# ---------------------------------------------------------------- manifest


@dataclass(frozen=True)
class FileEntry:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ExternalRef:
    project_id: int
    project_title: str
    ref: str  # 예: "DS1 eval-100 v1.0"
    uri: str
    sha256: str | None = None
    size: int | None = None


@dataclass
class Manifest:
    app_version: str
    schema_revision: str
    created_at: str  # UTC ISO 8601
    record_counts: dict[str, int]
    db: FileEntry
    files: list[FileEntry]
    external_refs: list[ExternalRef]
    warnings: list[str] = field(default_factory=list)
    format: int = FORMAT_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n"

    @property
    def total_records(self) -> int:
        return sum(self.record_counts.values())

    @property
    def created_local(self) -> str:
        """사람이 읽을 백업 시각(이 컴퓨터의 시간대)."""
        try:
            return datetime.fromisoformat(self.created_at).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return self.created_at

    @property
    def total_bytes(self) -> int:
        return self.db.size + sum(f.size for f in self.files)


def _bad(detail: str) -> BackupError:
    return BackupError(f"백업의 manifest.json이 올바르지 않습니다: {detail}")


def _entry(raw: object, what: str) -> FileEntry:
    if not isinstance(raw, dict):
        raise _bad(f"{what} 항목 형식")
    path, sha, size = raw.get("path"), raw.get("sha256"), raw.get("size")
    if not (isinstance(path, str) and isinstance(sha, str) and SHA256_RE.fullmatch(sha)
            and isinstance(size, int) and not isinstance(size, bool) and size >= 0):
        raise _bad(f"{what} 항목 {path!r}")
    return FileEntry(path, sha, size)


def parse_manifest(text: str) -> Manifest:
    """manifest를 엄격하게 읽습니다. 파일 경로는 sha256에서 만든 경로와 정확히 같아야 합니다."""
    try:
        raw = json.loads(text)
    except ValueError as error:
        raise _bad("JSON이 아닙니다") from error
    if not isinstance(raw, dict):
        raise _bad("최상위가 객체가 아닙니다")
    if raw.get("format") != FORMAT_VERSION:
        raise BackupError(f"이 앱이 모르는 백업 형식({raw.get('format')!r})입니다. 앱을 업데이트하세요.")
    revision, created, counts = raw.get("schema_revision"), raw.get("created_at"), raw.get("record_counts")
    if not isinstance(revision, str) or not isinstance(created, str):
        raise _bad("schema_revision, created_at")
    if not isinstance(counts, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0 for k, v in counts.items()):
        raise _bad("record_counts")
    db = _entry(raw.get("db"), "db")
    if db.path != DB_MEMBER:
        raise _bad("db 경로")
    files_raw = raw.get("files")
    if not isinstance(files_raw, list):
        raise _bad("files")
    files = [_entry(item, "files") for item in files_raw]
    for entry in files:
        if entry.path != blob_rel_path(entry.sha256):
            raise _bad(f"파일 경로 {entry.path!r}")
    if len({f.path for f in files}) != len(files):
        raise _bad("같은 파일이 두 번 적혀 있습니다")
    refs_raw = raw.get("external_refs", [])
    if not isinstance(refs_raw, list):
        raise _bad("external_refs")
    refs = []
    for item in refs_raw:
        try:
            refs.append(ExternalRef(int(item["project_id"]), str(item["project_title"]), str(item["ref"]),
                                    str(item["uri"]), item.get("sha256"), item.get("size")))
        except (KeyError, TypeError, ValueError) as error:
            raise _bad("external_refs 항목") from error
    warnings_raw = raw.get("warnings", [])
    if not isinstance(warnings_raw, list):
        raise _bad("warnings")
    warnings = [w for w in warnings_raw if isinstance(w, str)]
    app_version = raw.get("app_version") if isinstance(raw.get("app_version"), str) else "?"
    return Manifest(app_version, revision, created, counts, db, files, refs, warnings)


# ---------------------------------------------------------------- SQLite 스냅샷 읽기


@contextmanager
def _sqlite(path: Path) -> Iterator[sqlite3.Connection]:
    # isolation_level=None: 드라이버가 BEGIN을 끼워 넣지 않게 합니다(VACUUM은 트랜잭션 안에서 못 돌림).
    with closing(sqlite3.connect(str(path), timeout=10, isolation_level=None)) as connection:
        yield connection


def _tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [name for (name,) in rows if name != "alembic_version"]


def record_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: connection.execute('SELECT COUNT(*) FROM "{}"'.format(table.replace('"', '""'))).fetchone()[0]
        for table in _tables(connection)
    }


def _revision(connection: sqlite3.Connection) -> str:
    try:
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.DatabaseError:
        rows = []
    if len(rows) != 1:
        raise BackupError("DB에 마이그레이션 기록이 없습니다. 연구 나침반이 만든 DB가 아닐 수 있습니다.")
    return rows[0][0]


def _attached_files(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(rel_path, sha256, 표시용 이름). 같은 내용을 가리키는 기록이 여럿이면 첫 기록 이름만 씁니다."""
    rows = connection.execute(
        "SELECT f.rel_path, f.sha256, p.id, f.short_id FROM file_objects f JOIN projects p ON p.id = f.project_id "
        "WHERE f.storage = 'attached' ORDER BY f.id"
    ).fetchall()
    seen: dict[str, tuple[str, str, str]] = {}
    for rel_path, sha, project_id, short_id in rows:
        seen.setdefault(rel_path, (rel_path, sha, f"프로젝트 {project_id}의 {short_id}"))
    return list(seen.values())


def _external_refs(connection: sqlite3.Connection) -> list[ExternalRef]:
    refs = [
        ExternalRef(pid, title, f"{ds_id} {ds_name} {label}", uri, sha, size)
        for pid, title, ds_id, ds_name, label, uri, sha, size in connection.execute(
            "SELECT p.id, p.title, d.short_id, d.name, v.version_label, v.external_uri, v.sha256, v.size_bytes "
            "FROM dataset_versions v JOIN datasets d ON d.id = v.dataset_id JOIN projects p ON p.id = d.project_id "
            "WHERE v.storage = 'external' ORDER BY p.id, d.id, v.id"
        )
    ]
    refs += [
        ExternalRef(pid, title, f"{short_id} {name or ''}".strip(), uri, sha, size)
        for pid, title, short_id, name, uri, sha, size in connection.execute(
            "SELECT p.id, p.title, f.short_id, f.original_name, f.uri, f.sha256, f.size_bytes "
            "FROM file_objects f JOIN projects p ON p.id = f.project_id WHERE f.storage = 'external' ORDER BY f.id"
        )
    ]
    return refs


# ---------------------------------------------------------------- 공통 입출력


def _hash_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def backups_dir(data_dir: Path) -> Path:
    return data_dir / BACKUP_DIRNAME


def _reserve_name(directory: Path, now: datetime) -> Path:
    """같은 분에 만든 백업끼리 덮어쓰지 않도록 빈 파일을 배타적으로 만들어 이름을 잡아 둡니다."""
    stem = f"rcompass-backup-{now:%Y%m%d-%H%M}"
    for counter in range(1, 1000):
        candidate = directory / (f"{stem}.zip" if counter == 1 else f"{stem}-{counter}.zip")
        try:
            with candidate.open("xb"):
                return candidate
        except FileExistsError:
            continue
    raise BackupError("같은 분에 만든 백업이 너무 많습니다. 잠시 뒤 다시 시도하세요.")


def has_data(data_dir: Path) -> bool:
    """복구하면 덮어쓰게 될 기록(레코드가 하나라도 있는 DB나 첨부 파일)이 있는지.

    `serve`가 만든 빈 스키마 DB는 기록이 없으므로 그대로 바꿔도 됩니다.
    """
    db = data_dir / DB_FILENAME
    if db.exists() and db.stat().st_size > 0:
        try:
            with _sqlite(db) as connection:
                if any(record_counts(connection).values()):
                    return True
        except sqlite3.DatabaseError:
            return True  # 읽을 수 없는 파일도 덮어쓰지 않습니다.
    blobs = data_dir / BLOB_PREFIX
    return blobs.is_dir() and any(p.is_file() for p in blobs.rglob("*"))


# ---------------------------------------------------------------- 백업


@dataclass(frozen=True)
class BackupResult:
    path: Path
    manifest: Manifest


def create_backup(data_dir: Path, *, dest_dir: Path | None = None, now: datetime | None = None) -> BackupResult:
    """데이터 디렉터리를 zip 하나로 백업합니다. 서버가 켜져 있어도 일관된 스냅샷을 만듭니다."""
    db = data_dir / DB_FILENAME
    if not db.is_file() or db.stat().st_size == 0:
        raise BackupError(f"{db}에 기록이 없어 백업할 것이 없습니다.")
    dest = dest_dir or backups_dir(data_dir)
    try:
        return _create_backup(data_dir, db, dest, now or datetime.now().astimezone())
    except sqlite3.DatabaseError as error:
        raise BackupError(f"DB를 읽지 못했습니다: {error}") from error
    except OSError as error:
        raise BackupError(f"백업 파일을 쓰지 못했습니다({dest}): {error}") from error


def _create_backup(data_dir: Path, db: Path, dest: Path, now: datetime) -> BackupResult:
    dest.mkdir(parents=True, exist_ok=True)
    work = dest / f".tmp-{uuid.uuid4().hex}"
    work.mkdir()
    target: Path | None = None
    try:
        snapshot = work / DB_MEMBER
        try:
            with _sqlite(db) as connection:
                connection.execute("VACUUM INTO ?", (str(snapshot),))
        except sqlite3.DatabaseError as error:
            raise BackupError(f"DB 스냅샷을 만들지 못했습니다: {error}") from error
        with _sqlite(snapshot) as connection:
            revision = _revision(connection)
            counts = record_counts(connection)
            attached = _attached_files(connection)
            externals = _external_refs(connection)
        if not counts:
            raise BackupError("DB에 표가 없어 백업할 것이 없습니다.")

        warnings: list[str] = []
        files: list[FileEntry] = []
        for rel_path, expected, label in attached:
            try:
                source = blob_path(data_dir, rel_path)
            except ValueError:
                warnings.append(f"{label}: 첨부 경로가 올바르지 않아 백업에서 뺐습니다.")
                continue
            if not source.is_file():
                warnings.append(f"{label}: 첨부 파일 내용이 디스크에 없어 백업에서 뺐습니다.")
                continue
            actual, size = _hash_file(source)
            if actual != expected or rel_path != blob_rel_path(actual):
                warnings.append(f"{label}: 첨부 파일 내용이 지문과 달라(손상) 백업에서 뺐습니다.")
                continue
            files.append(FileEntry(rel_path, actual, size))

        db_sha, db_size = _hash_file(snapshot)
        manifest = Manifest(
            app_version=__version__,
            schema_revision=revision,
            created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            record_counts=counts,
            db=FileEntry(DB_MEMBER, db_sha, db_size),
            files=files,
            external_refs=externals,
            warnings=warnings,
        )
        part = work / "backup.zip.part"
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, DB_MEMBER)
            for entry in files:
                archive.write(blob_path(data_dir, entry.path), entry.path)
            archive.writestr(MANIFEST_NAME, manifest.to_json())
        target = _reserve_name(dest, now)
        os.replace(part, target)
        result = BackupResult(target, manifest)
        target = None
        return result
    finally:
        if target is not None:  # 이름만 잡아 두고 실패하면 빈 파일을 지웁니다.
            target.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


@dataclass(frozen=True)
class BackupInfo:
    name: str
    size: int
    modified: datetime


def list_backups(data_dir: Path) -> list[BackupInfo]:
    directory = backups_dir(data_dir)
    if not directory.is_dir():
        return []
    items = []
    for path in directory.iterdir():
        if BACKUP_NAME_RE.fullmatch(path.name) and path.is_file():
            stat = path.stat()
            items.append(BackupInfo(path.name, stat.st_size, datetime.fromtimestamp(stat.st_mtime).astimezone()))
    return sorted(items, key=lambda b: (b.modified, b.name), reverse=True)


def backup_file(data_dir: Path, name: str) -> Path | None:
    """내려받기용. 이름 규칙에 맞는 백업 폴더 안의 파일만 돌려줍니다."""
    if not BACKUP_NAME_RE.fullmatch(name):
        return None
    path = backups_dir(data_dir) / name
    return path if path.is_file() else None


# ---------------------------------------------------------------- 복구


ExternalStatus = Literal["ok", "missing", "mismatch", "unchecked", "remote", "present"]
EXTERNAL_LABELS: dict[str, str] = {
    "ok": "접근 가능 (지문 일치)",
    "present": "접근 가능",
    "missing": "없음",
    "mismatch": "지문 불일치",
    "unchecked": "확인 안 함 (URL 등 로컬 경로가 아님)",
    "remote": "확인 안 함 (네트워크 경로, --check-network로 확인)",
}


def _is_network_path(path: Path) -> bool:
    """UNC(``\\\\host\\share``)나 ``//host/share`` 경로인지. Windows는 이런 경로에 접근할 때 원격 호스트로 인증을 보냅니다."""
    text = str(path)
    return text.startswith("\\\\") or text.startswith("//")


@dataclass(frozen=True)
class ExternalCheck:
    ref: ExternalRef
    status: ExternalStatus

    @property
    def label(self) -> str:
        return EXTERNAL_LABELS[self.status]


def check_external(ref: ExternalRef, *, verify_hash: bool = True, check_network: bool = False) -> ExternalCheck:
    """외부 참조가 이 컴퓨터에서 보이는지 확인합니다. 원본은 읽기만 합니다.

    백업 zip은 다른 사람이 만든 것일 수 있으므로, 네트워크 경로는 ``check_network``일 때만 접근합니다.
    조작된 manifest가 공격자 호스트의 UNC 경로를 넣어 Windows 인증(NTLM)을 보내게 하는 것을 막습니다.
    """
    path = local_path_of(ref.uri)
    if path is None:
        return ExternalCheck(ref, "unchecked")
    if _is_network_path(path) and not check_network:
        return ExternalCheck(ref, "remote")
    try:
        if not path.is_file():
            return ExternalCheck(ref, "missing")
        if ref.size is not None and path.stat().st_size != ref.size:
            return ExternalCheck(ref, "mismatch")
        if ref.sha256 and verify_hash:
            actual, _ = _hash_file(path)
            return ExternalCheck(ref, "ok" if actual == ref.sha256 else "mismatch")
    except OSError:
        return ExternalCheck(ref, "missing")
    return ExternalCheck(ref, "present")


@dataclass(frozen=True)
class RestoreResult:
    manifest: Manifest
    pre_restore_backup: Path | None
    migrated: bool
    warnings: list[str]
    external: list[ExternalCheck]


def read_manifest(archive: zipfile.ZipFile) -> Manifest:
    try:
        info = archive.getinfo(MANIFEST_NAME)
    except KeyError as error:
        raise BackupError("연구 나침반 백업이 아닙니다(manifest.json이 없습니다).") from error
    if info.file_size > MAX_MANIFEST_BYTES:
        raise _bad("너무 큽니다")
    try:
        with archive.open(info) as handle:
            data = handle.read(MAX_MANIFEST_BYTES + 1)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as error:
        raise BackupError(f"manifest.json을 풀 수 없습니다. 손상된 백업입니다. ({error})") from error
    if len(data) > MAX_MANIFEST_BYTES:
        raise _bad("너무 큽니다")
    try:
        return parse_manifest(data.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise _bad("UTF-8이 아닙니다") from error


def _check_members(archive: zipfile.ZipFile, manifest: Manifest) -> None:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise BackupError("백업 zip에 같은 이름의 항목이 여러 개 있습니다. 손상되었거나 조작된 파일입니다.")
    expected = {MANIFEST_NAME, DB_MEMBER, *(f.path for f in manifest.files)}
    missing = sorted(expected - set(names))
    extra = sorted(set(names) - expected)
    if missing:
        raise BackupError(f"백업에 manifest가 적은 항목이 없습니다: {', '.join(missing[:5])}")
    if extra:
        raise BackupError(f"백업에 알 수 없는 항목이 있습니다: {', '.join(extra[:5])}")


def _extract_verified(archive: zipfile.ZipFile, entry: FileEntry, target: Path) -> None:
    """한 항목을 target에 씁니다. manifest의 크기를 넘으면 바로 멈추고, 끝나면 sha256을 확인합니다."""
    target.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    try:
        with archive.open(entry.path) as source, target.open("wb") as out:
            while chunk := source.read(CHUNK):
                size += len(chunk)
                if size > entry.size:
                    raise BackupError(f"{entry.path}의 크기가 manifest와 다릅니다. 손상되었거나 조작된 백업입니다.")
                digest.update(chunk)
                out.write(chunk)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as error:
        raise BackupError(f"{entry.path}을(를) 풀 수 없습니다. 손상된 백업입니다. ({error})") from error
    if size != entry.size or digest.hexdigest() != entry.sha256:
        raise BackupError(f"{entry.path}의 지문(sha256)이 manifest와 다릅니다. 손상된 백업입니다.")


def _verify_db(db: Path, manifest: Manifest) -> list[str]:
    try:
        with _sqlite(db) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise BackupError(f"백업의 DB가 손상되었습니다: {integrity}")
            if _revision(connection) != manifest.schema_revision:
                raise BackupError("DB의 스키마 버전이 manifest와 다릅니다.")
            counts = record_counts(connection)
            attached = _attached_files(connection)
    except sqlite3.DatabaseError as error:
        raise BackupError(f"백업의 DB를 읽을 수 없습니다: {error}") from error
    if counts != manifest.record_counts:
        diff = sorted(t for t in set(counts) | set(manifest.record_counts)
                      if counts.get(t) != manifest.record_counts.get(t))
        raise BackupError(f"레코드 수가 manifest와 다릅니다: {', '.join(diff[:5])}")
    present = {f.path for f in manifest.files}
    return [f"{label}: 백업에 첨부 파일 내용이 없습니다. 이 파일은 열 수 없습니다."
            for rel_path, _, label in attached if rel_path not in present]


def _known_revisions() -> set[str]:
    from rcompass.db import _alembic_config
    from alembic.script import ScriptDirectory

    return {r.revision for r in ScriptDirectory.from_config(_alembic_config()).walk_revisions()}


class _SwapStranded(BackupError):
    """바꾸다 실패했고 되돌리기도 실패한 경우. 임시 폴더를 지우지 않고 위치를 알려줍니다."""


def _swap_in(data_dir: Path, stage: Path, keep_old: Path | None) -> None:
    """임시 폴더(stage)의 DB와 files/를 데이터 디렉터리로 옮깁니다. 중간에 실패하면 원래대로 되돌립니다.

    ``keep_old``가 있으면 기존 파일을 그 폴더에 남기고, 없으면 바꾼 뒤 지웁니다.
    """
    names = [DB_FILENAME, *(DB_FILENAME + s for s in DB_SIDECARS), "files"]
    trash = keep_old or data_dir / f".replaced-{uuid.uuid4().hex}"
    trash.mkdir(parents=True)
    moved: list[str] = []
    placed: list[str] = []
    try:
        for name in names:
            if (data_dir / name).exists():
                os.replace(data_dir / name, trash / name)
                moved.append(name)
        for name in (DB_FILENAME, "files"):
            if (stage / name).exists():
                os.replace(stage / name, data_dir / name)
                placed.append(name)
    except OSError as error:
        stuck = []
        for name in placed:
            try:
                os.replace(data_dir / name, stage / name)
            except OSError:
                stuck.append(name)
        for name in moved:
            try:
                os.replace(trash / name, data_dir / name)
            except OSError:
                stuck.append(name)
        if stuck:
            raise _SwapStranded(
                f"데이터를 바꾸다 실패했고 원래대로 되돌리지도 못했습니다({', '.join(stuck)}). "
                f"원래 데이터는 {trash}에, 복구할 데이터는 {stage}에 남겨 두었습니다. 서버를 끄고 두 폴더의 "
                f"{DB_FILENAME}과 files를 확인해 {data_dir}로 직접 옮기세요. ({error})"
            ) from error
        shutil.rmtree(trash, ignore_errors=True)
        raise BackupError(
            "기존 데이터 파일을 옮기지 못했습니다. 이 데이터 디렉터리를 쓰는 서버(rcompass serve)를 끄고 다시 실행하세요. "
            f"({error})"
        ) from error
    if keep_old is None:
        shutil.rmtree(trash, ignore_errors=True)


def restore_backup(
    zip_path: Path,
    data_dir: Path,
    *,
    force: bool = False,
    verify_external_hash: bool = True,
    check_network: bool = False,
    now: datetime | None = None,
    progress: Callable[[str], None] | None = None,
) -> RestoreResult:
    """백업 zip을 검증한 뒤 데이터 디렉터리로 복구합니다.

    기존 기록이 있으면 거부하고, ``force``이면 기존 데이터를 먼저 백업한 뒤 바꿉니다. 기존 DB가 손상되어
    zip으로 백업할 수 없으면 원래 파일을 ``backups/before-restore-*`` 폴더에 그대로 옮겨 둡니다.
    이전 스키마의 백업이면 복구한 DB를 최신 스키마로 올립니다.
    """
    try:
        return _restore(zip_path, data_dir, force=force, verify_external_hash=verify_external_hash,
                        check_network=check_network, now=now, say=progress or (lambda _message: None))
    except OSError as error:
        raise BackupError(f"복구하는 중 파일을 읽거나 쓰지 못했습니다: {error}") from error


def _restore(zip_path: Path, data_dir: Path, *, force: bool, verify_external_hash: bool, check_network: bool,
             now: datetime | None, say: Callable[[str], None]) -> RestoreResult:
    if not zip_path.is_file():
        raise BackupError(f"백업 파일 {zip_path}을(를) 찾을 수 없습니다.")
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as error:
        raise BackupError("zip 파일이 아니거나 손상되었습니다.") from error
    with archive:
        manifest = read_manifest(archive)
        if manifest.schema_revision not in _known_revisions():
            raise BackupError("이 앱보다 새 버전이 만든 백업입니다. 앱을 업데이트한 뒤 복구하세요(`git pull` 후 `uv sync`).")
        _check_members(archive, manifest)

        data_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(data_dir).free
        if manifest.total_bytes > free:
            raise BackupError(f"디스크 공간이 부족합니다(필요 {manifest.total_bytes:,}바이트, 남은 공간 {free:,}바이트).")

        pre_backup = None
        if has_data(data_dir):
            if not force:
                raise BackupError(
                    f"{data_dir}에 이미 기록이 있습니다. 빈 폴더를 --data-dir로 지정하거나, "
                    "기존 데이터를 백업한 뒤 바꾸려면 --force를 붙이세요."
                )
            say("기존 데이터를 먼저 백업합니다…")
            keep_old = None
            try:
                pre_backup = create_backup(data_dir, now=now).path
            except BackupError as error:
                stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
                keep_old = backups_dir(data_dir) / f"before-restore-{stamp}-{uuid.uuid4().hex[:6]}"
                say(f"기존 데이터를 zip으로 백업하지 못했습니다({error}). 원래 파일을 그대로 옮겨 둡니다: {keep_old}")
                pre_backup = keep_old
        else:
            keep_old = None

        stage = data_dir / f".restore-{uuid.uuid4().hex}"
        stage.mkdir()
        stranded = False
        try:
            say("백업 내용을 검증하며 꺼냅니다…")
            staged_db = stage / DB_FILENAME
            _extract_verified(archive, manifest.db, staged_db)
            for entry in manifest.files:
                _extract_verified(archive, entry, blob_path(stage, entry.path))
            warnings = _verify_db(staged_db, manifest)

            from rcompass.db import SchemaStatus, check_schema, create_db_engine, upgrade_to_head

            status = check_schema(staged_db)
            migrated = False
            if status is SchemaStatus.OUTDATED:
                say("이전 버전의 백업이라 스키마를 최신으로 올립니다…")
                engine = create_db_engine(staged_db)
                try:
                    upgrade_to_head(engine)
                finally:
                    engine.dispose()
                migrated = True
            elif status is not SchemaStatus.CURRENT:
                raise BackupError("백업의 DB 스키마를 확인할 수 없습니다.")
            _swap_in(data_dir, stage, keep_old)
        except _SwapStranded:
            stranded = True
            raise
        finally:
            if not stranded:
                shutil.rmtree(stage, ignore_errors=True)

    say("외부 참조를 확인합니다…")
    external = [check_external(ref, verify_hash=verify_external_hash, check_network=check_network)
                for ref in manifest.external_refs]
    return RestoreResult(manifest, pre_backup, migrated, warnings, external)

