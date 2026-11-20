from pathlib import Path

from fastapi.testclient import TestClient

from rcompass.config import Settings, source_checkout_root
from rcompass.web.app import create_app


def _client(data_dir: Path) -> TestClient:
    return TestClient(create_app(Settings(data_dir=data_dir)))


def test_healthz(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_empty_home_shows_korean_message_and_data_dir(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "아직 프로젝트가 없습니다" in response.text
    assert str(tmp_path) in response.text


def test_home_says_outside_repo_for_external_data_dir(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/")
    assert "Git에 올라가지 않습니다" in response.text
    assert "소스 저장소 안에 있습니다" not in response.text


def test_home_warns_when_data_dir_inside_checkout() -> None:
    root = source_checkout_root()
    assert root is not None
    # lifespan(DB 생성)을 실행하지 않도록 with 블록 없이 요청합니다: 저장소에 파일이 생기지 않습니다.
    client = TestClient(create_app(Settings(data_dir=root / "data")))
    response = client.get("/")
    assert "소스 저장소 안에 있습니다" in response.text
    assert "Git에 올라가지 않습니다" not in response.text
    assert not (root / "data").exists()


def test_no_files_created_before_startup(tmp_path: Path) -> None:
    data_dir = tmp_path / "not-yet"
    create_app(Settings(data_dir=data_dir))
    assert not data_dir.exists()


def test_db_file_created_in_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "nested" / "data"
    with _client(data_dir):
        pass
    assert (data_dir / "rcompass.db").is_file()


def test_static_css_served(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/static/app.css")
    assert response.status_code == 200
