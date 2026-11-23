"""데이터셋과 버전 (출처, 수집일, 위치). 4단계는 외부 참조만, 첨부 파일은 5단계에서 추가합니다."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.ids import next_short_id
from rcompass.models import Dataset, DatasetVersion, Project, StorageKind
from rcompass.services import ValidationError

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
    external_uri: str,
    source: str | None = None,
    collected_at: date | None = None,
    sha256: str | None = None,
    notes: str | None = None,
) -> VersionView:
    """데이터 버전을 등록합니다. 같은 이름의 데이터셋이 없으면 새로 만듭니다. 호출한 쪽이 커밋합니다."""
    errors: dict[str, str] = {}
    name = (dataset_name or "").strip()
    label = (version_label or "").strip()
    uri = (external_uri or "").strip()
    digest = (sha256 or "").strip().lower() or None
    if not name:
        errors["dataset_name"] = "데이터셋 이름을 입력하세요."
    if not label:
        errors["version_label"] = "버전을 입력하세요. 예: v1.0"
    if not uri:
        errors["external_uri"] = "데이터 위치(경로나 URL)를 입력하세요."
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
        storage=StorageKind.EXTERNAL,
        external_uri=uri,
        sha256=digest,
        notes=(notes or "").strip() or None,
    )
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
