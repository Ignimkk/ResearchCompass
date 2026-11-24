"""9단계: 백업과 복구 왕복, 변조된 백업 거부, 이전 버전 백업, 업데이트 전 자동 백업, 외부 참조 점검."""

from __future__ import annotations

import hashlib
import json
import os
import warnings
import zipfile
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from rcompass.cli import app as cli_app
from rcompass.config import Settings
from rcompass.db import SchemaStatus, _alembic_config, check_schema, create_db_engine, open_database, session_factory
from rcompass.example import EXAMPLE_TITLE, seed_example
from rcompass.services.backup import (
    BackupError,
    ExternalRef,
    check_external,
    create_backup,
    list_backups,
    restore_backup,
)
from rcompass.services.handoff import HandoffOptions, build_handoff
from rcompass.services.projects import get_project
from rcompass.web.app import create_app

BASE_URL = "http://127.0.0.1:8765"
NOW = datetime(2026, 11, 24, 9, 30)
runner = CliRunner()


def _seed(data_dir: Path) -> None:
    engine = open_database(data_dir / "rcompass.db")
    with session_factory(engine)() as session:
        seed_example(session, data_dir, max_bytes=50 * 1024 * 1024)
        session.commit()
    engine.dispose()


def _handoff(data_dir: Path) -> str:
    engine = open_database(data_dir / "rcompass.db")
    with session_factory(engine)() as session:
        text = build_handoff(session, get_project(session, 1), HandoffOptions(today=date(2026, 11, 24)))
    engine.dispose()
    return text


def _blobs(data_dir: Path) -> dict[str, str]:
    root = data_dir / "files" / "sha256"
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def source(tmp_path: Path) -> Path:
    data_dir = tmp_path / "source"
    _seed(data_dir)
    return data_dir


@pytest.fixture
def backup_zip(source: Path) -> Path:
    return create_backup(source, now=NOW).path


def _rewrite(original: Path, target: Path, mutate: Callable[[dict[str, bytes]], None]) -> Path:
    with zipfile.ZipFile(original) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    mutate(members)
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return target


def _manifest(members: dict[str, bytes]) -> dict:
    return json.loads(members["manifest.json"])


def _set_manifest(members: dict[str, bytes], manifest: dict) -> None:
    members["manifest.json"] = json.dumps(manifest).encode("utf-8")


# ---------------------------------------------------------------- 왕복


def test_backup_contents_and_name(source: Path, backup_zip: Path) -> None:
    assert backup_zip.parent == source / "backups"
    assert backup_zip.name == "rcompass-backup-20261124-0930.zip"
    with zipfile.ZipFile(backup_zip) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert {"manifest.json", "db.sqlite"} <= names and len(names) == 2 + 4  # 첨부 4개
    assert manifest["schema_revision"] == "0002" and manifest["record_counts"]["projects"] == 1
    assert manifest["record_counts"]["runs"] == 9 and manifest["warnings"] == []
    assert manifest["external_refs"] == [{
        "project_id": 1, "project_title": EXAMPLE_TITLE, "ref": "DS1 eval-100 v1.0",
        "uri": "D:/data/eval100_v1.jsonl", "sha256": None, "size": None}]
    # 같은 분에 다시 만들면 이름이 겹치지 않습니다.
    assert create_backup(source, now=NOW).path.name == "rcompass-backup-20261124-0930-2.zip"
    assert [b.name for b in list_backups(source)][0].startswith("rcompass-backup-20261124-0930")


