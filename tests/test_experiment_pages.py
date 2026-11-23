"""4단계 화면: 문제에서 실험 만들기 → 입력 → 결과 붙여넣기 → 집계 → 제외."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcompass.config import Settings
from rcompass.web.app import create_app

BASE_URL = "http://127.0.0.1:8765"


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(data_dir=tmp_path)), base_url=BASE_URL) as client:
        client.post("/projects", data={
            "title": "QA", "question": "오답을 12개 이하로?", "baseline_label": "현행 프롬프트 v1",
            "criteria-0-name": "오답 수", "criteria-0-metric_key": "wrong_count", "criteria-0-direction": "lower",
            "criteria-0-target": "12", "criteria-0-min_runs": "3", "criteria-0-baseline_values": "17, 18, 19",
        })
        client.post("/projects/1/problems", data={"title": "장문 지문 오답", "why": "18개 중 7개가 장문"})
        yield client


def _create_trial(client: TestClient, **overrides):
    data = {"problem": "P1", "title": "few-shot 예시 3개", "hypothesis": "예시를 넣으면 장문 오답이 준다",
            "decision_rule": "평균 ≤ 17이면 채택, ≥ 18이면 기각"}
    data.update(overrides)
    return client.post("/projects/1/experiments", data=data, follow_redirects=False)


def test_problem_page_offers_new_experiment(client: TestClient) -> None:
    page = client.get("/projects/1/problems/P1").text
    assert 'href="/projects/1/problems/P1/experiments/new"' in page
    form = client.get("/projects/1/problems/P1/experiments/new").text
    assert "가설" in form and "판정 기준" in form and "나중에 채워도 됩니다" in form


def test_create_experiment_and_context(client: TestClient) -> None:
    response = _create_trial(client)
    assert response.headers["location"] == "/projects/1/experiments/E1"
    page = client.get("/projects/1/experiments/E1").text
    bar = page.split('class="context-bar"')[1].split("</nav>")[0]
    assert "P1 장문 지문 오답" in bar and "E1 few-shot 예시 3개" in bar
    assert '돌아갈 곳: <a href="/projects/1/problems/P1">' in bar
    assert "출발 기준선: <span class=\"id\">B0</span>" in page
    assert "E1</span> few-shot 예시 3개" in client.get("/projects/1/problems/P1").text


def test_missing_hypothesis_keeps_input(client: TestClient) -> None:
    response = _create_trial(client, hypothesis="")
    assert response.status_code == 422
    assert "가설을 먼저 적어 주세요" in response.text
    assert 'value="few-shot 예시 3개"' in response.text


def test_paste_results_shows_runs_and_aggregate(client: TestClient) -> None:
    _create_trial(client)
    response = client.post("/projects/1/experiments/E1/results",
                           data={"results": "label\twrong_count\nseed=1\t18\nseed=2\t19\nseed=3\t20"},
                           follow_redirects=False)
    assert response.status_code == 303 and "saved=results" in response.headers["location"]
    page = client.get(response.headers["location"]).text
    assert "결과를 기록했습니다" in page
    assert "seed=2" in page and "19.0 ± 1.0 (n=3)" in page and "범위 18 ~ 20" in page


def test_bad_table_shows_line_errors_and_keeps_text(client: TestClient) -> None:
    _create_trial(client)
    text = "label\twrong_count\nseed=1\t열아홉"
    response = client.post("/projects/1/experiments/E1/results", data={"results": text})
    assert response.status_code == 422
    assert "2번째 줄" in response.text and "숫자가 아닙니다" in response.text
    assert "seed=1\t열아홉" in response.text


def test_exclude_run(client: TestClient) -> None:
    _create_trial(client)
    client.post("/projects/1/experiments/E1/results", data={"results": "label\twrong_count\na\t18\nb\t19\nc\t30"})
    assert client.post("/projects/1/experiments/E1/runs/R3/exclude", data={"reason": ""}).status_code == 422
    client.post("/projects/1/experiments/E1/runs/R3/exclude", data={"reason": "로그 손상"})
    page = client.get("/projects/1/experiments/E1").text
    assert "제외: 로그 손상" in page and "18.5 ± 0.7 (n=2)" in page and "제외 뺀 2개" in page


def test_e0_page_shows_locked_baseline_runs_and_extends_snapshot(client: TestClient) -> None:
    page = client.get("/projects/1/experiments/E0").text
    assert "기준선 측정" in page and "B0 근거" in page
    locked = client.post("/projects/1/experiments/E0/results", data={"results": "label\twrong_count\n실행 1\t5"})
    assert locked.status_code == 422 and "근거라" in locked.text
    response = client.post("/projects/1/experiments/E0/results", data={"results": "label\twrong_count\nseed=4\t18"},
                           follow_redirects=False)
    assert "snapshot=B0" in response.headers["location"]
    assert "B0의 근거에 더했습니다" in client.get(response.headers["location"]).text
    assert "18.0 ± 0.8 (n=4)" in client.get("/projects/1").text


def test_plan_edit_after_results_needs_confirmation(client: TestClient) -> None:
    _create_trial(client)
    client.post("/projects/1/experiments/E1/results", data={"results": "wrong_count\n19"})
    plan = {"title": "few-shot 예시 3개", "hypothesis": "예시를 넣으면 장문 오답이 준다", "decision_rule": "평균 ≤ 19이면 채택"}
    response = client.post("/projects/1/experiments/E1/plan", data=plan)
    assert response.status_code == 422 and "확인란에 체크하세요" in response.text
    client.post("/projects/1/experiments/E1/plan", data={**plan, "confirm": "1"})
    page = client.get("/projects/1/experiments/E1").text
    assert "변경 이력 1건" in page and "평균 ≤ 17이면 채택, ≥ 18이면 기각" in page


def test_dataset_registration_and_inputs(client: TestClient) -> None:
    _create_trial(client)
    response = client.post("/projects/1/datasets", data={
        "dataset_name": "eval-100", "version_label": "v1.0", "external_uri": "D:/data/eval100_v1.jsonl",
        "source": "내부 QA 수집", "collected_at": "2026-11-02",
        "back": "/projects/1/experiments/E1"}, follow_redirects=False)
    assert response.headers["location"] == "/projects/1/experiments/E1"
    client.post("/projects/1/experiments/E1/inputs",
                data={"dataset_version": "1", "code_ref": "3f2a9c1", "config_text": "shots: 3"})
    page = client.get("/projects/1/experiments/E1").text
    assert 'value="1" checked' in page and 'value="3f2a9c1"' in page
    assert "이 값을 잰 데이터 버전" in page
    datasets = client.get("/projects/1/datasets").text
    assert "DS1</span> eval-100 <strong>v1.0</strong>" in datasets and "2026-11-02" in datasets


def test_dataset_errors_and_unsafe_back_link(client: TestClient) -> None:
    bad = client.post("/projects/1/datasets", data={"dataset_name": "x", "version_label": "v1",
                                                     "external_uri": "p", "collected_at": "11/02"})
    assert bad.status_code == 422 and "2026-11-02 형식" in bad.text
    for back in ("https://evil.example/", "//evil.example", "/projects/2/x"):
        response = client.post("/projects/1/datasets", data={
            "dataset_name": "d" + str(len(back)), "version_label": "v1", "external_uri": "p", "back": back},
            follow_redirects=False)
        assert response.headers["location"] == "/projects/1/datasets"


def _register_version(client: TestClient) -> None:
    client.post("/projects/1/datasets", data={"dataset_name": "eval-100", "version_label": "v1.0",
                                              "external_uri": "D:/data/eval100_v1.jsonl"})


def test_failed_dataset_registration_keeps_return_link(client: TestClient) -> None:
    _create_trial(client)
    back = "/projects/1/experiments/E1"
    failed = client.post("/projects/1/datasets", data={"dataset_name": "x", "version_label": "", "external_uri": "p",
                                                       "back": back})
    assert failed.status_code == 422 and f'name="back" value="{back}"' in failed.text
    ok = client.post("/projects/1/datasets", data={"dataset_name": "x", "version_label": "v1", "external_uri": "p",
                                                   "back": back}, follow_redirects=False)
    assert ok.headers["location"] == back


def test_results_error_keeps_no_dataset_choice(client: TestClient) -> None:
    _create_trial(client)
    _register_version(client)
    client.post("/projects/1/experiments/E1/inputs", data={"dataset_version": "1"})
    page = client.post("/projects/1/experiments/E1/results",
                       data={"results": "wrong_count\nabc", "dataset_version": ""}).text
    select = page.split('name="dataset_version">')[1].split("</select>")[0]
    assert "selected" not in select  # '기록하지 않음'(첫 옵션, selected 없음)이 유지됨
    fresh = client.get("/projects/1/experiments/E1").text.split('name="dataset_version">')[1].split("</select>")[0]
    assert 'value="1" selected' in fresh  # 처음 열 때는 입력 데이터 버전이 기본


def test_failed_inputs_save_keeps_typed_values(client: TestClient) -> None:
    _create_trial(client)
    response = client.post("/projects/1/experiments/E1/inputs",
                           data={"dataset_version": "abc", "code_ref": "new-sha", "config_text": "new cfg"})
    assert response.status_code == 422
    assert 'value="new-sha"' in response.text and "new cfg</textarea>" in response.text


def test_unknown_experiment_and_run(client: TestClient) -> None:
    assert client.get("/projects/1/experiments/E9").status_code == 404
    _create_trial(client)
    assert client.post("/projects/1/experiments/E1/runs/R9/exclude", data={"reason": "x"}).status_code == 404
    assert _create_trial(client, problem="P9").status_code == 404
