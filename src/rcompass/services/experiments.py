"""실험 (여정 3: 가설과 판정 기준을 먼저 적고, 입력·설정·코드 버전을 연결)."""

from __future__ import annotations

from sqlalchemy import delete, exists, insert, select
from sqlalchemy.orm import Session

from rcompass.ids import next_short_id
from rcompass.models import (
    Experiment,
    ExperimentKind,
    ExperimentRevision,
    MetricResult,
    Problem,
    Project,
    Run,
    experiment_inputs,
)
from rcompass.services import ValidationError
from rcompass.services.datasets import VersionView, get_version
from rcompass.services.problems import get_problem

TRACKED_FIELDS = ("hypothesis", "decision_rule")  # 결과를 본 뒤 바뀌면 판정이 흔들리는 항목


def _clean(text: str | None) -> str | None:
    return (text or "").strip() or None


def get_experiment(session: Session, project_id: int, short_id: str) -> Experiment | None:
    return session.scalars(
        select(Experiment).where(Experiment.project_id == project_id, Experiment.short_id == short_id)
    ).one_or_none()


def create_experiment(
    session: Session,
    project: Project,
    *,
    problem_short_id: str,
    title: str,
    hypothesis: str,
    decision_rule: str,
    code_ref: str | None = None,
    config_text: str | None = None,
) -> Experiment:
    """실험을 만듭니다. 출발 기준선은 지금의 현재 기준선입니다. 호출한 쪽이 커밋합니다."""
    errors: dict[str, str] = {}
    problem = get_problem(session, project.id, problem_short_id) if problem_short_id else None
    if problem is None:
        errors["problem"] = "실험은 문제 하나에 속해야 합니다."
    if not _clean(title):
        errors["title"] = "실험 제목을 입력하세요."
    if not _clean(hypothesis):
        errors["hypothesis"] = "가설을 먼저 적어 주세요. 결과를 본 뒤 가설을 맞추지 않도록 실험 전에 기록합니다."
    if not _clean(decision_rule):
        errors["decision_rule"] = "판정 기준을 먼저 적어 주세요. 예: 평균 오답 ≤ 17이면 채택, ≥ 18이면 기각"
    if errors:
        raise ValidationError(errors)
    experiment = Experiment(
        project_id=project.id,
        short_id=next_short_id(session, project.id, "E"),
        kind=ExperimentKind.TRIAL,
        problem_id=problem.id,
        based_on_baseline_id=project.current_baseline_id,
        title=_clean(title),
        hypothesis=_clean(hypothesis),
        decision_rule=_clean(decision_rule),
        code_ref=_clean(code_ref),
        config_text=_clean(config_text),
    )
    session.add(experiment)
    session.flush()
    return experiment


def has_results(session: Session, experiment: Experiment) -> bool:
    return bool(session.scalar(
        select(exists().where(MetricResult.run_id == Run.id, Run.experiment_id == experiment.id))
    ))


def update_plan(
    session: Session,
    experiment: Experiment,
    *,
    title: str,
    hypothesis: str,
    decision_rule: str,
    confirm_after_results: bool = False,
) -> list[ExperimentRevision]:
    """제목, 가설, 판정 기준을 고칩니다. 가설과 판정 기준의 변경은 이력으로 남깁니다.

    결과가 이미 있으면 ``confirm_after_results``가 참일 때만 고칩니다(결과를 보고 기준을 바꾸는 것을 막기 위한 확인).
    """
    if experiment.kind is not ExperimentKind.TRIAL:
        raise ValidationError({"hypothesis": "기준선 측정 실험에는 가설이 없습니다."})
    new_values = {"hypothesis": _clean(hypothesis), "decision_rule": _clean(decision_rule)}
    errors: dict[str, str] = {}
    if not _clean(title):
        errors["title"] = "실험 제목을 입력하세요."
    if not new_values["hypothesis"]:
        errors["hypothesis"] = "가설은 비울 수 없습니다."
    if not new_values["decision_rule"]:
        errors["decision_rule"] = "판정 기준은 비울 수 없습니다."
    changed = [f for f in TRACKED_FIELDS if new_values[f] != getattr(experiment, f)]
    if changed and not confirm_after_results and has_results(session, experiment):
        errors["confirm"] = "이미 결과가 있습니다. 결과를 본 뒤 가설이나 판정 기준을 바꾸는 것이 맞는지 확인란에 체크하세요."
    if errors:
        raise ValidationError(errors)

    revisions = []
    for field in changed:
        revision = ExperimentRevision(experiment_id=experiment.id, field=field,
                                      old_value=getattr(experiment, field), new_value=new_values[field])
        session.add(revision)
        revisions.append(revision)
        setattr(experiment, field, new_values[field])
    experiment.title = _clean(title)
    session.flush()
    return revisions


def revisions(session: Session, experiment: Experiment) -> list[ExperimentRevision]:
    return list(session.scalars(
        select(ExperimentRevision).where(ExperimentRevision.experiment_id == experiment.id)
        .order_by(ExperimentRevision.id)
    ))


def update_inputs(
    session: Session,
    experiment: Experiment,
    *,
    dataset_version_ids: list[int],
    code_ref: str | None,
    config_text: str | None,
) -> None:
    """입력 데이터 버전, 코드 버전(커밋 SHA 또는 메모), 설정을 저장합니다."""
    versions = [get_version(session, experiment.project_id, vid) for vid in dataset_version_ids]
    if any(v is None for v in versions):
        raise ValidationError({"inputs": "선택한 데이터 버전을 찾을 수 없습니다."})
    session.execute(delete(experiment_inputs).where(experiment_inputs.c.experiment_id == experiment.id))
    for version_id in dict.fromkeys(dataset_version_ids):
        session.execute(insert(experiment_inputs).values(
            experiment_id=experiment.id, dataset_version_id=version_id, role="eval"))
    experiment.code_ref = _clean(code_ref)
    experiment.config_text = _clean(config_text)
    session.flush()


def input_versions(session: Session, experiment: Experiment) -> list[VersionView]:
    ids = session.scalars(
        select(experiment_inputs.c.dataset_version_id).where(experiment_inputs.c.experiment_id == experiment.id)
    ).all()
    return [v for v in (get_version(session, experiment.project_id, vid) for vid in ids) if v]


def problem_experiments(session: Session, problem: Problem) -> list[Experiment]:
    return list(session.scalars(
        select(Experiment).where(Experiment.problem_id == problem.id).order_by(Experiment.id)
    ))