def test_round_trip_restores_same_records_files_and_handoff(tmp_path: Path, source: Path, backup_zip: Path) -> None:
    target = tmp_path / "restored"
    result = restore_backup(backup_zip, target, verify_external_hash=True)
    assert result.pre_restore_backup is None and not result.migrated and result.warnings == []
    assert [c.label for c in result.external] == ["없음"]  # 예제의 외부 참조는 이 컴퓨터에 없음
    assert check_schema(target / "rcompass.db") is SchemaStatus.CURRENT
    assert _blobs(target) == _blobs(source)
    assert _handoff(target) == _handoff(source)  # 인계 Markdown이 바이트 단위로 같음
    assert not list(target.glob(".restore-*")) and not list(target.glob(".replaced-*"))

    with TestClient(create_app(Settings(data_dir=target)), base_url=BASE_URL) as client:
        home = client.get("/projects/1").text
        assert "11.0 ± 1.0 (n=3)" in home and "문단 요약 v1" in home
        image = client.get("/projects/1/files/F3/content")
        assert image.status_code == 200 and image.content == (
            Path(__file__).parents[1] / "src/rcompass/example/assets/wrong_types_E2.png").read_bytes()


def test_restore_refuses_existing_data_unless_forced(tmp_path: Path, source: Path, backup_zip: Path) -> None:
    other = tmp_path / "other"
    _seed(other)
    with pytest.raises(BackupError, match="이미 기록이 있습니다"):
        restore_backup(backup_zip, other)
    result = restore_backup(backup_zip, other, force=True, now=NOW)
    assert result.pre_restore_backup is not None and result.pre_restore_backup.parent == other / "backups"
    assert _handoff(other) == _handoff(source)
    # 강제 복구 전 백업으로 되돌릴 수도 있습니다.
    restore_backup(result.pre_restore_backup, other, force=True)
    assert check_schema(other / "rcompass.db") is SchemaStatus.CURRENT


def test_restore_over_fresh_empty_schema_needs_no_force(tmp_path: Path, backup_zip: Path) -> None:
    fresh = tmp_path / "fresh"
    open_database(fresh / "rcompass.db").dispose()  # serve가 만든 빈 DB
    restore_backup(backup_zip, fresh)
    assert "QA 평가" in _handoff(fresh)


def test_backup_reports_missing_attachment(tmp_path: Path, source: Path) -> None:
    victim = next(p for p in (source / "files" / "sha256").rglob("*") if p.is_file())
    victim.unlink()
    result = create_backup(source)
    assert len(result.manifest.files) == 3
    assert any("디스크에 없어" in w for w in result.manifest.warnings)
    restored = restore_backup(result.path, tmp_path / "r")
    assert any("백업에 첨부 파일 내용이 없습니다" in w for w in restored.warnings)


def test_backup_without_db_fails(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="백업할 것이 없습니다"):
        create_backup(tmp_path / "nothing")


# ---------------------------------------------------------------- 변조·손상된 백업


def _refused(tmp_path: Path, archive: Path, match: str) -> None:
    target = tmp_path / "target"
    with pytest.raises(BackupError, match=match):
        restore_backup(archive, target)
    assert not (target / "rcompass.db").exists()
    assert not list(target.glob(".restore-*"))
    assert not (tmp_path / "evil.txt").exists()


def test_zip_slip_member_is_rejected(tmp_path: Path, backup_zip: Path) -> None:
    bad = _rewrite(backup_zip, tmp_path / "slip.zip", lambda m: m.update({"../evil.txt": b"x"}))
    _refused(tmp_path, bad, "알 수 없는 항목")


def test_manifest_path_traversal_is_rejected(tmp_path: Path, backup_zip: Path) -> None:
    def mutate(members: dict[str, bytes]) -> None:
        manifest = _manifest(members)
        entry = manifest["files"][0]
        data = members.pop(entry["path"])
        entry["path"] = "../evil.txt"
        members["../evil.txt"] = data
        _set_manifest(members, manifest)

    _refused(tmp_path, _rewrite(backup_zip, tmp_path / "t.zip", mutate), "파일 경로")


def test_changed_file_content_is_rejected(tmp_path: Path, backup_zip: Path) -> None:
    def mutate(members: dict[str, bytes]) -> None:
        path = _manifest(members)["files"][0]["path"]
        data = bytearray(members[path])
        data[-1] ^= 0xFF
        members[path] = bytes(data)

    _refused(tmp_path, _rewrite(backup_zip, tmp_path / "t.zip", mutate), "sha256")


