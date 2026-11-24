"""5단계 화면: 결과 파일 첨부 → 자료 열람 → 그래프에서 원본 실험까지 역추적. 업로드 보안."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcompass.config import Settings
from rcompass.web.app import create_app

BASE_URL = "http://127.0.0.1:8765"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _seed(client: TestClient) -> None:
    client.post("/projects", data={
        "title": "QA", "question": "동일 평가 100문항에서 오답을 18→12개 이하로 줄일 수 있는가?",
        "baseline_label": "현행 프롬프트 v1", "criteria-0-name": "오답 수", "criteria-0-metric_key": "wrong_count",
        "criteria-0-direction": "lower", "criteria-0-target": "12", "criteria-0-min_runs": "3",
        "criteria-0-baseline_values": "17, 18, 19"})
    client.post("/projects/1/problems", data={"title": "장문 지문 오답", "why": "오답 18 중 7이 장문"})
    client.post("/projects/1/experiments", data={
        "problem": "P1", "title": "문단 요약 후 답변", "hypothesis": "지문을 문단별 요약 후 답하면 장문 오답이 줄어든다",
        "decision_rule": "평균 오답 ≤ 17이고 다른 유형 악화 없음 → 채택"})
    client.post("/projects/1/datasets", data={
        "dataset_name": "eval-100", "version_label": "v1.0", "external_uri": "D:/data/eval100_v1.jsonl",
        "source": "내부 QA 수집", "collected_at": "2026-11-02"})
    client.post("/projects/1/experiments/E1/inputs",
                data={"dataset_version": "1", "code_ref": "a1b2c3d", "config_text": "configs/summarize.yaml"})
    client.post("/projects/1/experiments/E1/results",
                data={"results": "label\twrong_count\nseed=1\t11\nseed=2\t12\nseed=3\t10", "dataset_version": "1"})


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(data_dir=tmp_path, max_attachment_bytes=64 * 1024)),
                    base_url=BASE_URL) as client:
        _seed(client)
        yield client


def _upload(client: TestClient, name: str, content: bytes, **data):
    return client.post("/projects/1/experiments/E1/files", files={"upload": (name, content)},
                       data={"kind": "figure", **data}, follow_redirects=False)


def test_upload_figure_and_trace_back_to_experiment(client: TestClient) -> None:
    response = _upload(client, "오답유형_E1.png", PNG, run="R1", caption="오답 유형별 개수")
    assert response.status_code == 303 and response.headers["location"].endswith("saved=file#files")
    assert "F1</span> 오답유형_E1.png" in client.get("/projects/1/experiments/E1").text

    page = client.get("/projects/1/files/F1").text
    assert '<img src="/projects/1/files/F1/content"' in page
    chain = page.split('class="chain"')[1]
    for expected in ["생성 실행", "E1/R1", "seed=1", "문단 요약 후 답변", "P1 장문 지문 오답", "오답 18 중 7이 장문",
                     "가설: 지문을 문단별 요약", "판정 기준: 평균 오답 ≤ 17", "eval-100 v1.0", "내부 QA 수집",
                     "2026-11-02", "D:/data/eval100_v1.jsonl", "a1b2c3d", "configs/summarize.yaml",
                     "wrong_count = 11.0 ± 1.0 (n=3)"]:
        assert expected in chain, expected
    bar = page.split('class="context-bar"')[1].split("</nav>")[0]
    assert "P1 장문 지문 오답" in bar  # 맥락 바에도 원래 문제


def test_materials_page_lists_files_and_dataset_usage(client: TestClient) -> None:
    _upload(client, "plot.png", PNG)
    page = client.get("/projects/1/datasets").text
    assert "첨부 파일 1개" in page and "plot.png" in page
    assert 'href="/projects/1/experiments/E1">E1</a>' in page  # 이 데이터 버전을 쓴 실험


@pytest.mark.parametrize(
    ("name", "content", "media", "disposition"),
    [
        ("plot.png", PNG, "image/png", "inline"),
        ("wrong.csv", "유형,개수\n장문,7\n".encode(), "text/plain; charset=utf-8", "inline"),
        ("diagram.svg", b"<svg onload='alert(1)'></svg>", "application/octet-stream", "attachment"),
        ("page.html", b"<script>alert(1)</script>", "application/octet-stream", "attachment"),
    ],
)
def test_content_is_served_safely(client: TestClient, name: str, content: bytes, media: str, disposition: str) -> None:
    _upload(client, name, content)
    response = client.get("/projects/1/files/F1/content")
    assert response.status_code == 200 and response.content == content
    assert response.headers["content-type"] == media
    assert response.headers["content-disposition"].startswith(disposition)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


def test_text_preview_is_escaped(client: TestClient) -> None:
    _upload(client, "notes.txt", b"<script>alert(1)</script>\nline2")
    page = client.get("/projects/1/files/F1").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert(1)" not in page


def test_upload_over_limit_is_rejected(client: TestClient, tmp_path: Path) -> None:
    response = _upload(client, "big.png", b"x" * (64 * 1024 + 1))
    assert response.status_code == 422 and "MB보다 큽니다" in response.text
    huge = _upload(client, "huge.bin", b"x" * (2 * 1024 * 1024))
    assert huge.status_code == 413 and "요청이 너무 큽니다" in huge.text
    store = tmp_path / "files" / "sha256"
    assert not store.exists() or not [p for p in store.rglob("*") if p.is_file()]  # 아무것도 저장되지 않음
    assert not [p for p in (tmp_path / "files" / "tmp").iterdir()]  # 임시 파일도 남지 않음


def test_upload_requires_file_and_valid_run(client: TestClient) -> None:
    assert client.post("/projects/1/experiments/E1/files", data={"kind": "figure"}).status_code == 422
    response = _upload(client, "plot.png", PNG, run="R9")
    assert response.status_code == 422 and "실행을 찾을 수 없습니다" in response.text


def test_cross_site_upload_is_blocked(client: TestClient) -> None:
    response = client.post("/projects/1/experiments/E1/files", files={"upload": ("x.png", PNG)},
                           headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert client.get("/projects/1/files/F1").status_code == 404


def test_path_like_filename_is_neutralised(client: TestClient, tmp_path: Path) -> None:
    _upload(client, "../../../rcompass.db", b"not a db")
    page = client.get("/projects/1/datasets").text
    assert "F1</span> rcompass.db" in page
    assert (tmp_path / "rcompass.db").read_bytes() != b"not a db"


def test_attached_dataset_version_via_form(client: TestClient) -> None:
    response = client.post("/projects/1/datasets", data={"dataset_name": "small", "version_label": "v1"},
                           files={"attachment": ("small.csv", b"q,a\n1,2\n")}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/projects/1/datasets").text
    assert "small <strong>v1</strong>" in page
    assert "첨부 파일 <span class=\"muted\">· 백업에 포함" in page
    both = client.post("/projects/1/datasets", data={"dataset_name": "x", "version_label": "v1", "external_uri": "p"},
                       files={"attachment": ("x.csv", b"1")})
    assert both.status_code == 422 and "하나만 고르세요" in both.text


def test_fingerprint_button(client: TestClient, tmp_path: Path) -> None:
    data = tmp_path / "local.jsonl"
    data.write_bytes(b"line\n")
    client.post("/projects/1/datasets", data={"dataset_name": "local", "version_label": "v1",
                                              "external_uri": str(data)})
    first = client.post("/projects/1/datasets/2/verify", follow_redirects=True).text
    assert "지문(sha256)을 계산해 기록했습니다" in first
    data.write_bytes(b"changed\n")
    second = client.post("/projects/1/datasets/2/verify", follow_redirects=True).text
    assert "기록한 지문과 다릅니다" in second
    url_result = client.post("/projects/1/datasets/1/verify", follow_redirects=True).text
    assert "이 위치에서 파일을 찾을 수 없습니다" in url_result  # D:/data/... 는 이 PC에 없음


def test_unknown_or_foreign_files_are_404(client: TestClient) -> None:
    assert client.get("/projects/1/files/F9").status_code == 404
    assert client.get("/projects/1/files/F9/content").status_code == 404
    assert client.post("/projects/1/datasets/99/verify").status_code == 404
