from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from rcompass.db import open_database, session_factory


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = open_database(tmp_path / "rcompass.db")
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with session_factory(engine)() as session:
        yield session
