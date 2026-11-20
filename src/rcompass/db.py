"""SQLite 엔진, 세션, 스키마 마이그레이션."""

from __future__ import annotations

import enum
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import URL, Connection, Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from rcompass.models import APPEND_ONLY_TABLES

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BUSY_TIMEOUT_SECONDS = 10


def create_db_engine(db_path: Path, *, wal: bool = True) -> Engine:
    """SQLite 엔진을 만듭니다.

    - 모든 트랜잭션을 ``BEGIN IMMEDIATE``로 직접 시작합니다. pysqlite 기본 동작은 DDL을
      트랜잭션 밖에서 바로 커밋하므로, 마이그레이션이 중간에 실패하면 표 일부만 남습니다.
      또 읽은 뒤 쓰는 트랜잭션(짧은 ID 발급 등)이 동시에 돌 때 SQLITE_BUSY로 실패하지 않도록
      시작할 때 쓰기 잠금을 잡고, 다른 쪽은 busy timeout 동안 기다립니다.
    - ``wal=False``는 스키마 확인용입니다. 이 앱이 만들지 않은 DB 파일을 WAL로 바꾸지 않습니다.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 문자열 결합 대신 URL.create를 써서 경로의 '?' 등이 쿼리로 해석되지 않게 합니다.
    engine = create_engine(
        URL.create("sqlite", database=str(db_path)),
        connect_args={"timeout": BUSY_TIMEOUT_SECONDS},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _connection_record) -> None:
        dbapi_connection.isolation_level = None  # 드라이버의 자동 BEGIN을 끄고 아래 begin 이벤트에 맡김
        cursor = dbapi_connection.cursor()
        if wal:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def _alembic_config() -> Config:
    config = Config()
    # ConfigParser 보간을 피하려고 '%'를 이스케이프합니다.
    config.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
    return config


class SchemaStatus(str, enum.Enum):
    EMPTY = "empty"  # 표가 하나도 없음: 새 데이터 디렉터리
    CURRENT = "current"  # 최신
    OUTDATED = "outdated"  # 알려진 이전 버전: rcompass migrate 필요
    UNVERSIONED = "unversioned"  # 표는 있는데 마이그레이션 기록이 없음
    UNKNOWN = "unknown"  # 이 앱이 모르는 버전(더 새 앱이 만든 DB 등)


STATUS_MESSAGES = {
    SchemaStatus.OUTDATED: "DB가 이전 버전입니다. 먼저 `rcompass migrate`를 실행하세요.",
    SchemaStatus.UNVERSIONED: "DB에 마이그레이션 기록이 없습니다. 연구 나침반이 만든 DB가 아닐 수 있습니다.",
    SchemaStatus.UNKNOWN: "이 앱보다 새 버전이 만든 DB입니다. 앱을 업데이트하세요(`git pull` 후 `uv sync`).",
}


class SchemaError(RuntimeError):
    def __init__(self, status: SchemaStatus) -> None:
        super().__init__(STATUS_MESSAGES[status])
        self.status = status


def head_revision() -> str:
    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def schema_status(engine: Engine) -> SchemaStatus:
    script = ScriptDirectory.from_config(_alembic_config())
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        current = set(MigrationContext.configure(connection).get_current_heads())
    if not tables - {"alembic_version"} and not current:
        return SchemaStatus.EMPTY
    if not current:
        return SchemaStatus.UNVERSIONED
    if current == set(script.get_heads()):
        return SchemaStatus.CURRENT
    known = {revision.revision for revision in script.walk_revisions()}
    return SchemaStatus.OUTDATED if current <= known else SchemaStatus.UNKNOWN


def check_schema(db_path: Path) -> SchemaStatus:
    """파일을 WAL로 바꾸지 않고 스키마 상태만 확인합니다."""
    engine = create_db_engine(db_path, wal=False)
    try:
        return schema_status(engine)
    finally:
        engine.dispose()


def open_database(db_path: Path) -> Engine:
    """DB를 열고 스키마를 확인합니다. 빈 DB는 최신 스키마로 만들고, 다른 불일치는 SchemaError로 알립니다.

    이미 기록이 있는 DB를 자동으로 마이그레이션하지 않습니다(계획서 5절: 업데이트는 `rcompass migrate`).
    """
    status = check_schema(db_path)
    if status not in (SchemaStatus.EMPTY, SchemaStatus.CURRENT):
        raise SchemaError(status)
    engine = create_db_engine(db_path)
    if status is SchemaStatus.EMPTY:
        try:
            upgrade_to_head(engine)
        except BaseException:
            engine.dispose()
            raise
    return engine


def ensure_append_only_triggers(connection: Connection) -> None:
    """추가만 가능한 표의 UPDATE/DELETE 차단 트리거를 (없으면) 만듭니다.

    SQLite 배치 마이그레이션은 표를 새로 만들어 바꾸면서 트리거를 지웁니다.
    그래서 마이그레이션이 끝날 때마다 현재 모델 기준으로 트리거를 되살립니다.
    """
    existing = set(inspect(connection).get_table_names())
    for table in APPEND_ONLY_TABLES:
        if table not in existing:
            continue
        for action in ("UPDATE", "DELETE"):
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} 기록은 추가만 할 수 있습니다'); END"
            )


def upgrade_to_head(engine: Engine) -> None:
    """한 트랜잭션 안에서 마이그레이션합니다. 실패하면 DB는 시작 전 상태 그대로입니다."""
    config = _alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        ensure_append_only_triggers(connection)
