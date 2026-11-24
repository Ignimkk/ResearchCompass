"""8단계 화면과 명령: 인계 보기·내려받기, 결과 블록 미리보기 → 확인하고 기록, 기준선 측정 실험, rcompass handoff."""

from __future__ import annotations

from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from rcompass.cli import app as cli_app
from rcompass.config import Settings
from rcompass.web.app import create_app

BASE_URL = "http://127.0.0.1:8765"

BLOCK = """좋습니다. 결과입니다.
```rcompass-result
experiment: E2
dataset: DS1@v1.0
code: 9e8d7c6
config: configs/summarize.yaml
runs:
  - {label: seed=1, metrics: {avg_latency_s: 1.8}}
  - {label: seed=2, metrics: {avg_latency_s: 1.9}}
  - {label: seed=3, metrics: {avg_latency_s: 1.7}}
files: [outputs/latency.png]
note: 2초 안쪽
```"""


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(data_dir: Path):
    with TestClient(create_app(Settings(data_dir=data_dir)), base_url=BASE_URL) as client:
        client.post("/projects", data={
            "title": "QA", "question": "오답을 12개 이하로?", "baseline_label": "현행 프롬프트 v1",
            "criteria-0-name": "오답 수", "criteria-0-metric_key": "wrong_count", "criteria-0-direction": "lower",
            "criteria-0-target": "12", "criteria-0-min_runs": "3", "criteria-0-baseline_values": "17, 18, 19",
            "criteria-1-name": "평균 응답 시간", "criteria-1-metric_key": "avg_latency_s",
            "criteria-1-direction": "lower", "criteria-1-target": "2.0",
            "next_action": "응답 시간을 잰다",
        })
        client.post("/projects/1/problems", data={"title": "장문 지문 오답", "why": "18개 중 7개가 장문"})
        client.post("/projects/1/datasets", data={"dataset_name": "eval-100", "version_label": "v1.0",
                                                  "external_uri": "D:/data/eval100_v1.jsonl"})
        yield client


def _trial(client: TestClient) -> str:
    location = client.post("/projects/1/experiments", data={
        "problem": "P1", "title": "문단 요약", "hypothesis": "h", "decision_rule": "r"}, follow_redirects=False
    ).headers["location"]
    client.post(f"{location}/inputs", data={"dataset_version": "1", "code_ref": "a1b2c3d", "config_text": "c.yaml"})
    client.post(f"{location}/results", data={"results": "label\twrong_count\na\t11\nb\t12\nc\t10",
                                              "dataset_version": "1"})
    client.post(f"{location}/decisions", data={"type": "adopt", "reason": "목표 달성", "baseline_label": "요약 v1"})
    return location


def test_home_links_to_handoff_and_page_shows_markdown(client: TestClient) -> None:
    assert 'href="/projects/1/handoff">Claude Code 인계</a>' in client.get("/projects/1").text
    page = client.get("/projects/1/handoff").text
    assert "# 연구 인계: QA (" in page and "```rcompass-result" in page
    assert 'id="copy-handoff"' in page and "handoff.js" in page and "rcompass handoff 1" in page


