from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcompass.config import Settings
from rcompass.web.app import create_app
from rcompass.web.routes import projects as project_routes

BASE_URL = "http://127.0.0.1:8765"


def _client(data_dir: Path) -> TestClient:
    return TestClient(create_app(Settings(data_dir=data_dir)), base_url=BASE_URL)


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
    assert 'href="/projects/new"' in response.text
    assert str(tmp_path) in response.text


def test_home_says_outside_repo_for_external_data_dir(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/")
    assert "Git에 올라가지 않습니다" in response.text
    assert "소스 저장소 안에 있습니다" not in response.text


def test_home_warns_when_data_dir_inside_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 실제 저장소에 DB를 만들지 않도록, 임시 폴더를 '소스 체크아웃'으로 간주합니다.
    monkeypatch.setattr(project_routes, "source_checkout_root", lambda: tmp_path)
    with _client(tmp_path / "data") as client:
        response = client.get("/")
    assert "소스 저장소 안에 있습니다" in response.text
    assert "Git에 올라가지 않습니다" not in response.text


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


# ---------------------------------------------------------------- 로컬 앱 보호


def test_unknown_host_header_is_rejected(tmp_path: Path) -> None:
    """DNS 리바인딩: 공격자 도메인을 127.0.0.1로 돌려도 Host 헤더로 막힙니다."""
    with _client(tmp_path) as client:
        response = client.get("/", headers={"host": "evil.example"})
        assert response.status_code == 400
        assert "127.0.0.1" in response.text
        assert client.get("/", headers={"host": "localhost:8765"}).status_code == 200


def test_error_pages_are_korean_html(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        missing = client.get("/projects/999")
        bad_address = client.get("/projects/abc")
        unknown_path = client.get("/nope")
    assert missing.status_code == 404 and "프로젝트를 찾을 수 없습니다" in missing.text
    assert bad_address.status_code == 404 and "주소를 다시 확인해 주세요" in bad_address.text
    assert unknown_path.status_code == 404 and "Not Found" not in unknown_path.text


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"referer": "https://evil.example/form"},
        {"origin": "http://127.0.0.1:9999"},  # 같은 호스트라도 다른 포트는 다른 출처
    ],
)
def test_cross_site_post_is_rejected(tmp_path: Path, headers: dict[str, str]) -> None:
    with _client(tmp_path) as client:
        response = client.post("/projects", data={"title": "x"}, headers=headers)
        assert response.status_code == 403
        assert "아직 프로젝트가 없습니다" in client.get("/").text


def test_same_origin_post_is_accepted(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post("/projects", data={"title": ""}, headers={"origin": BASE_URL})
    assert response.status_code == 422  # 출처 검사는 통과하고 입력 검증에서 걸림
