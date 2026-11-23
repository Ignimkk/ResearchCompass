"""스키마 상태 판별과 `rcompass migrate`, 서버 시작 시 처리."""

from pathlib import Path

import pytest
import uvicorn
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import text
from typer.testing import CliRunner

from rcompass.cli import app as cli_app
from rcompass.config import Settings
from rcompass.db import SchemaError, SchemaStatus, create_db_engine, head_revision, open_database, schema_status
from rcompass.web.app import create_app

runner = CliRunner()


def _status(db_path: Path) -> SchemaStatus:
    engine = create_db_engine(db_path)
    try:
        return schema_status(engine)
    finally:
        engine.dispose()


def _set_version(db_path: Path, sql: str) -> None:
    engine = create_db_engine(db_path)
    with engine.begin() as connection:
        connection.execute(text(sql))
    engine.dispose()


def _make_current(db_path: Path) -> None:
    open_database(db_path).dispose()


def test_new_file_is_empty_then_current(tmp_path: Path) -> None:
    db_path = tmp_path / "rcompass.db"
    assert _status(db_path) is SchemaStatus.EMPTY
    _make_current(db_path)
    assert _status(db_path) is SchemaStatus.CURRENT


def test_unknown_revision_is_reported(tmp_path: Path) -> None:
    db_path = tmp_path / "rcompass.db"
    _make_current(db_path)
    _set_version(db_path, "UPDATE alembic_version SET version_num = 'ffffffff'")
    with pytest.raises(SchemaError) as error:
        open_database(db_path)
    assert error.value.status is SchemaStatus.UNKNOWN


def test_tables_without_version_are_unversioned(tmp_path: Path) -> None:
    db_path = tmp_path / "rcompass.db"
    _make_current(db_path)
    _set_version(db_path, "DELETE FROM alembic_version")
    assert _status(db_path) is SchemaStatus.UNVERSIONED


def test_older_known_revision_is_outdated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "rcompass.db"
    _make_current(db_path)
    monkeypatch.setattr(ScriptDirectory, "get_heads", lambda self: ["9999"])
    assert _status(db_path) is SchemaStatus.OUTDATED
    with pytest.raises(SchemaError):
        open_database(db_path)  # 기록이 있는 DB는 자동으로 마이그레이션하지 않습니다.


def test_migrate_creates_then_reports_current(tmp_path: Path) -> None:
    first = runner.invoke(cli_app, ["migrate", "--data-dir", str(tmp_path)])
    assert first.exit_code == 0, first.output
    assert "최신 버전으로 올렸습니다" in first.output
    second = runner.invoke(cli_app, ["migrate", "--data-dir", str(tmp_path)])
    assert "이미 최신" in second.output
    assert _status(tmp_path / "rcompass.db") is SchemaStatus.CURRENT
    assert head_revision() == "0002"


def test_migrate_refuses_unknown_db(tmp_path: Path) -> None:
    _make_current(tmp_path / "rcompass.db")
    _set_version(tmp_path / "rcompass.db", "UPDATE alembic_version SET version_num = 'ffffffff'")
    result = runner.invoke(cli_app, ["migrate", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "새 버전이 만든 DB" in result.output


def test_serve_refuses_mismatched_db_before_starting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_current(tmp_path / "rcompass.db")
    _set_version(tmp_path / "rcompass.db", "DELETE FROM alembic_version")
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.append(True))
    result = runner.invoke(cli_app, ["serve", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "마이그레이션 기록이 없습니다" in result.output
    assert started == []


def test_failed_migration_leaves_no_partial_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """마이그레이션 도중 실패하면 표 일부만 남지 않고 빈 DB로 돌아가야 다시 시도할 수 있습니다."""
    from rcompass import db as db_module

    def failing_triggers(connection) -> None:  # 모든 표를 만든 뒤 마지막 단계에서 실패
        raise RuntimeError("중간 실패")

    monkeypatch.setattr(db_module, "ensure_append_only_triggers", failing_triggers)
    db_path = tmp_path / "rcompass.db"
    with pytest.raises(RuntimeError, match="중간 실패"):
        open_database(db_path)
    assert _status(db_path) is SchemaStatus.EMPTY

    monkeypatch.undo()
    _make_current(db_path)  # 다시 시도하면 정상적으로 만들어짐
    assert _status(db_path) is SchemaStatus.CURRENT


def test_upgrade_restores_dropped_append_only_triggers(tmp_path: Path) -> None:
    """배치 마이그레이션이 표를 다시 만들며 트리거를 지워도 업그레이드 끝에 되살아나야 합니다."""
    from rcompass.db import ensure_append_only_triggers
    from rcompass.models import APPEND_ONLY_TABLES

    db_path = tmp_path / "rcompass.db"
    engine = open_database(db_path)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER decisions_no_update")
        ensure_append_only_triggers(connection)
        names = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'")}
    engine.dispose()
    expected = {f"{t}_no_{a}" for t in APPEND_ONLY_TABLES for a in ("update", "delete")}
    assert expected <= names


def test_checking_foreign_db_does_not_switch_it_to_wal(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "rcompass.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE other_tool (id INTEGER)")
    connection.close()
    runner.invoke(cli_app, ["serve", "--data-dir", str(tmp_path)])
    with sqlite3.connect(db_path) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    connection.close()
    assert mode == "delete"
    assert not (tmp_path / "rcompass.db-wal").exists()


def test_server_startup_fails_on_mismatched_db(tmp_path: Path) -> None:
    _make_current(tmp_path / "rcompass.db")
    _set_version(tmp_path / "rcompass.db", "UPDATE alembic_version SET version_num = 'ffffffff'")
    with pytest.raises(SchemaError):
        with TestClient(create_app(Settings(data_dir=tmp_path))):
            pass
