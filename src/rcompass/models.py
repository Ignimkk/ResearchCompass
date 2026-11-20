"""데이터 모델 (구현 계획서 4절).

규칙
- 시각은 모두 UTC이며 tzinfo 없이 저장합니다.
- 사람이 읽는 짧은 ID(P3, E7, B2, …)는 ``short_id`` 열에 두고, 내부 PK와 분리합니다.
  발급은 ``rcompass.ids.next_short_id``가 맡습니다.
- 추가만 가능한 표(APPEND_ONLY_TABLES)는 DB 트리거가 UPDATE와 DELETE를 막습니다.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# 기록을 고치거나 지우지 않는 표. 마이그레이션이 이 표들에 UPDATE/DELETE 차단 트리거를 만듭니다.
APPEND_ONLY_TABLES = ("decisions", "baselines", "baseline_runs", "baseline_changes", "run_exclusions")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """값(value)을 문자열로 저장하고 CHECK 제약으로 허용 값을 강제합니다."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=32,
        values_callable=lambda members: [m.value for m in members],
    )


def _not_blank(column: str) -> str:
    # coalesce가 없으면 NULL에서 식 전체가 NULL이 되어 SQLite CHECK를 그냥 통과합니다.
    return f"length(trim(coalesce({column}, ''))) > 0"


# ---------------------------------------------------------------- 열거형


class Direction(str, enum.Enum):
    LOWER = "lower"  # 낮을수록 좋음
    HIGHER = "higher"  # 높을수록 좋음


class Aggregate(str, enum.Enum):
    MEAN = "mean"
    MEDIAN = "median"
    WORST = "worst"


class ProblemStatus(str, enum.Enum):
    ACTIVE = "active"  # 지금 조사
    PARKED = "parked"  # 나중에 검토
    RESOLVED = "resolved"
    DROPPED = "dropped"


class ExperimentKind(str, enum.Enum):
    TRIAL = "trial"  # 가설을 검증하는 일반 실험
    BASELINE_MEASUREMENT = "baseline_measurement"  # 기준선 0을 재는 실험 E0


class ExperimentStatus(str, enum.Enum):
    PLANNED = "planned"
    DONE = "done"


class ExperimentOutcome(str, enum.Enum):
    ADOPTED = "adopted"
    REJECTED = "rejected"
    HELD = "held"
    REVOKED = "revoked"


class RunStatus(str, enum.Enum):
    PLANNED = "planned"
    DONE = "done"
    FAILED = "failed"


class Provenance(str, enum.Enum):
    MANUAL = "manual"
    CLAUDE_CONFIRMED = "claude_confirmed"  # Claude Code 결과 블록을 연구자가 확인


class StorageKind(str, enum.Enum):
    ATTACHED = "attached"  # 데이터 디렉터리에 복사(백업 포함)
    EXTERNAL = "external"  # 위치와 지문만 기록(백업 제외)


class FileKind(str, enum.Enum):
    FIGURE = "figure"
    TABLE = "table"
    CONFIG = "config"
    LOG = "log"
    DIAGRAM = "diagram"
    DATA = "data"
    OTHER = "other"


class DecisionType(str, enum.Enum):
    ADOPT = "adopt"
    REJECT = "reject"
    HOLD = "hold"
    REVOKE = "revoke"


class BaselineChangeKind(str, enum.Enum):
    INITIAL = "initial"  # 프로젝트 생성 시 B0 지정
    ADOPT = "adopt"
    REVOKE = "revoke"
    SNAPSHOT_UPDATE = "snapshot_update"  # E0에 실행을 더 기록해 B0 스냅샷을 넓힘


class DiagramKind(str, enum.Enum):
    RESEARCH_FLOW = "research_flow"
    APP_CODE = "app_code"
    RESEARCH_REPO = "research_repo"


class DiagramBasis(str, enum.Enum):
    PLANNED = "planned"
    CODE_BACKED = "code_backed"


