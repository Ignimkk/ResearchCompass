"""데이터 디렉터리 결정과 앱 설정.

개인 연구 데이터(DB, 첨부 파일, 백업)는 항상 저장소 밖에 둡니다.
우선순위: ``--data-dir`` 옵션 > ``RCOMPASS_DATA_DIR`` 환경변수 > 운영체제 사용자 데이터 경로.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from platformdirs import user_data_path

APP_NAME = "rcompass"
ENV_DATA_DIR = "RCOMPASS_DATA_DIR"
DB_FILENAME = "rcompass.db"

DataDirSource = Literal["cli", "env", "default"]

SOURCE_LABELS: dict[DataDirSource, str] = {
    "cli": "--data-dir 옵션",
    "env": f"{ENV_DATA_DIR} 환경변수",
    "default": "기본 사용자 데이터 경로",
}


@dataclass(frozen=True)
class Settings:
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME


def default_data_dir() -> Path:
    """운영체제별 사용자 데이터 경로 (Windows: %LOCALAPPDATA%\\rcompass)."""
    return user_data_path(APP_NAME, appauthor=False)


def resolve_data_dir(
    cli_value: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, DataDirSource]:
    """사용할 데이터 디렉터리와 그 출처를 돌려줍니다."""
    if env is None:
        env = os.environ
    cli_text = "" if cli_value is None else os.fspath(cli_value).strip()
    if cli_text:
        return Path(cli_text).expanduser().resolve(), "cli"
    env_value = env.get(ENV_DATA_DIR, "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve(), "env"
    return default_data_dir().resolve(), "default"


def source_checkout_root() -> Path | None:
    """소스 체크아웃(클론)에서 실행 중이면 그 루트를, 설치본이면 None을 돌려줍니다."""
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").is_file() and (candidate / "src" / APP_NAME).is_dir():
        return candidate
    return None


def is_inside(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
