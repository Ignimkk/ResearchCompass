"""3단계 화면: 문제 트리, 깊은 하위 문제의 맥락 바, 보류 서랍, 집중 문제."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcompass.config import Settings
from rcompass.web.app import create_app

BASE_URL = "http://127.0.0.1:8765"
QUESTION = "동일 평가 100문항에서 오답을 18→12개 이하로 줄일 수 있는가?"


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(data_dir=tmp_path)), base_url=BASE_URL) as client:
        response = client.post("/projects", data={
            "title": "QA 평가 오답 줄이기", "question": QUESTION, "baseline_label": "현행 프롬프트 v1",
            "criteria-0-name": "오답 수", "criteria-0-metric_key": "wrong_count", "criteria-0-direction": "lower",
            "criteria-0-target": "12", "criteria-0-baseline_values": "17, 18, 19",
        }, follow_redirects=False)
        assert response.headers["location"] == "/projects/1"
        yield client


def _add(client: TestClient, title: str, why: str, parent: str = "", parked: bool = False):
    data = {"title": title, "why": why, "parent": parent}
    if parked:
        data["parked"] = "1"
    return client.post("/projects/1/problems", data=data, follow_redirects=False)


def test_empty_tree_invites_first_problem(client: TestClient) -> None:
    page = client.get("/projects/1/problems").text
    assert "아직 하위 문제가 없습니다" in page
    assert QUESTION in page


def test_add_problems_and_see_nested_tree(client: TestClient) -> None:
    assert _add(client, "오답 유형 분석", "개선 방향 결정").headers["location"] == "/projects/1/problems#P1"
    _add(client, "장문 지문 오답", "18개 중 7개가 장문", parent="P1")
    page = client.get("/projects/1/problems").text
    assert re.search(r'id="P1".*id="P2"', page, re.S)  # P2가 P1 안에 중첩
    assert "이유: 18개 중 7개가 장문" in page
    assert "지금 조사" in page


def test_reason_is_required(client: TestClient) -> None:
    response = _add(client, "장문 지문 오답", "", parent="")
    assert response.status_code == 422
    assert "이 문제를 푸는 이유를 한 줄" in response.text
    assert 'value="장문 지문 오답"' in response.text  # 입력 유지


def test_depth_four_problem_shows_question_path_and_return(client: TestClient) -> None:
    parent = ""
    for level in range(1, 5):
        _add(client, f"깊이 {level} 문제", f"이유 {level}", parent=parent)
        parent = f"P{level}"
    page = client.get("/projects/1/problems/P4").text
    context = page.split('class="context-bar"')[1].split("</nav>")[0]
    assert QUESTION in context  # 원래 질문
    for level in range(1, 5):
        assert f"P{level} 깊이 {level} 문제" in context  # 전체 경로
    assert 'href="/projects/1/problems/P3"' in context and "돌아갈 곳" in context  # 상위 문제로 돌아가기
    assert "현재 기준선" in context and "B0" in context
    assert '<span aria-current="page">P4 깊이 4 문제</span>' in context


def test_top_level_problem_returns_to_question(client: TestClient) -> None:
    _add(client, "오답 유형 분석", "w")
    context = client.get("/projects/1/problems/P1").text.split('class="context-bar"')[1].split("</nav>")[0]
    assert '돌아갈 곳: <a href="/projects/1/problems">연구 질문</a>' in context


def test_parked_drawer_and_resume(client: TestClient) -> None:
    _add(client, "모델 교체", "더 큰 모델이면?", parked=True)
    tree = client.get("/projects/1/problems").text
    drawer = tree.split('class="card drawer"')[1]
    assert "모델 교체" in drawer and "나중에 검토할 아이디어 1개" in drawer
    client.post("/projects/1/problems/P1/status", data={"status": "active"})
    drawer = client.get("/projects/1/problems").text.split('class="card drawer"')[1]
    assert "보류한 아이디어가 없습니다" in drawer


def test_focus_problem_appears_on_home_with_path(client: TestClient) -> None:
    _add(client, "오답 유형 분석", "개선 방향 결정")
    _add(client, "장문 지문 오답", "18개 중 7개가 장문", parent="P1")
    assert client.post("/projects/1/focus", data={"problem": "P2"}, follow_redirects=False).status_code == 303
    home = client.get("/projects/1").text
    focus = home.split("지금 집중할 문제")[1]
    assert "P1</span> 오답 유형 분석" in focus and "P2</span> 장문 지문 오답" in focus
    assert "이유: 18개 중 7개가 장문" in focus


def test_focus_on_parked_problem_is_rejected_in_korean(client: TestClient) -> None:
    _add(client, "아이디어", "w", parked=True)
    response = client.post("/projects/1/focus", data={"problem": "P1"})
    assert response.status_code == 400
    assert "상태인 문제만 집중 문제로 지정할 수 있습니다" in response.text


def test_unknown_problem_and_parent(client: TestClient) -> None:
    assert client.get("/projects/1/problems/P99").status_code == 404
    # 없는 상위 문제로는 폼을 다시 보여주지 않습니다(다시 제출하면 연구 질문 아래에 잘못 생기므로).
    response = _add(client, "t", "", parent="P99")
    assert response.status_code == 404
    assert "문제를 찾을 수 없습니다" in response.text
    assert "아직 하위 문제가 없습니다" in client.get("/projects/1/problems").text
    assert client.get("/projects/2/problems").status_code == 404


def _context_bar(html: str) -> str:
    return html.split('class="context-bar"')[1].split("</nav>")[0]


def test_new_subproblem_page_returns_to_its_parent(client: TestClient) -> None:
    _add(client, "오답 유형 분석", "w")
    _add(client, "장문 지문 오답", "w", parent="P1")
    bar = _context_bar(client.get("/projects/1/problems/new?parent=P2").text)
    assert '돌아갈 곳: <a href="/projects/1/problems/P2">상위 문제 P2 장문 지문 오답</a>' in bar
    bar = _context_bar(client.get("/projects/1/problems/new").text)
    assert '돌아갈 곳: <a href="/projects/1/problems">연구 질문</a>' in bar


def test_home_warns_when_focus_is_under_inactive_branch(client: TestClient) -> None:
    _add(client, "오답 유형 분석", "w")
    _add(client, "장문 지문 오답", "w", parent="P1")
    client.post("/projects/1/focus", data={"problem": "P2"})
    assert "상태입니다. 이 집중 문제를" not in client.get("/projects/1").text
    client.post("/projects/1/problems/P1/status", data={"status": "dropped"})
    home = client.get("/projects/1").text
    assert "오답 유형 분석이(가)" in home and "중단" in home


def test_invalid_status_is_rejected(client: TestClient) -> None:
    _add(client, "t", "w")
    response = client.post("/projects/1/problems/P1/status", data={"status": "someday"})
    assert response.status_code == 400
    assert "알 수 없는 상태" in response.text
