"""여러 실행의 수치를 하나로 모으는 계산과 표시 형식."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from rcompass.models import Aggregate, Direction


@dataclass(frozen=True)
class Aggregated:
    values: tuple[float, ...]
    value: float  # 판정에 쓰는 대표값(기준의 aggregate 방식)
    mean: float
    std: float | None  # 표본 표준편차(n-1). 실행이 하나면 None
    minimum: float
    maximum: float

    @property
    def n(self) -> int:
        return len(self.values)


def aggregate(values: list[float], method: Aggregate, direction: Direction) -> Aggregated | None:
    if not values:
        return None
    if method is Aggregate.MEAN:
        value = statistics.fmean(values)
    elif method is Aggregate.MEDIAN:
        value = statistics.median(values)
    else:  # WORST: 낮을수록 좋으면 최댓값, 높을수록 좋으면 최솟값
        value = max(values) if direction is Direction.LOWER else min(values)
    return Aggregated(
        values=tuple(values),
        value=value,
        mean=statistics.fmean(values),
        std=statistics.stdev(values) if len(values) > 1 else None,
        minimum=min(values),
        maximum=max(values),
    )


def meets_target(value: float, target: float, direction: Direction) -> bool:
    return value <= target if direction is Direction.LOWER else value >= target


MAX_DECIMALS = 6


def decimals_of(values: tuple[float, ...]) -> int:
    """입력값에 쓰인 소수 자릿수(최대 6). 0.0004도 0으로 뭉개지지 않게 6자리까지 봅니다."""
    places = 0
    for value in values:
        text = f"{value:.{MAX_DECIMALS}f}".rstrip("0")
        if "." in text:
            places = max(places, len(text.split(".")[1]))
    return places


def format_plain(value: float) -> str:
    """사용자가 입력한 그대로에 가깝게(가장 짧은 표현) 보여줍니다. 예: 12, 2, 1234567, 0.1234567."""
    text = repr(float(value))
    return text[:-2] if text.endswith(".0") else text


def format_number(value: float, decimals: int = 0) -> str:
    return f"{value:.{decimals}f}"


def format_aggregated(agg: Aggregated, method: Aggregate = Aggregate.MEAN) -> str:
    """예: 실행 1개 ``18``, 여러 개 평균 ``18.0 ± 1.0 (n=3)``, 중앙값 ``18.0 (중앙값, n=3)``."""
    base = decimals_of(agg.values)
    if agg.n == 1:
        return format_number(agg.value, base)
    places = max(1, base)
    if method is Aggregate.MEAN:
        return f"{format_number(agg.mean, places)} ± {format_number(agg.std or 0.0, places)} (n={agg.n})"
    label = "중앙값" if method is Aggregate.MEDIAN else "최악값"
    return f"{format_number(agg.value, places)} ({label}, n={agg.n})"
