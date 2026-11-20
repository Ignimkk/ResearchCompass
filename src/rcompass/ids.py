"""프로젝트 안에서 사람이 읽는 짧은 ID(P3, E7, B2, R2 …)를 발급합니다.

번호는 지운 기록이 있어도 재사용하지 않고, 모두 1부터 시작합니다.
기준선 B0과 기준선 측정 실험 E0은 프로젝트를 만들 때 ``BASELINE_ZERO_ID``와
``BASELINE_MEASUREMENT_ID``로 직접 붙이므로, 일반 실험이 E0을 받는 일이 없습니다.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.models import IdCounter

PREFIXES = {
    "problem": "P",
    "experiment": "E",
    "baseline": "B",
    "decision": "D",
    "file": "F",
    "dataset": "DS",
    "criterion": "C",
    "diagram": "G",
}
BASELINE_ZERO_ID = "B0"
BASELINE_MEASUREMENT_ID = "E0"


def next_short_id(session: Session, project_id: int, prefix: str, *, scope: str | None = None) -> str:
    """``prefix`` 번호를 하나 발급합니다.

    ``scope``를 주면 카운터를 따로 둡니다. 예: 실행은 실험마다 R1부터 시작하므로
    ``next_short_id(s, pid, "R", scope=f"R:{experiment_id}")``.
    호출한 세션의 트랜잭션 안에서 증가하므로, 커밋하지 않으면 번호도 되돌아갑니다.
    동시 발급은 엔진이 트랜잭션을 ``BEGIN IMMEDIATE``로 시작해 직렬화합니다(rcompass.db).
    """
    key = scope or prefix
    counter = session.execute(
        select(IdCounter).where(IdCounter.project_id == project_id, IdCounter.scope == key)
    ).scalar_one_or_none()
    if counter is None:
        counter = IdCounter(project_id=project_id, scope=key, last_value=1)
        session.add(counter)
    else:
        counter.last_value += 1
    session.flush()
    return f"{prefix}{counter.last_value}"
