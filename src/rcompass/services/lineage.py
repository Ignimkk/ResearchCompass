"""계보: 결과 파일(그래프·표)에서 원본 실험, 데이터 버전, 코드, 설정, 결정, 기준선까지 거슬러 올라갑니다.

계획서 4절의 역추적 예시를 그대로 재현하는 데이터입니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.models import Baseline, Decision, Experiment, FileKind, FileObject, Problem, Run, file_derivations
from rcompass.services.datasets import VersionView, get_version
from rcompass.services.experiments import input_versions
from rcompass.services.problems import problem_path
from rcompass.services.runs import ResultsView, experiment_results


@dataclass
class Lineage:
    file: FileObject
    run: Run | None = None
    experiment: Experiment | None = None
    path: list[Problem] = field(default_factory=list)
    derived_from: list[VersionView] = field(default_factory=list)  # 이 파일을 만든 데이터 버전
    experiment_inputs: list[VersionView] = field(default_factory=list)
    config_files: list[FileObject] = field(default_factory=list)
    results: ResultsView | None = None
    decisions: list[Decision] = field(default_factory=list)
    baselines: list[Baseline] = field(default_factory=list)  # 이 실험을 근거로 만든 기준선


def file_lineage(session: Session, record: FileObject) -> Lineage:
    lineage = Lineage(file=record)
    version_ids = session.scalars(
        select(file_derivations.c.dataset_version_id).where(file_derivations.c.file_id == record.id)
    ).all()
    lineage.derived_from = [v for v in (get_version(session, record.project_id, i) for i in version_ids) if v]
    if record.produced_by_run_id:
        lineage.run = session.get(Run, record.produced_by_run_id)
    if record.produced_by_experiment_id is None:
        return lineage

    experiment = session.get(Experiment, record.produced_by_experiment_id)
    lineage.experiment = experiment
    if experiment.problem_id:
        lineage.path = problem_path(session, session.get(Problem, experiment.problem_id))
    lineage.experiment_inputs = input_versions(session, experiment)
    lineage.config_files = list(session.scalars(
        select(FileObject).where(FileObject.produced_by_experiment_id == experiment.id,
                                 FileObject.kind == FileKind.CONFIG, FileObject.id != record.id)
        .order_by(FileObject.id)
    ))
    lineage.results = experiment_results(session, experiment)
    lineage.decisions = list(session.scalars(
        select(Decision).where(Decision.experiment_id == experiment.id).order_by(Decision.id)
    ))
    lineage.baselines = list(session.scalars(
        select(Baseline).where(Baseline.source_experiment_id == experiment.id).order_by(Baseline.id)
    ))
    return lineage
