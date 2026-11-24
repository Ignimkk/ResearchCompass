"""라우트 공용 의존성과 조회 도우미."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from rcompass.models import Experiment, Project


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessions() as session:
        yield session


def project_or_404(session: Session, project_id: int) -> Project:
    from rcompass.services.projects import get_project

    project = get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    return project


def experiment_or_404(session: Session, project: Project, short_id: str) -> Experiment:
    from rcompass.services.experiments import get_experiment

    experiment = get_experiment(session, project.id, short_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="실험을 찾을 수 없습니다.")
    return experiment


def safe_back(project: Project, back: str, default: str) -> str:
    """돌아갈 주소는 이 프로젝트 안의 경로만 허용합니다(열린 리디렉션 방지)."""
    ok = back.startswith(f"/projects/{project.id}/") and "//" not in back and "\\" not in back
    return back if ok else default