# ---------------------------------------------------------------- 프로젝트와 문제


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(_not_blank("title"), name="title_not_blank"),
        CheckConstraint(_not_blank("question"), name="question_not_blank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    question: Mapped[str] = mapped_column(Text)
    next_action: Mapped[Optional[str]] = mapped_column(Text)
    focus_problem_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("problems.id", use_alter=True, ondelete="SET NULL")
    )
    # BaselineChange에서 계산할 수 있는 값의 캐시. 결정 서비스가 같은 트랜잭션에서 갱신합니다.
    current_baseline_id: Mapped[Optional[int]] = mapped_column(ForeignKey("baselines.id", use_alter=True))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class IdCounter(Base):
    """짧은 ID 발급용 카운터. scope 예: ``P``, ``E``, ``R:12``(실험 12의 실행)."""

    __tablename__ = "id_counters"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    scope: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer)


class SuccessCriterion(Base):
    __tablename__ = "success_criteria"
    __table_args__ = (
        UniqueConstraint("project_id", "short_id"),
        CheckConstraint(_not_blank("name"), name="name_not_blank"),
        CheckConstraint(_not_blank("metric_key"), name="metric_key_not_blank"),
        CheckConstraint("min_runs >= 1", name="min_runs_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    short_id: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    metric_key: Mapped[str] = mapped_column(String(100))
    direction: Mapped[Direction] = mapped_column(_enum(Direction, "direction"))
    target: Mapped[float] = mapped_column(Float)
    unit: Mapped[Optional[str]] = mapped_column(String(40))
    eval_dataset_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("dataset_versions.id"))
    aggregate: Mapped[Aggregate] = mapped_column(_enum(Aggregate, "aggregate"), default=Aggregate.MEAN)
    min_runs: Mapped[int] = mapped_column(Integer, default=1)
    position: Mapped[int] = mapped_column(Integer, default=0)


class Problem(Base):
    __tablename__ = "problems"
    __table_args__ = (
        UniqueConstraint("project_id", "short_id"),
        CheckConstraint(_not_blank("title"), name="title_not_blank"),
        CheckConstraint(_not_blank("why"), name="why_not_blank"),  # 이 문제를 푸는 이유 한 줄은 필수
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    short_id: Mapped[str] = mapped_column(String(16))
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("problems.id"), index=True)  # None: 연구 질문 바로 아래
    title: Mapped[str] = mapped_column(String(200))
    why: Mapped[str] = mapped_column(Text)
    status: Mapped[ProblemStatus] = mapped_column(
        _enum(ProblemStatus, "problem_status"), default=ProblemStatus.ACTIVE
    )
    revisit_condition: Mapped[Optional[str]] = mapped_column(Text)
    detail: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- 실험과 실행


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        UniqueConstraint("project_id", "short_id"),
        CheckConstraint(_not_blank("title"), name="title_not_blank"),
        # 일반 실험은 문제, 가설, 판정 기준이 필수. 기준선 측정 실험(E0)은 문제에 속하지 않음.
        CheckConstraint(
            "kind = 'baseline_measurement' OR (problem_id IS NOT NULL"
            f" AND {_not_blank('hypothesis')} AND {_not_blank('decision_rule')})",
            name="trial_requires_hypothesis_and_rule",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    short_id: Mapped[str] = mapped_column(String(16))
    kind: Mapped[ExperimentKind] = mapped_column(_enum(ExperimentKind, "experiment_kind"), default=ExperimentKind.TRIAL)
    problem_id: Mapped[Optional[int]] = mapped_column(ForeignKey("problems.id"), index=True)
    based_on_baseline_id: Mapped[Optional[int]] = mapped_column(ForeignKey("baselines.id", use_alter=True))
    title: Mapped[str] = mapped_column(String(200))
    hypothesis: Mapped[Optional[str]] = mapped_column(Text)
    decision_rule: Mapped[Optional[str]] = mapped_column(Text)
    code_ref: Mapped[Optional[str]] = mapped_column(String(200))  # 커밋 SHA 또는 버전 메모(공통값)
    config_text: Mapped[Optional[str]] = mapped_column(Text)  # 설정(공통값)
    status: Mapped[ExperimentStatus] = mapped_column(
        _enum(ExperimentStatus, "experiment_status"), default=ExperimentStatus.PLANNED
    )
    outcome: Mapped[Optional[ExperimentOutcome]] = mapped_column(_enum(ExperimentOutcome, "experiment_outcome"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ExperimentRevision(Base):
    """결과 기록 뒤 가설이나 판정 기준을 고친 이력."""

    __tablename__ = "experiment_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"), index=True)
    field: Mapped[str] = mapped_column(String(40))
    old_value: Mapped[Optional[str]] = mapped_column(Text)
    new_value: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("experiment_id", "short_id"),
        CheckConstraint(_not_blank("label"), name="label_not_blank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"), index=True)
    short_id: Mapped[str] = mapped_column(String(16))  # 실험 안에서 R1, R2, …
    label: Mapped[str] = mapped_column(String(100))  # 예: seed=1
    config_override: Mapped[Optional[str]] = mapped_column(Text)
    code_ref_override: Mapped[Optional[str]] = mapped_column(String(200))
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus, "run_status"), default=RunStatus.PLANNED)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RunExclusion(Base):
    """실행 제외 기록(추가만 가능). 행이 있으면 그 실행은 집계에서 빠집니다."""

    __tablename__ = "run_exclusions"
    __table_args__ = (CheckConstraint(_not_blank("reason"), name="reason_not_blank"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), unique=True)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


