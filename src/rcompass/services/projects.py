"""프로젝트 생성과 조회 (여정 1: 연구 질문, 성공 기준, 현재 기준선)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.ids import BASELINE_MEASUREMENT_ID, BASELINE_ZERO_ID, next_short_id
from rcompass.models import (
    Aggregate,
    Baseline,
    BaselineChange,
    BaselineChangeKind,
    BaselineRun,
    Direction,
    Experiment,
    ExperimentKind,
    ExperimentStatus,
    MetricResult,
    Project,
    Run,
    RunStatus,
    SuccessCriterion,
    utcnow,
)
from rcompass.services import ValidationError

# 결과 블록(YAML)과 인계 문서에서 그대로 쓰는 이름이므로 ASCII 식별자로 제한합니다.
METRIC_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$")
# 한 번의 입력으로 만들 수 있는 실행 수 상한(최소 실행 수도 같은 상한).
MAX_RUNS = 100


@dataclass
class CriterionInput:
    name: str
    metric_key: str
    direction: Direction
    target: float
    unit: str | None = None
    aggregate: Aggregate = Aggregate.MEAN
    min_runs: int = 1
    baseline_values: list[float] = field(default_factory=list)  # 기준선 0의 실행별 값(선택)


@dataclass
class ProjectInput:
    title: str
    question: str
    baseline_label: str
    criteria: list[CriterionInput]
    next_action: str | None = None


def _blank(text: str | None) -> bool:
    return text is None or not text.strip()


def validate_project_input(data: ProjectInput) -> dict[str, str]:
    errors: dict[str, str] = {}
    if _blank(data.title):
        errors["title"] = "제목을 입력하세요."
    if _blank(data.question):
        errors["question"] = "연구 질문을 입력하세요."
    if _blank(data.baseline_label):
        errors["baseline_label"] = "현재 기준선의 이름을 입력하세요. 예: 현행 프롬프트 v1"
    if not data.criteria:
        errors["criteria"] = "성공 기준을 하나 이상 입력하세요."
    seen_keys: set[str] = set()
    for index, criterion in enumerate(data.criteria):
        prefix = f"criteria-{index}"
        if _blank(criterion.name):
            errors[f"{prefix}-name"] = "기준 이름을 입력하세요."
        key = criterion.metric_key.strip()
        if not METRIC_KEY_PATTERN.match(key):
            errors[f"{prefix}-metric_key"] = "지표 키는 영문자로 시작하고 영문·숫자·_ . - 만 쓸 수 있습니다. 예: wrong_count"
        elif key in seen_keys:
            errors[f"{prefix}-metric_key"] = "같은 지표 키를 두 기준에 쓸 수 없습니다."
        seen_keys.add(key)
        if not math.isfinite(criterion.target):
            errors[f"{prefix}-target"] = "목표값은 숫자여야 합니다."
        if not 1 <= criterion.min_runs <= MAX_RUNS:
            errors[f"{prefix}-min_runs"] = f"최소 실행 수는 1에서 {MAX_RUNS} 사이의 정수여야 합니다."
        if any(not math.isfinite(v) for v in criterion.baseline_values):
            errors[f"{prefix}-baseline_values"] = "기준선 값은 숫자여야 합니다."
        elif len(criterion.baseline_values) > MAX_RUNS:
            errors[f"{prefix}-baseline_values"] = f"기준선 값은 실행 {MAX_RUNS}개까지 입력할 수 있습니다."
    return errors


def create_project(session: Session, data: ProjectInput) -> Project:
    """프로젝트, 성공 기준, 기준선 측정 실험 E0, 기준선 B0을 한 번에 만듭니다.

    기준선 값이 있으면 E0에 실행(R1, R2, …)과 수치를 기록하고 B0 스냅샷으로 고정합니다.
    값이 없으면 B0 스냅샷은 비어 있고, 성공 기준은 '미측정'으로 보입니다.
    호출한 쪽이 커밋합니다.
    """
    errors = validate_project_input(data)
    if errors:
        raise ValidationError(errors)

    project = Project(
        title=data.title.strip(),
        question=data.question.strip(),
        next_action=(data.next_action or "").strip() or None,
    )
    session.add(project)
    session.flush()

    criteria: list[tuple[SuccessCriterion, CriterionInput]] = []
    for position, item in enumerate(data.criteria):
        criterion = SuccessCriterion(
            project_id=project.id,
            short_id=next_short_id(session, project.id, "C"),
            name=item.name.strip(),
            metric_key=item.metric_key.strip(),
            direction=item.direction,
            target=item.target,
            unit=(item.unit or "").strip() or None,
            aggregate=item.aggregate,
            min_runs=item.min_runs,
            position=position,
        )
        session.add(criterion)
        criteria.append((criterion, item))

    baseline_label = data.baseline_label.strip()
    run_count = max((len(item.baseline_values) for item in data.criteria), default=0)
    e0 = Experiment(
        project_id=project.id,
        short_id=BASELINE_MEASUREMENT_ID,
        kind=ExperimentKind.BASELINE_MEASUREMENT,
        title=f"기준선 측정: {baseline_label}",
        status=ExperimentStatus.DONE if run_count else ExperimentStatus.PLANNED,
    )
    session.add(e0)
    session.flush()

    b0 = Baseline(
        project_id=project.id,
        short_id=BASELINE_ZERO_ID,
        label=baseline_label,
        source_experiment_id=e0.id,
    )
    session.add(b0)
    session.flush()
    change = BaselineChange(project_id=project.id, kind=BaselineChangeKind.INITIAL, to_baseline_id=b0.id)
    session.add(change)
    session.flush()
    project.current_baseline_id = b0.id

    for run_index in range(run_count):
        run = Run(
            experiment_id=e0.id,
            short_id=next_short_id(session, project.id, "R", scope=f"R:{e0.id}"),
            label=f"실행 {run_index + 1}",
            status=RunStatus.DONE,
            finished_at=utcnow(),
        )
        session.add(run)
        session.flush()
        for criterion, item in criteria:
            if run_index < len(item.baseline_values):
                session.add(
                    MetricResult(
                        run_id=run.id,
                        metric_key=criterion.metric_key,
                        value=item.baseline_values[run_index],
                        unit=criterion.unit,
                        criterion_id=criterion.id,
                    )
                )
        session.add(BaselineRun(baseline_id=b0.id, run_id=run.id, added_by_change_id=change.id))
    session.flush()
    return project


def list_projects(session: Session) -> list[Project]:
    return list(session.scalars(select(Project).order_by(Project.updated_at.desc(), Project.id.desc())))


def get_project(session: Session, project_id: int) -> Project | None:
    return session.get(Project, project_id)


def get_criteria(session: Session, project_id: int) -> list[SuccessCriterion]:
    return list(
        session.scalars(
            select(SuccessCriterion)
            .where(SuccessCriterion.project_id == project_id)
            .order_by(SuccessCriterion.position, SuccessCriterion.id)
        )
    )
