"""2단계 화면: 새 연구 폼 → 프로젝트 홈."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcompass.config import Settings
from rcompass.web.app import create_app

BASE_URL = "http://127.0.0.1:8765"

FULL_FORM = {
    "title": "QA 평가 오답 줄이기",
    "question": "동일 평가 100문항에서 오답을 18→12개 이하로 줄일 수 있는가?",
    "baseline_label": "현행 프롬프트 v1",
    "next_action": "오답 18개를 유형별로 분류한다",
    "criteria-0-name": "오답 수",
    "criteria-0-metric_key": "wrong_count",
    "criteria-0-direction": "lower",
    "criteria-0-target": "12",
    "criteria-0-unit": "개",
    "criteria-0-aggregate": "mean",
    "criteria-0-min_runs": "3",
    "criteria-0-baseline_values": "17, 18, 19",
    "criteria-1-name": "평균 응답 시간",
    "criteria-1-metric_key": "avg_latency_s",
    "criteria-1-direction": "lower",
    "criteria-1-target": "2.0",
    "criteria-1-unit": "초",
    "criteria-1-aggregate": "mean",
    "criteria-1-min_runs": "1",
    "criteria-1-baseline_values": "",
}


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(data_dir=tmp_path)), base_url=BASE_URL) as client:
        yield client


def test_new_project_form_renders(client: TestClient) -> None:
    response = client.get("/projects/new")
    assert response.status_code == 200
    assert 'name="criteria-0-name"' in response.text
    assert 'name="criteria-1-name"' in response.text  # 기본 2행
    assert "나중에 채워도 됩니다" in response.text


def test_create_project_redirects_to_home_with_criteria(client: TestClient) -> None:
    response = client.post("/projects", data=FULL_FORM, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/projects/")

    page = client.get(location).text
    assert "동일 평가 100문항에서 오답을 18→12개 이하로 줄일 수 있는가?" in page
    assert "원래 질문" in page  # 맥락 바
    assert "18.0 ± 1.0 (n=3)" in page
    assert "미달" in page and "미측정" in page
    assert "B0" in page and "현행 프롬프트 v1" in page
    assert "오답 18개를 유형별로 분류한다" in page

    home = client.get("/").text
    assert "QA 평가 오답 줄이기" in home
    assert "아직 프로젝트가 없습니다" not in home


def test_required_only_project(client: TestClient) -> None:
    form = {
        "title": "최소 입력",
        "question": "가능한가?",
        "baseline_label": "현재",
        "criteria-0-name": "정확도",
        "criteria-0-metric_key": "accuracy",
        "criteria-0-direction": "higher",
        "criteria-0-target": "0.9",
    }
    response = client.post("/projects", data=form)
    assert response.status_code == 200
    assert "미측정" in response.text
    assert "≥ 0.9" in response.text


def test_blank_extra_rows_are_ignored(client: TestClient) -> None:
    form = dict(FULL_FORM)
    form.update({f"criteria-2-{k}": "" for k in ("name", "metric_key", "target", "unit", "baseline_values")})
    response = client.post("/projects", data=form, follow_redirects=False)
    assert response.status_code == 303


def test_validation_errors_keep_input_and_point_to_fields(client: TestClient) -> None:
    form = dict(FULL_FORM)
    form.update({"title": "", "criteria-1-metric_key": "평균 시간", "criteria-0-baseline_values": "17, 열여덟"})
    response = client.post("/projects", data=form)
    assert response.status_code == 422
    text = response.text
    assert "입력을 확인해 주세요" in text
    assert "제목을 입력하세요" in text
    assert 'id="criteria-1-metric_key-error"' in text
    assert 'id="criteria-0-baseline_values-error"' in text
    assert 'value="동일 평가' not in text  # textarea는 value가 아니라 본문
    assert "동일 평가 100문항에서" in text  # 입력한 질문 유지
    assert 'value="17, 열여덟"' in text


def test_form_without_any_criterion_is_rejected(client: TestClient) -> None:
    response = client.post("/projects", data={"title": "t", "question": "q", "baseline_label": "b"})
    assert response.status_code == 422
    assert "성공 기준을 하나 이상" in response.text


@pytest.mark.parametrize("min_runs", ["²", "0", "101", "99999999999999999999", "abc"])
def test_invalid_min_runs_is_a_field_error_not_a_crash(client: TestClient, min_runs: str) -> None:
    form = dict(FULL_FORM, **{"criteria-0-min_runs": min_runs})
    response = client.post("/projects", data=form)
    assert response.status_code == 422
    assert 'id="criteria-0-min_runs-error"' in response.text


def test_too_many_baseline_values_rejected(client: TestClient) -> None:
    form = dict(FULL_FORM, **{"criteria-0-baseline_values": ", ".join(["1"] * 101)})
    response = client.post("/projects", data=form)
    assert response.status_code == 422
    assert 'id="criteria-0-baseline_values-error"' in response.text


def test_service_errors_point_to_screen_rows(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """빈 0행 + 채운 1행일 때, 서비스가 낸 '기준 0번' 오류는 화면 1행에 표시돼야 합니다."""
    from rcompass.services import ValidationError
    from rcompass.web.routes import projects as routes

    def raise_error(session, data):
        raise ValidationError({"criteria-0-metric_key": "이미 쓰는 지표 키입니다."})

    monkeypatch.setattr(routes, "create_project", raise_error)
    # 브라우저는 비어 있는 0행도 함께 보냅니다.
    form = {key.replace("criteria-0-", "criteria-1-"): value for key, value in FULL_FORM.items()
            if not key.startswith("criteria-1-")}
    form.update({f"criteria-0-{k}": "" for k in ("name", "metric_key", "target", "unit", "baseline_values")})
    response = client.post("/projects", data=form)
    assert response.status_code == 422
    assert 'id="criteria-1-metric_key-error"' in response.text


def test_targets_and_small_values_display_as_entered(client: TestClient) -> None:
    form = dict(FULL_FORM, **{
        "criteria-0-target": "1234567", "criteria-0-baseline_values": "0.0004", "criteria-0-min_runs": "1",
        "criteria-1-target": "0.1234567",
    })
    location = client.post("/projects", data=form, follow_redirects=False).headers["location"]
    page = client.get(location).text
    assert "≤ 1234567" in page
    assert "≤ 0.1234567" in page
    assert "0.0004" in page


def test_html_is_escaped(client: TestClient) -> None:
    form = dict(FULL_FORM, title="<script>alert(1)</script>")
    location = client.post("/projects", data=form, follow_redirects=False).headers["location"]
    page = client.get(location).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
