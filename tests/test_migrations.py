"""마이그레이션 업그레이드 경로: 이전 버전 DB → `rcompass migrate` → 최신."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from sqlalchemy import inspect
from typer.testing import CliRunner

from rcompass.cli import app as cli_app
from rcompass.db import (
    SchemaStatus,
    _alembic_config,
    check_schema,
    create_db_engine,
    ensure_append_only_triggers,
    open_database,
)
from rcompass.models import APPEND_ONLY_TABLES


def _db_at(db_path: Path, revision: str) -> None:
    engine = create_db_engine(db_path)
    config = _alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
        ensure_append_only_triggers(connection)
    engine.dispose()


def test_0001_database_is_outdated_and_migrate_brings_it_current(tmp_path: Path) -> None:
    db_path = tmp_path / "rcompass.db"
    _db_at(db_path, "0001")
    assert check_schema(db_path) is SchemaStatus.OUTDATED

    result = CliRunner().invoke(cli_app, ["migrate", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert check_schema(db_path) is SchemaStatus.CURRENT

    engine = open_database(db_path)
    indexes = {ix["name"] for ix in inspect(engine).get_indexes("runs")}
    with engine.connect() as connection:
        triggers = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'")}
    engine.dispose()
    assert "uq_runs_experiment_id_label" in indexes
    assert {f"{t}_no_{a}" for t in APPEND_ONLY_TABLES for a in ("update", "delete")} <= triggers
