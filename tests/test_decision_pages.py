"""6단계 화면: 판정 패널, 채택·기각·보류·철회, 재현성 관문, 다음 시도, Git과 파일 미변경."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcompass.config import Settings
from rcompass.web.app import create_app

BASE_URL = "http://127.0.0.1:8765"


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(data_dir=tmp_path / "data")), base_url=BASE_URL) as client:
        client.post("/projects", data={
            "title": "QA", "question": "오답을 12개 이하로?", "baseline_label": "현행 프롬프트 v1",
            "criteria-0-name": "오답 수", "criteria-0-metric_key": "wrong_count", "criteria-0-direction": "lower",
            "criteria-0-target": "12", "criteria-0-min_runs": "3", "criteria-0-baseline_values": "17, 18, 19",
        })
        client.post("/projects/1/problems", data={"title": "장문 지문 오답", "why": "18개 중 7개가 장문"})
        client.post("/projects/1/datasets", data={"dataset_name": "eval-100", "version_label": "v1.0",
                                                  "external_uri": "D:/data/eval100_v1.jsonl"})
        yield client


def _trial(client: TestClient, title: str, values: list[int], *, inputs: bool = True,
           code_ref: str = "3f2a9e1", config: str = "configs/x.yaml") -> str:
    location = client.post("/projects/1/experiments", data={
        "problem": "P1", "title": title, "hypothesis": "h", "decision_rule": "평균 ≤ 17이면 채택, ≥ 18이면 기각",
    }, follow_redirects=False).headers["location"]
    if inputs:
        client.post(f"{location}/inputs", data={"dataset_version": "1", "code_ref": code_ref, "config_text": config})
    rows = "\n".join(f"seed={i}\t{v}" for i, v in enumerate(values, start=1))
    client.post(f"{location}/results", data={"results": f"label\twrong_count\n{rows}", "dataset_version": "1"})
    return location


def _decide(client: TestClient, location: str, kind: str, **data):
    return client.post(f"{location}/decisions", data={"type": kind, **data}, follow_redirects=False)


def test_panel_shows_comparison_overlap_and_dots(client: TestClient) -> None:
    """시나리오 3: 18.0 → 19.0 (+1.0)과 '범위가 겹침' 경고가 함께 보입니다."""
    page = client.get(_trial(client, "few-shot", [18, 19, 20])).text
    panel = page.split('id="decision"')[1]
    assert "평균 ≤ 17이면 채택" in panel
    assert "<strong>18.0 ± 1.0 (n=3)</strong> → 이 실험 <strong>19.0 ± 1.0 (n=3)</strong>" in panel
    assert "+1.0 (나빠짐)" in panel and "차이가 실행 간 변동 안에 있습니다" in panel
    assert panel.count('class="dot dot-base"') == 3 and panel.count('class="dot dot-trial"') == 3
    assert "<title>E1/R2 seed=2: 19</title>" in panel and "목표 ≤ 12" in panel
    assert 'role="img" aria-label="오답 수(wrong_count) 실행별 값. 기준선 B0: 17, 18, 19.' in panel


def test_reject_flow_keeps_baseline_and_offers_next_try(client: TestClient) -> None:
    location = _trial(client, "few-shot", [18, 19, 20])
    response = _decide(client, location, "reject", reason="평균 오답 +1, 형식 오류 2건 증가")
    assert response.status_code == 303 and response.headers["location"].endswith("?saved=decision#decision")
    page = client.get(location).text
    assert "✘ 기각" in page and "D1</span> <strong>기각</strong>" in page
    assert "평균 오답 +1, 형식 오류 2건 증가" in page
    assert 'href="/projects/1/problems/P1/experiments/new">P1 장문 지문 오답에서 다음 시도' in page
    assert "기준선 B0에서 다른 문제 고르기" in page
    assert 'value="adopt"' not in page  # 기각한 실험은 다시 판정하지 않음
    new_form = client.get("/projects/1/problems/P1/experiments/new").text
    assert '출발 기준선은 현재 기준선 <span class="id">B0</span>' in new_form
    home = client.get("/projects/1").text
    assert "미달" in home and "18.0 ± 1.0 (n=3)" in home


def test_gate_replaces_adopt_button_until_filled(client: TestClient) -> None:
    """시나리오 4: 실행 2개와 데이터 버전 없이는 '빠진 항목 채우기'가 채택 버튼 대신 나옵니다."""
    location = _trial(client, "문단 요약", [11, 12], inputs=False)
    page = client.get(location).text
    assert "빠진 항목 채우기" in page and 'value="adopt"' not in page
    assert '<a href="#results">C1 오답 수: 실행 2/3</a>' in page and '<a href="#inputs">입력 데이터 버전</a>' in page
    assert 'id="inputs"' in page and 'id="results"' in page
    blocked = _decide(client, location, "adopt", reason="목표 달성")
    assert blocked.status_code == 422 and "채택하려면 먼저 채우세요" in blocked.text

    client.post(f"{location}/results", data={"results": "label\twrong_count\nseed=3\t10", "dataset_version": "1"})
    client.post(f"{location}/inputs", data={"dataset_version": "1", "code_ref": "a1b2c3d", "config_text": "c.yaml"})
    page = client.get(location).text
    assert 'value="adopt"' in page and "빠진 항목 채우기" not in page


def test_adopt_then_revoke_round_trip(client: TestClient) -> None:
    """시나리오 4~5: 채택으로 B1, 철회로 B0 복귀. 두 결정이 모두 보이고 B1 기록은 남습니다."""
    location = _trial(client, "문단 요약 후 답변", [11, 12, 10])
    _decide(client, location, "adopt", reason="목표 달성, 다른 유형 악화 없음", baseline_label="문단 요약 v1")
    home = client.get("/projects/1").text
    assert "B1" in home and "달성" in home and "11.0 ± 1.0 (n=3)" in home
    page = client.get(location).text
    assert "✔ 채택" in page and "채택 철회" in page and 'value="B0" selected' in page
    assert "새 기준선 <span class=\"id\">B1</span> \"문단 요약 v1\"" in page

    response = client.post(f"{location}/revoke", data={"reason": "평가셋 일부가 예시에 섞임", "target": "B0"},
                           follow_redirects=False)
    assert response.status_code == 303
    page = client.get(location + "?saved=revoke").text
    assert "현재 기준선은 B0 현행 프롬프트 v1입니다" in page
    assert "철회됨 (D2)" in page and "현재 기준선 B1 → B0" in page
    assert page.index("<strong>채택</strong>") < page.index("<strong>채택 철회</strong>")
    assert 'action="/projects/1/experiments/E1/revoke"' not in page
    home = client.get("/projects/1").text
    assert "미달" in home and "18.0 ± 1.0 (n=3)" in home


def test_revoke_errors_keep_input(client: TestClient) -> None:
    location = _trial(client, "a", [11, 12, 10])
    _decide(client, location, "adopt", reason="ok")
    response = client.post(f"{location}/revoke", data={"reason": "", "target": "B0"})
    assert response.status_code == 422 and "철회하는 이유를 적어 주세요" in response.text
    response = client.post(f"{location}/revoke", data={"reason": "x", "target": "B9"})
    assert response.status_code == 422 and "돌아갈 수 없습니다" in response.text and 'value="x"' in response.text


def test_hold_with_revisit_condition(client: TestClient) -> None:
    location = _trial(client, "a", [15, 16, 17])
    _decide(client, location, "hold", reason="재측정 필요", revisit_condition="평가셋 v2가 나오면")
    page = client.get(location).text
    assert "⏸ 보류" in page and "재개 조건: 평가셋 v2가 나오면" in page
    assert 'value="adopt"' in page and 'value="hold"' not in page  # 다시 판정 가능, 보류는 한 번


def test_bad_decision_type_and_missing_reason(client: TestClient) -> None:
    location = _trial(client, "a", [18, 19, 20])
    assert _decide(client, location, "revoke", reason="x").status_code == 400
    assert _decide(client, location, "delete", reason="x").status_code == 400
    response = _decide(client, location, "reject", reason=" ")
    assert response.status_code == 422 and "이유를 한 줄 적어 주세요" in response.text


def test_baseline_measurement_has_no_panel(client: TestClient) -> None:
    page = client.get("/projects/1/experiments/E0").text
    assert 'id="decision"' not in page
    assert _decide(client, "/projects/1/experiments/E0", "adopt", reason="x").status_code == 422


def test_excluding_snapshot_run_after_adoption_warns(client: TestClient) -> None:
    location = _trial(client, "a", [11, 12, 10])
    _decide(client, location, "adopt", reason="ok")
    client.post(f"{location}/runs/R2/exclude", data={"reason": "로그 손상"})
    page = client.get(location).text
    assert "기준선 B1의 근거 실행 E1/R2(seed=2)이(가) 제외되었습니다(이유: 로그 손상)" in page
    assert "11.0 ± 1.0 (n=3)" in client.get("/projects/1").text


def test_file_lineage_shows_decisions_and_baseline(client: TestClient) -> None:
    location = _trial(client, "a", [11, 12, 10])
    client.post(f"{location}/files", files={"upload": ("plot.png", b"\x89PNG\r\n", "image/png")},
                data={"kind": "figure", "run": "R1"})
    _decide(client, location, "adopt", reason="목표 달성", baseline_label="문단 요약 v1")
    page = client.get("/projects/1/files/F1").text
    assert "채택" in page and "문단 요약 v1" in page


@pytest.mark.skipif(shutil.which("git") is None, reason="git이 없습니다")
def test_decisions_never_touch_research_repo(client: TestClient, tmp_path: Path) -> None:
    """채택과 철회는 앱 기록만 바꿉니다. 연구 저장소의 HEAD, 작업 트리, 파일 내용이 그대로여야 합니다."""
    repo = tmp_path / "research"
    repo.mkdir()
    (repo / "configs").mkdir()
    (repo / "configs" / "summarize.yaml").write_text("model: x\n", encoding="utf-8")
    (repo / "eval.jsonl").write_text('{"q": 1}\n', encoding="utf-8")

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                              env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
                                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
                                   "PATH": __import__("os").environ["PATH"]}).stdout

    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    head = git("rev-parse", "HEAD").strip()
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}

    client.post("/projects/1/datasets", data={"dataset_name": "eval", "version_label": "v1",
                                              "external_uri": str(repo / "eval.jsonl")})
    client.post("/projects/1/datasets/2/verify")
    location = _trial(client, "a", [11, 12, 10], code_ref=head,
                      config=str(repo / "configs" / "summarize.yaml"))
    assert _decide(client, location, "adopt", reason="ok").status_code == 303
    assert client.post(f"{location}/revoke", data={"reason": "x", "target": "B0"},
                       follow_redirects=False).status_code == 303

    assert git("rev-parse", "HEAD").strip() == head
    assert git("status", "--porcelain") == ""
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts} == hashes


def test_enter_key_cannot_submit_a_decision(client: TestClient) -> None:
    """Enter를 누르면 브라우저는 폼의 첫 제출 버튼을 씁니다. 첫 버튼은 비활성이어야 합니다."""
    page = client.get(_trial(client, "a", [11, 12, 10])).text
    form = page.split('action="/projects/1/experiments/E1/decisions"')[1].split("</form>")[0]
    first_button = form.split("<button")[1].split(">")[0]
    assert "disabled" in first_button and 'name="type"' not in first_button
    assert form.index('value="adopt"') > form.index("enter-guard")


def test_locked_forms_are_not_offered(client: TestClient) -> None:
    rejected = _trial(client, "a", [18, 19, 20])
    _decide(client, rejected, "reject", reason="x")
    page = client.get(rejected).text
    assert 'name="hypothesis" rows' not in page and "가설과 판정 기준은 고칠 수 없습니다" in page
    assert 'action="/projects/1/experiments/E1/inputs"' in page  # 기각한 실험은 입력을 나중에 채울 수 있음
    renamed = client.post(f"{rejected}/plan", data={"title": "새 제목", "hypothesis": "h",
                                                    "decision_rule": "평균 ≤ 17이면 채택, ≥ 18이면 기각"},
                          follow_redirects=False)
    assert renamed.status_code == 303 and "새 제목" in client.get(rejected).text

    adopted = _trial(client, "b", [11, 12, 10])
    _decide(client, adopted, "adopt", reason="ok")
    page = client.get(adopted).text
    assert 'action="/projects/1/experiments/E2/inputs"' not in page
    assert "기준선의 재현 기록이라 고칠 수 없습니다" in page and "3f2a9e1" in page


def test_snapshot_warning_disappears_after_revoke(client: TestClient) -> None:
    location = _trial(client, "a", [11, 12, 10])
    _decide(client, location, "adopt", reason="ok")
    client.post(f"{location}/runs/R2/exclude", data={"reason": "로그 손상"})
    client.post(f"{location}/revoke", data={"reason": "누수", "target": "B0"})
    page = client.get(location).text
    assert "↺ 철회" in page and "chip-outcome-revoked" in page
    assert "철회하거나 다시 판정할지 검토하세요" not in page
