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


@pytest.fixture
def project(session: Session):
    """검증 시나리오(계획서 7절)의 프로젝트: 기준선 B0 오답 17·18·19, 응답 시간 미측정."""
    from rcompass.models import Direction
    from rcompass.services.projects import CriterionInput, ProjectInput, create_project

    created = create_project(
        session,
        ProjectInput(
            title="QA 평가 오답 줄이기",
            question="동일 평가 100문항에서 오답을 18→12개 이하로 줄일 수 있는가?",
            baseline_label="현행 프롬프트 v1",
            criteria=[
                CriterionInput(name="오답 수", metric_key="wrong_count", direction=Direction.LOWER, target=12,
                               min_runs=3, baseline_values=[17, 18, 19]),
                CriterionInput(name="평균 응답 시간", metric_key="avg_latency_s", direction=Direction.LOWER,
                               target=2.0),
            ],
        ),
    )
    session.commit()
    return created
