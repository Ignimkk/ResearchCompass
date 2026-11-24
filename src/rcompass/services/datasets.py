"""데이터셋과 버전 (출처, 수집일, 위치, 지문).

버전은 두 가지로 저장합니다.
- 외부 참조: 위치(로컬·NAS 경로나 URL)와 선택적 sha256만 기록합니다. 백업에 포함되지 않습니다.
- 첨부: 작은 파일을 데이터 디렉터리에 복사합니다(services.files). 백업에 포함됩니다.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.ids import next_short_id
from rcompass.models import Dataset, DatasetVersion, Experiment, FileObject, Project, StorageKind, utcnow
from rcompass.models import experiment_inputs
from rcompass.services import ValidationError
from rcompass.services.files import Fingerprint, fingerprint_file, local_path_of

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class VersionView:
    dataset: Dataset
    version: DatasetVersion

    @property
    def label(self) -> str:
        return f"{self.dataset.short_id} {self.dataset.name} {self.version.version_label}"


def add_dataset_version(
    session: Session,
    project: Project,
    *,
    dataset_name: str,
    version_label: str,
    external_uri: str = "",
    source: str | None = None,
    collected_at: date | None = None,
    sha256: str | None = None,
    notes: str | None = None,
    attached_file: FileObject | None = None,
) -> VersionView:
    """데이터 버전을 등록합니다. 같은 이름의 데이터셋이 없으면 새로 만듭니다. 호출한 쪽이 커밋합니다.

    ``attached_file``이 있으면 첨부 버전이고 위치와 sha256은 파일에서 가져옵니다.
    """
    errors: dict[str, str] = {}
    name = (dataset_name or "").strip()
    label = (version_label or "").strip()
    uri = (external_uri or "").strip()
    digest = (sha256 or "").strip().lower() or None
    if not name:
        errors["dataset_name"] = "데이터셋 이름을 입력하세요."
    if not label:
        errors["version_label"] = "버전을 입력하세요. 예: v1.0"
    if attached_file is None and not uri:
        errors["external_uri"] = "데이터 위치(경로나 URL)를 입력하거나 작은 파일을 첨부하세요."
    if attached_file is not None and uri:
        errors["external_uri"] = "위치와 첨부 파일 중 하나만 고르세요."
    if digest and not SHA256_PATTERN.match(digest):
        errors["sha256"] = "sha256은 16진수 64자리입니다."

    dataset = None
    if name:
        dataset = session.scalars(
            select(Dataset).where(Dataset.project_id == project.id, Dataset.name == name)
        ).one_or_none()
    if dataset is not None and label:
        exists = session.scalars(
            select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id,
                                         DatasetVersion.version_label == label)
        ).one_or_none()
        if exists is not None:
            errors["version_label"] = f"'{name}'에는 이미 {label} 버전이 있습니다."
    if errors:
        raise ValidationError(errors)

    if dataset is None:
        dataset = Dataset(project_id=project.id, short_id=next_short_id(session, project.id, "DS"), name=name)
        session.add(dataset)
        session.flush()
    version = DatasetVersion(
        dataset_id=dataset.id,
        version_label=label,
        source=(source or "").strip() or None,
        collected_at=collected_at,
        notes=(notes or "").strip() or None,
    )
    if attached_file is not None:
        version.storage = StorageKind.ATTACHED
        version.file_id = attached_file.id
        version.sha256 = attached_file.sha256
        version.size_bytes = attached_file.size_bytes
        version.last_verified_at = utcnow()
    else:
        version.storage = StorageKind.EXTERNAL
        version.external_uri = uri
        version.sha256 = digest
    session.add(version)
    session.flush()
    return VersionView(dataset, version)


def list_versions(session: Session, project_id: int) -> list[VersionView]:
    rows = session.execute(
        select(Dataset, DatasetVersion)
        .join(DatasetVersion, DatasetVersion.dataset_id == Dataset.id)
        .where(Dataset.project_id == project_id)
        .order_by(Dataset.id, DatasetVersion.id)
    )
    return [VersionView(dataset, version) for dataset, version in rows]


def get_version(session: Session, project_id: int, version_id: int) -> VersionView | None:
    row = session.execute(
        select(Dataset, DatasetVersion)
        .join(DatasetVersion, DatasetVersion.dataset_id == Dataset.id)
        .where(Dataset.project_id == project_id, DatasetVersion.id == version_id)
    ).one_or_none()
    return VersionView(*row) if row else None


def version_usage(session: Session, project_id: int) -> dict[int, list[Experiment]]:
    """데이터 버전 id → 그 버전을 입력으로 쓴 실험들."""
    rows = session.execute(
        select(experiment_inputs.c.dataset_version_id, Experiment)
        .join(Experiment, Experiment.id == experiment_inputs.c.experiment_id)
        .where(Experiment.project_id == project_id)
        .order_by(Experiment.id)
    )
    usage: dict[int, list[Experiment]] = {}
    for version_id, experiment in rows:
        usage.setdefault(version_id, []).append(experiment)
    return usage


class VerifyResult(str, enum.Enum):
    RECORDED = "recorded"  # 처음 계산해 기록함
    MATCH = "match"  # 기록한 지문과 같음
    MISMATCH = "mismatch"  # 기록한 지문과 다름(기록은 그대로 둠)
    MISSING = "missing"  # 파일을 찾을 수 없음
    NOT_LOCAL = "not_local"  # URL 등 로컬 경로가 아님


VERIFY_MESSAGES = {
    VerifyResult.RECORDED: "지문(sha256)을 계산해 기록했습니다.",
    VerifyResult.MATCH: "기록한 지문과 같습니다. 데이터가 바뀌지 않았습니다.",
    VerifyResult.MISMATCH: "기록한 지문과 다릅니다. 데이터가 바뀌었을 수 있습니다. 기록은 그대로 두었습니다.",
    VerifyResult.MISSING: "이 위치에서 파일을 찾을 수 없습니다.",
    VerifyResult.NOT_LOCAL: "URL이나 상대 경로는 지문을 계산하지 않습니다. 로컬·NAS 절대 경로를 적어 주세요.",
}


def verification_target(view: VersionView) -> Path | VerifyResult:
    """지문을 계산할 로컬 파일 경로, 또는 계산 없이 끝나는 결과(URL, 파일 없음)."""
    version = view.version
    if version.storage is not StorageKind.EXTERNAL:
        raise ValidationError({"verify": "첨부 버전은 저장할 때 지문을 기록했습니다."})
    path = local_path_of(version.external_uri or "")
    if path is None:
        return VerifyResult.NOT_LOCAL
    if not path.is_file():
        return VerifyResult.MISSING
    return path


def compute_fingerprint(path: Path) -> Fingerprint:
    """원본 파일을 읽기만 해서 sha256을 계산합니다. DB 트랜잭션 밖에서 부르세요(큰 파일은 오래 걸림)."""
    return fingerprint_file(path)


def apply_fingerprint(session: Session, version: DatasetVersion, fingerprint: Fingerprint) -> VerifyResult:
    """계산한 지문을 기록하거나 대조합니다. 불일치여도 기존 기록은 바꾸지 않습니다."""
    version.last_verified_at = utcnow()
    if version.sha256 is None:
        version.sha256 = fingerprint.sha256
        version.size_bytes = fingerprint.size
        result = VerifyResult.RECORDED
    elif version.sha256 == fingerprint.sha256:
        result = VerifyResult.MATCH
    else:
        result = VerifyResult.MISMATCH
    session.flush()
    return result


def verify_external(session: Session, view: VersionView) -> VerifyResult:
    """한 번에 확인하는 편의 함수(테스트·CLI용). 웹 라우트는 해시 계산 중 잠금을 잡지 않도록 단계를 나눠 씁니다."""
    target = verification_target(view)
    if isinstance(target, VerifyResult):
        return target
    return apply_fingerprint(session, view.version, compute_fingerprint(target))
