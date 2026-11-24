"""Jinja2 템플릿 설정과 화면용 필터."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from rcompass import __version__
from rcompass.models import Aggregate, Direction
from rcompass.services.aggregate import format_aggregated, format_plain
from rcompass.services.problems import OUTCOME_SYMBOLS

WEB_DIR = Path(__file__).parent

DIRECTION_SYMBOLS = {Direction.LOWER: "≤", Direction.HIGHER: "≥"}
DIRECTION_LABELS = {Direction.LOWER: "낮을수록 좋음", Direction.HIGHER: "높을수록 좋음"}
AGGREGATE_LABELS = {Aggregate.MEAN: "평균", Aggregate.MEDIAN: "중앙값", Aggregate.WORST: "최악값"}


templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.globals.update(
    version=__version__,
    DIRECTION_LABELS=DIRECTION_LABELS,
    AGGREGATE_LABELS=AGGREGATE_LABELS,
    outcome_symbols=OUTCOME_SYMBOLS,
    format_agg=format_aggregated,
)
templates.env.filters.update(
    direction_symbol=lambda direction: DIRECTION_SYMBOLS[direction],
    target=format_plain,
)
