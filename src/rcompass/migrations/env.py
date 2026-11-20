"""Alembic 실행 환경.

앱(`rcompass.db.upgrade_to_head`)은 ``config.attributes["connection"]``으로 연결을 넘깁니다.
개발 중 ``alembic`` 명령(저장소 루트의 alembic.ini)은 ``sqlalchemy.url``을 씁니다.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from rcompass.models import Base

config = context.config


def _configure_and_run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=True,  # SQLite는 ALTER가 제한적이라 배치 모드로 표를 다시 만듭니다.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    _configure_and_run(connection)
else:
    url = context.get_x_argument(as_dictionary=True).get("url") or config.get_main_option("sqlalchemy.url")
    engine = create_engine(url)
    with engine.begin() as conn:
        _configure_and_run(conn)
    engine.dispose()
