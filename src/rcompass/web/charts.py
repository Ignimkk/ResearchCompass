"""판정 패널의 실행별 점 그림(인라인 SVG) 좌표 계산.

성공 기준 하나마다 두 줄을 그립니다: 위는 출발 기준선의 실행들, 아래는 이 실험의 실행들.
각 실행은 점, 대표값(집계)은 세로 눈금, 최소~최대는 옅은 가로선, 목표는 점선입니다.
색은 CSS 변수(--series-base, --series-trial)로 정해 밝은·어두운 화면을 모두 지원하고,
두 줄은 위치와 왼쪽 이름으로도 구분되므로 색만으로 구별하지 않습니다. JS 없이 점마다 <title> 풍선말이 붙습니다.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from rcompass.services.aggregate import Aggregated, format_plain
from rcompass.services.decisions import Comparison

WIDTH = 560
LEFT = 104  # 줄 이름 자리
RIGHT = 20
LANE_Y = (34, 70)
AXIS_Y = 96
HEIGHT = 116


@dataclass(frozen=True)
class Dot:
    x: float
    y: float
    title: str


@dataclass(frozen=True)
class Lane:
    name: str
    css: str  # base | trial
    y: float
    dots: list[Dot]
    agg_x: float | None
    range_x: tuple[float, float] | None


@dataclass(frozen=True)
class Tick:
    x: float
    label: str


@dataclass(frozen=True)
class StripChart:
    width: int
    height: int
    left: int
    right_edge: int
    lanes: list[Lane]
    ticks: list[Tick]
    target_x: float
    target_label: str
    axis_y: int
    summary: str  # 화면 낭독기용 요약


def _nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    span = hi - lo
    raw = span / max(1, count - 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    first = math.ceil(lo / step) * step
    ticks, value = [], first
    while value <= hi + step * 1e-9:
        ticks.append(round(value, 10))
        value += step
    return ticks


def _domain(values: list[float]) -> tuple[float, float]:
    lo, hi = min(values), max(values)
    if hi == lo:
        pad = abs(lo) * 0.1 or 1.0
    else:
        pad = (hi - lo) * 0.12
    return lo - pad, hi + pad


def strip_chart(comparison: Comparison, trial_name: str) -> StripChart | None:
    lanes_data: list[tuple[str, str, list[tuple[str, float]], Aggregated | None]] = []
    if comparison.base_points:
        lanes_data.append((f"기준선 {comparison.baseline.short_id}", "base", comparison.base_points, comparison.base))
    if comparison.trial_points:
        lanes_data.append((trial_name, "trial", comparison.trial_points, comparison.trial))
    if not lanes_data:
        return None
    criterion = comparison.criterion
    all_values = [v for _, _, points, _ in lanes_data for _, v in points] + [criterion.target]
    lo, hi = _domain(all_values)
    right_edge = WIDTH - RIGHT

    def x(value: float) -> float:
        return round(LEFT + (value - lo) / (hi - lo) * (right_edge - LEFT), 1)

    lanes = []
    for index, (name, css, points, agg) in enumerate(lanes_data):
        y = LANE_Y[index] if len(lanes_data) == 2 else (LANE_Y[0] + LANE_Y[1]) / 2
        totals = Counter(v for _, v in points)
        seen: Counter = Counter()
        dots = []
        for label, value in points:
            # 같은 값의 점은 위아래로 조금씩 벌려 겹쳐 사라지지 않게 합니다.
            # 전체 벌림은 ±14px(두 줄 간격의 절반 아래)를 넘지 않게 해 다른 줄로 넘어가지 않습니다.
            step = min(7.0, 28 / (totals[value] - 1)) if totals[value] > 1 else 0.0
            offset = round((seen[value] - (totals[value] - 1) / 2) * step, 1)
            seen[value] += 1
            dots.append(Dot(x(value), y + offset, f"{label}: {format_plain(value)}"))
        range_x = (x(agg.minimum), x(agg.maximum)) if agg and agg.n > 1 else None
        lanes.append(Lane(name, css, y, dots, x(agg.value) if agg else None, range_x))

    symbol = "≤" if criterion.direction.value == "lower" else "≥"
    target_label = f"목표 {symbol} {format_plain(criterion.target)}"
    parts = [f"{criterion.name}({criterion.metric_key}) 실행별 값."]
    for lane, (_, _, points, _) in zip(lanes, lanes_data):
        parts.append(f"{lane.name}: " + ", ".join(format_plain(v) for _, v in points) + ".")
    parts.append(target_label + ".")
    ticks = [Tick(x(v), format_plain(v)) for v in _nice_ticks(lo, hi)]
    return StripChart(WIDTH, HEIGHT, LEFT, right_edge, lanes, ticks, x(criterion.target), target_label, AXIS_Y,
                      " ".join(parts))
