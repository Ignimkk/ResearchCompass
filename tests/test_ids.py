import threading

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from rcompass.db import session_factory
from rcompass.ids import next_short_id
from rcompass.models import Project


def _project(session: Session) -> int:
    project = Project(title="t", question="q")
    session.add(project)
    session.flush()
    return project.id


def test_sequential_ids_per_prefix(session: Session) -> None:
    pid = _project(session)
    assert [next_short_id(session, pid, "P") for _ in range(3)] == ["P1", "P2", "P3"]
    assert next_short_id(session, pid, "D") == "D1"


def test_issued_experiment_and_baseline_ids_never_take_zero(session: Session) -> None:
    # E0과 B0은 프로젝트 생성 시 직접 붙이는 예약 번호입니다.
    pid = _project(session)
    assert next_short_id(session, pid, "E") == "E1"
    assert next_short_id(session, pid, "B") == "B1"


def test_concurrent_issuing_gives_unique_ids(engine: Engine) -> None:
    factory = session_factory(engine)
    with factory.begin() as setup:
        pid = _project(setup)
    issued: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(15):
                with factory.begin() as session:
                    issued.append(next_short_id(session, pid, "P"))
        except BaseException as error:  # noqa: BLE001 - 스레드 예외를 본 스레드로 전달
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(issued, key=lambda s: int(s[1:])) == [f"P{n}" for n in range(1, 61)]


def test_scoped_counters_are_independent(session: Session) -> None:
    pid = _project(session)
    assert next_short_id(session, pid, "R", scope="R:1") == "R1"
    assert next_short_id(session, pid, "R", scope="R:1") == "R2"
    assert next_short_id(session, pid, "R", scope="R:2") == "R1"


def test_counters_are_per_project(session: Session) -> None:
    first, second = _project(session), _project(session)
    next_short_id(session, first, "P")
    assert next_short_id(session, second, "P") == "P1"


def test_rollback_returns_number(session: Session) -> None:
    pid = _project(session)
    session.commit()
    next_short_id(session, pid, "P")
    session.rollback()
    assert next_short_id(session, pid, "P") == "P1"