def test_oversized_member_stops_early(tmp_path: Path, backup_zip: Path) -> None:
    bad = _rewrite(backup_zip, tmp_path / "t.zip", lambda m: m.update({"db.sqlite": m["db.sqlite"] + b"\0" * 4096}))
    _refused(tmp_path, bad, "크기가 manifest와 다릅니다")


def test_record_count_mismatch_is_rejected(tmp_path: Path, backup_zip: Path) -> None:
    def mutate(members: dict[str, bytes]) -> None:
        manifest = _manifest(members)
        manifest["record_counts"]["runs"] = 99
        _set_manifest(members, manifest)

    _refused(tmp_path, _rewrite(backup_zip, tmp_path / "t.zip", mutate), "레코드 수")


def test_duplicate_member_names_are_rejected(tmp_path: Path, backup_zip: Path) -> None:
    bad = tmp_path / "dup.zip"
    with zipfile.ZipFile(backup_zip) as archive, zipfile.ZipFile(bad, "w") as out:
        for name in archive.namelist():
            out.writestr(name, archive.read(name))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out.writestr("db.sqlite", b"other")
    _refused(tmp_path, bad, "같은 이름")


def test_newer_or_unknown_backups_are_rejected(tmp_path: Path, backup_zip: Path) -> None:
    def newer_schema(members: dict[str, bytes]) -> None:
        manifest = _manifest(members)
        manifest["schema_revision"] = "ffff"
        _set_manifest(members, manifest)

    def newer_format(members: dict[str, bytes]) -> None:
        manifest = _manifest(members)
        manifest["format"] = 2
        _set_manifest(members, manifest)

    _refused(tmp_path, _rewrite(backup_zip, tmp_path / "a.zip", newer_schema), "새 버전이 만든 백업")
    _refused(tmp_path, _rewrite(backup_zip, tmp_path / "b.zip", newer_format), "모르는 백업 형식")
    _refused(tmp_path, _rewrite(backup_zip, tmp_path / "c.zip", lambda m: m.pop("manifest.json")), "manifest.json이 없습니다")
    (tmp_path / "plain.zip").write_bytes(b"not a zip")
    _refused(tmp_path, tmp_path / "plain.zip", "zip 파일이 아니거나")


# ---------------------------------------------------------------- 이전 버전


def _db_at(db_path: Path, revision: str) -> None:
    engine = create_db_engine(db_path)
    config = _alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
    engine.dispose()


def test_old_schema_backup_is_migrated_on_restore(tmp_path: Path) -> None:
    old = tmp_path / "old"
    _db_at(old / "rcompass.db", "0001")
    archive = create_backup(old).path
    result = restore_backup(archive, tmp_path / "new")
    assert result.migrated and result.manifest.schema_revision == "0001"
    assert check_schema(tmp_path / "new" / "rcompass.db") is SchemaStatus.CURRENT