experiment_inputs = Table(
    "experiment_inputs",
    Base.metadata,
    Column("experiment_id", ForeignKey("experiments.id"), primary_key=True),
    Column("dataset_version_id", ForeignKey("dataset_versions.id"), primary_key=True),
    Column("role", String(40), nullable=False, default="eval"),
)


class MetricResult(Base):
    __tablename__ = "metric_results"
    __table_args__ = (CheckConstraint(_not_blank("metric_key"), name="metric_key_not_blank"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    metric_key: Mapped[str] = mapped_column(String(100))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[Optional[str]] = mapped_column(String(40))
    dataset_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("dataset_versions.id"))
    criterion_id: Mapped[Optional[int]] = mapped_column(ForeignKey("success_criteria.id"))
    provenance: Mapped[Provenance] = mapped_column(_enum(Provenance, "provenance"), default=Provenance.MANUAL)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- 자료


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (
        UniqueConstraint("project_id", "short_id"),
        CheckConstraint(_not_blank("name"), name="name_not_blank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    short_id: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text)


class FileObject(Base):
    __tablename__ = "file_objects"
    __table_args__ = (
        UniqueConstraint("project_id", "short_id"),
        CheckConstraint(
            "(storage = 'attached' AND rel_path IS NOT NULL AND sha256 IS NOT NULL)"
            " OR (storage = 'external' AND uri IS NOT NULL)",
            name="storage_location",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    short_id: Mapped[str] = mapped_column(String(16))
    kind: Mapped[FileKind] = mapped_column(_enum(FileKind, "file_kind"), default=FileKind.OTHER)
    storage: Mapped[StorageKind] = mapped_column(_enum(StorageKind, "file_storage"))
    sha256: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    rel_path: Mapped[Optional[str]] = mapped_column(String(300))  # 데이터 디렉터리 기준(첨부)
    uri: Mapped[Optional[str]] = mapped_column(Text)  # 외부 위치
    original_name: Mapped[Optional[str]] = mapped_column(String(300))
    mime: Mapped[Optional[str]] = mapped_column(String(100))
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    caption: Mapped[Optional[str]] = mapped_column(Text)
    produced_by_experiment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("experiments.id"), index=True)
    produced_by_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("runs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version_label"),
        CheckConstraint(_not_blank("version_label"), name="version_label_not_blank"),
        CheckConstraint(
            "(storage = 'attached' AND file_id IS NOT NULL)"
            " OR (storage = 'external' AND external_uri IS NOT NULL)",
            name="storage_location",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), index=True)
    version_label: Mapped[str] = mapped_column(String(60))
    source: Mapped[Optional[str]] = mapped_column(Text)  # 출처
    collected_at: Mapped[Optional[date]] = mapped_column(Date)  # 수집일
    storage: Mapped[StorageKind] = mapped_column(_enum(StorageKind, "dataset_storage"))
    file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("file_objects.id"))
    external_uri: Mapped[Optional[str]] = mapped_column(Text)
    sha256: Mapped[Optional[str]] = mapped_column(String(64))
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    notes: Mapped[Optional[str]] = mapped_column(Text)


file_derivations = Table(
    "file_derivations",
    Base.metadata,
    Column("file_id", ForeignKey("file_objects.id"), primary_key=True),
    Column("dataset_version_id", ForeignKey("dataset_versions.id"), primary_key=True),
)


# ---------------------------------------------------------------- 결정과 기준선 (추가만 가능)


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("project_id", "short_id"),
        CheckConstraint(_not_blank("reason"), name="reason_not_blank"),
        CheckConstraint(
            "(type = 'revoke') = (revokes_decision_id IS NOT NULL)",
            name="revoke_target",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    short_id: Mapped[str] = mapped_column(String(16))
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"), index=True)
    type: Mapped[DecisionType] = mapped_column(_enum(DecisionType, "decision_type"))
    reason: Mapped[str] = mapped_column(Text)
    revokes_decision_id: Mapped[Optional[int]] = mapped_column(ForeignKey("decisions.id"), unique=True)
    revisit_condition: Mapped[Optional[str]] = mapped_column(Text)  # 보류 시 재개 조건
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Baseline(Base):
    __tablename__ = "baselines"
    __table_args__ = (
        UniqueConstraint("project_id", "short_id"),
        CheckConstraint(_not_blank("label"), name="label_not_blank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    short_id: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text)
    source_experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"))
    parent_baseline_id: Mapped[Optional[int]] = mapped_column(ForeignKey("baselines.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BaselineChange(Base):
    __tablename__ = "baseline_changes"
    __table_args__ = (
        CheckConstraint(
            "(kind IN ('adopt', 'revoke')) = (decision_id IS NOT NULL)",
            name="decision_for_adopt_revoke",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[BaselineChangeKind] = mapped_column(_enum(BaselineChangeKind, "baseline_change_kind"))
    from_baseline_id: Mapped[Optional[int]] = mapped_column(ForeignKey("baselines.id"))
    to_baseline_id: Mapped[int] = mapped_column(ForeignKey("baselines.id"))
    decision_id: Mapped[Optional[int]] = mapped_column(ForeignKey("decisions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BaselineRun(Base):
    """기준선 수치의 근거가 되는 실행 스냅샷. 채택 시점(또는 B0 스냅샷 갱신 시점)에 고정됩니다."""

    __tablename__ = "baseline_runs"

    baseline_id: Mapped[int] = mapped_column(ForeignKey("baselines.id"), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    added_by_change_id: Mapped[int] = mapped_column(ForeignKey("baseline_changes.id"))


# ---------------------------------------------------------------- 구조도


class Diagram(Base):
    __tablename__ = "diagrams"
    __table_args__ = (
        UniqueConstraint("project_id", "short_id"),
        CheckConstraint(_not_blank("title"), name="title_not_blank"),
        CheckConstraint(
            "basis = 'planned' OR (commit_sha IS NOT NULL AND length(commit_sha) = 40)",
            name="code_backed_requires_commit",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    short_id: Mapped[str] = mapped_column(String(16))
    kind: Mapped[DiagramKind] = mapped_column(_enum(DiagramKind, "diagram_kind"))
    basis: Mapped[DiagramBasis] = mapped_column(_enum(DiagramBasis, "diagram_basis"))
    title: Mapped[str] = mapped_column(String(200))
    file_id: Mapped[int] = mapped_column(ForeignKey("file_objects.id"))
    json_ir_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("file_objects.id"))
    repo_url: Mapped[Optional[str]] = mapped_column(Text)
    commit_sha: Mapped[Optional[str]] = mapped_column(String(40))
    related_paths: Mapped[Optional[str]] = mapped_column(Text)  # 줄바꿈으로 구분한 저장소 상대 경로
    explanation_ko: Mapped[Optional[str]] = mapped_column(Text)
    linked_problem_id: Mapped[Optional[int]] = mapped_column(ForeignKey("problems.id"))
    linked_experiment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("experiments.id"))
    linked_baseline_id: Mapped[Optional[int]] = mapped_column(ForeignKey("baselines.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