def test_markdown_download(client: TestClient) -> None:
    response = client.get("/projects/1/handoff.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert 'filename="rcompass-handoff-1.md"' in response.headers["content-disposition"]
    assert response.text.startswith("# 연구 인계: QA")
    assert client.get("/projects/1/handoff.md?experiment=E9").status_code == 404
    assert client.get("/projects/9/handoff").status_code == 404


def test_measurement_experiment_flow_with_result_block(client: TestClient) -> None:
    """B1에서 재지 않은 응답 시간을 측정 실험으로 재고, 결과 블록 → 미리보기 → 확인하면 C2가 채워집니다."""
    _trial(client)
    home = client.get("/projects/1").text
    assert 'href="/projects/1/measurements/new">측정 실험 만들기</a>' in home
    form = client.get("/projects/1/measurements/new").text
    assert "C2</span> 평균 응답 시간" in form and "현재 기준선 <span class=\"id\">B1</span>" in form
    created = client.post("/projects/1/measurements", data={"title": "응답 시간 측정"}, follow_redirects=False)
    assert created.headers["location"] == "/projects/1/experiments/E2#results"
    page = client.get("/projects/1/experiments/E2").text
    assert "현재 기준선 B1의 근거에 더해져" in page and 'id="decision"' not in page
    assert 'href="/projects/1/handoff?experiment=E2"' in page

    preview = client.post("/projects/1/experiments/E2/result-block", data={"block": BLOCK})
    assert preview.status_code == 200 and "아직 기록하지 않았습니다" in preview.text
    assert "label\tavg_latency_s\nseed=1\t1.8" in preview.text and "2초 안쪽" in preview.text
    assert "outputs/latency.png" in preview.text and 'value="1" selected' in preview.text
    assert "아직 기록한 실행이 없습니다" in client.get("/projects/1/experiments/E2").text  # 미리보기는 쓰지 않음

    confirmed = client.post("/projects/1/experiments/E2/result-block/confirm", data={
        "results": "label\tavg_latency_s\nseed=1\t1.8\nseed=2\t1.9\nseed=3\t1.7", "dataset_version": "1",
        "code": "9e8d7c6", "config": "configs/summarize.yaml"}, follow_redirects=False)
    assert confirmed.status_code == 303 and "saved=claude" in confirmed.headers["location"]
    assert "snapshot=B1" in confirmed.headers["location"]
    page = client.get(confirmed.headers["location"]).text
    assert "Claude Code 결과를 확인하고 기록했습니다" in page and "R3</span> seed=3" in page
    home = client.get("/projects/1").text
    assert "1.8 ± 0.1 (n=3)" in home and "측정 실험 만들기" not in home


def test_bad_block_shows_line_error_and_keeps_text(client: TestClient) -> None:
    _trial(client)
    client.post("/projects/1/measurements", data={})
    bad = BLOCK.replace("1.9", "빠름")
    response = client.post("/projects/1/experiments/E2/result-block", data={"block": bad})
    assert response.status_code == 422
    assert "9번째 줄: avg_latency_s의 값 '빠름'은(는) 숫자가 아닙니다." in unescape(response.text)
    assert "빠름" in response.text.split('name="block"')[1].split("</textarea>")[0]


def test_confirm_errors_keep_edits(client: TestClient) -> None:
    client.post("/projects/1/measurements", data={})
    response = client.post("/projects/1/experiments/E1/result-block/confirm", data={
        "results": "label\tavg_latency_s\nseed=1\tabc", "dataset_version": "", "code": "", "config": "",
        "note": "메모", "files": ["a.png"]})
    assert response.status_code == 422 and "'abc'은(는) 숫자가 아닙니다" in unescape(response.text)
    assert "seed=1\tabc" in response.text and "a.png" in response.text and "메모" in response.text
    bad_version = client.post("/projects/1/experiments/E1/result-block/confirm", data={
        "results": "label\tavg_latency_s\na\t1", "dataset_version": "99"})
    assert bad_version.status_code == 422 and "데이터 버전을 찾을 수 없습니다" in bad_version.text


def test_block_for_other_experiment_is_rejected(client: TestClient) -> None:
    location = _trial(client)
    response = client.post(f"{location}/result-block", data={"block": BLOCK})
    assert response.status_code == 422 and "E2 화면에서 붙여넣으세요" in response.text


# ---------------------------------------------------------------- 명령줄

runner = CliRunner()


def test_cli_handoff_prints_and_saves(client: TestClient, data_dir: Path, tmp_path: Path) -> None:
    _trial(client)
    result = runner.invoke(cli_app, ["handoff", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("# 연구 인계: QA") and "B1 요약 v1" in result.output
    out = tmp_path / "handoff.md"
    result = runner.invoke(cli_app, ["handoff", "1", "-e", "e1", "-o", str(out), "--data-dir", str(data_dir)])
    assert result.exit_code == 0 and "experiment: E1" in out.read_text(encoding="utf-8")


def test_cli_handoff_errors(client: TestClient, data_dir: Path, tmp_path: Path) -> None:
    assert runner.invoke(cli_app, ["handoff", "--data-dir", str(tmp_path / "none")]).exit_code == 1
    assert runner.invoke(cli_app, ["handoff", "7", "--data-dir", str(data_dir)]).exit_code == 1
    assert runner.invoke(cli_app, ["handoff", "1", "-e", "E9", "--data-dir", str(data_dir)]).exit_code == 1
    client.post("/projects", data={"title": "둘째", "question": "q", "baseline_label": "b",
                                   "criteria-0-name": "n", "criteria-0-metric_key": "m",
                                   "criteria-0-direction": "lower", "criteria-0-target": "1"})
    result = runner.invoke(cli_app, ["handoff", "--data-dir", str(data_dir)])
    assert result.exit_code == 2 and "프로젝트 번호를 적어 주세요" in result.output


def test_handoff_experiment_param_is_case_insensitive(client: TestClient) -> None:
    client.post("/projects/1/measurements", data={})
    assert "experiment: E1" in client.get("/projects/1/handoff.md?experiment=e1").text


def test_measurement_form_unknown_problem_is_404(client: TestClient) -> None:
    assert client.get("/projects/1/measurements/new?problem=P99").status_code == 404
    created = client.post("/projects/1/measurements", data={"problem": " P1 "}, follow_redirects=False)
    assert created.status_code == 303