def test_migrate_makes_backup_first(tmp_path: Path) -> None:
    _db_at(tmp_path / "rcompass.db", "0001")
    result = runner.invoke(cli_app, ["migrate", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "업데이트 전 백업:" in result.output
    [backup] = list_backups(tmp_path)
    with zipfile.ZipFile(tmp_path / "backups" / backup.name) as archive:
        assert json.loads(archive.read("manifest.json"))["schema_revision"] == "0001"
    assert check_schema(tmp_path / "rcompass.db") is SchemaStatus.CURRENT


# ---------------------------------------------------------------- 외부 참조


def test_external_reference_statuses(tmp_path: Path) -> None:
    data = tmp_path / "data.jsonl"
    data.write_bytes(b"hello")
    digest = hashlib.sha256(b"hello").hexdigest()

    def status(uri: str, sha: str | None = None, size: int | None = None, **kw) -> str:
        return check_external(ExternalRef(1, "p", "DS1", uri, sha, size), **kw).status

    assert status(str(data), digest, 5) == "ok"
    assert status(str(data)) == "present"
    assert status(str(data), "0" * 64) == "mismatch"
    assert status(str(data), size=6) == "mismatch"
    assert status(str(data), "0" * 64, verify_hash=False) == "present"
    assert status(str(tmp_path / "gone.jsonl")) == "missing"
    assert status("https://example.org/data.jsonl") == "unchecked"
    assert status("relative/data.jsonl") == "unchecked"
    assert data.read_bytes() == b"hello"  # 원본은 읽기만 함


def test_network_references_are_not_touched_unless_asked(monkeypatch) -> None:
    """조작된 manifest의 UNC 경로로 Windows가 원격 호스트에 인증을 보내지 않도록, 기본으로는 접근하지 않습니다."""
    touched: list[str] = []

    def fake_is_file(self: Path) -> bool:
        touched.append(str(self))
        return False

    monkeypatch.setattr(Path, "is_file", fake_is_file)
    uris = ["//attacker.invalid/share/x", "file://attacker.invalid/share/x"]
    if os.name == "nt":
        uris.append(r"\\attacker.invalid\share\x")
    for uri in uris:
        ref = ExternalRef(1, "p", "DS1", uri)
        assert check_external(ref).status == "remote"
        assert touched == []
        assert check_external(ref, check_network=True).status == "missing"
        assert len(touched) == 1
        touched.clear()


# ---------------------------------------------------------------- 화면과 명령줄


def test_backup_page_create_and_download(tmp_path: Path) -> None:
    data_dir = tmp_path / "web"
    _seed(data_dir)
    with TestClient(create_app(Settings(data_dir=data_dir)), base_url=BASE_URL) as client:
        assert 'href="/backups">백업</a>' in client.get("/projects/1").text
        assert "아직 백업이 없습니다" in client.get("/backups").text
        made = client.post("/backups", follow_redirects=False)
        assert made.status_code == 303 and made.headers["location"].startswith("/backups?created=rcompass-backup-")
        page = client.get(made.headers["location"]).text
        assert "백업을 만들었습니다" in page and "외부 참조 1건" in page and "D:/data/eval100_v1.jsonl" in page
        name = made.headers["location"].split("=", 1)[1]
        download = client.get(f"/backups/{name}")
        assert download.status_code == 200 and download.headers["content-type"] == "application/zip"
        assert download.headers["x-content-type-options"] == "nosniff"
        assert download.content == (data_dir / "backups" / name).read_bytes()
        for bad in ("rcompass.db", "..%2Frcompass.db", "rcompass-backup-x.zip", "rcompass-backup-20261124-0930.zip"):
            assert client.get(f"/backups/{bad}").status_code == 404
        assert "백업을 만들었습니다" not in client.get("/backups?created=../rcompass.db").text


def test_cli_init_example_backup_restore(tmp_path: Path) -> None:
    data_dir, target = tmp_path / "a", tmp_path / "b"
    first = runner.invoke(cli_app, ["init", "--example", "--data-dir", str(data_dir)])
    assert first.exit_code == 0, first.output
    assert "예제 프로젝트를 만들었습니다: /projects/1" in first.output
    again = runner.invoke(cli_app, ["init", "--example", "--data-dir", str(data_dir)])
    assert "예제 프로젝트가 이미 있습니다" in again.output

    made = runner.invoke(cli_app, ["backup", "--data-dir", str(data_dir), "-o", str(tmp_path / "out")])
    assert made.exit_code == 0, made.output
    assert "첨부 파일 4개" in made.output and "D:/data/eval100_v1.jsonl" in made.output
    [archive] = list((tmp_path / "out").glob("rcompass-backup-*.zip"))

    restored = runner.invoke(cli_app, ["restore", str(archive), "--data-dir", str(target)])
    assert restored.exit_code == 0, restored.output
    assert "sha256이 백업" in restored.output and "[없음] DS1 eval-100 v1.0" in restored.output
    refused = runner.invoke(cli_app, ["restore", str(archive), "--data-dir", str(target)])
    assert refused.exit_code == 1 and "--force" in refused.output
    assert _handoff(target) == _handoff(data_dir)


def test_failed_swap_puts_old_data_back(tmp_path: Path, source: Path, backup_zip: Path, monkeypatch) -> None:
    """서버가 DB를 잡고 있어 옮기지 못하면(Windows) 기존 데이터를 그대로 되돌립니다."""
    import os

    from rcompass.services import backup as backup_module

    other = tmp_path / "other"
    _seed(other)
    before = (other / "rcompass.db").read_bytes(), _blobs(other)
    real_replace = os.replace

    def flaky_replace(src, dst):
        if Path(src).parent.name.startswith(".restore-") and Path(src).name == "files":
            raise PermissionError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr(backup_module.os, "replace", flaky_replace)
    with pytest.raises(BackupError, match="서버"):
        restore_backup(backup_zip, other, force=True)
    monkeypatch.undo()
    assert (other / "rcompass.db").read_bytes() == before[0] and _blobs(other) == before[1]
    assert not list(other.glob(".restore-*")) and not list(other.glob(".replaced-*"))


def test_force_restore_over_damaged_db_keeps_old_files_aside(tmp_path: Path, source: Path, backup_zip: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "rcompass.db").write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(BackupError, match="이미 기록이 있습니다"):
        restore_backup(backup_zip, broken)
    result = restore_backup(backup_zip, broken, force=True)
    kept = result.pre_restore_backup
    assert kept is not None and kept.is_dir() and kept.parent == broken / "backups"
    assert (kept / "rcompass.db").read_bytes().startswith(b"this is not sqlite")
    assert _handoff(broken) == _handoff(source)


def test_corrupt_deflated_member_gives_korean_error(tmp_path: Path, backup_zip: Path) -> None:
    data = bytearray(backup_zip.read_bytes())
    with zipfile.ZipFile(backup_zip) as archive:
        info = archive.getinfo("db.sqlite")
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    for offset in range(start + 20, start + 60):
        data[offset] ^= 0x5A
    bad = tmp_path / "crc.zip"
    bad.write_bytes(bytes(data))
    _refused(tmp_path, bad, "손상|다릅니다")


def test_manifest_warnings_must_be_a_list(tmp_path: Path, backup_zip: Path) -> None:
    def mutate(members: dict[str, bytes]) -> None:
        manifest = _manifest(members)
        manifest["warnings"] = None
        _set_manifest(members, manifest)

    _refused(tmp_path, _rewrite(backup_zip, tmp_path / "w.zip", mutate), "warnings")


def test_unwritable_output_is_a_backup_error(tmp_path: Path, source: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    with pytest.raises(BackupError, match="백업 파일을 쓰지 못했습니다"):
        create_backup(source, dest_dir=blocker)


def test_stranded_swap_keeps_both_copies(tmp_path: Path, backup_zip: Path, monkeypatch) -> None:
    import os

    from rcompass.services import backup as backup_module

    other = tmp_path / "other"
    _seed(other)
    real_replace = os.replace

    def broken_replace(src, dst):
        src = Path(src)
        if src.parent.name.startswith(".restore-") and src.name == "files":
            raise PermissionError("locked")
        if src.parent.name.startswith(".replaced-") and src.name == "rcompass.db":
            raise PermissionError("locked too")
        return real_replace(src, dst)

    monkeypatch.setattr(backup_module.os, "replace", broken_replace)
    with pytest.raises(BackupError, match="되돌리지도 못했습니다") as info:
        restore_backup(backup_zip, other, force=True)
    monkeypatch.undo()
    [stage] = other.glob(".restore-*")
    [trash] = other.glob(".replaced-*")
    assert str(stage) in str(info.value) and str(trash) in str(info.value)
    assert (trash / "rcompass.db").exists() and (stage / "files").is_dir()
