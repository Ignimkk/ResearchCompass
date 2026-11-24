"""5단계 리뷰 후속: 업로드와 지문 계산의 잠금·정리·경로 해석·크기 제한."""

from __future__ import annotations

import io
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.config import Settings
from rcompass.models import FileObject, Project
from rcompass.services.files import add_file, local_path_of, store_stream
from rcompass.web.app import create_app

BASE_URL = "http://127.0.0.1:8765"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _blobs(data_dir: Path) -> list[Path]:
    store = data_dir / "files" / "sha256"
    return [p for p in store.rglob("*") if p.is_file()] if store.exists() else []


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(data_dir=tmp_path, max_attachment_bytes=64 * 1024)),
                    base_url=BASE_URL) as client:
        client.post("/projects", data={
            "title": "QA", "question": "q", "baseline_label": "B", "criteria-0-name": "오답 수",
            "criteria-0-metric_key": "wrong_count", "criteria-0-direction": "lower", "criteria-0-target": "12"})
        client.post("/projects/1/problems", data={"title": "장문", "why": "w"})
        client.post("/projects/1/experiments", data={"problem": "P1", "title": "E", "hypothesis": "h",
                                                     "decision_rule": "r"})
        client.post("/projects/1/experiments/E1/results", data={"results": "label\twrong_count\nseed=1\t11"})
        yield client


# ---------------------------------------------------------------- 기록과 정리


def test_reattaching_same_content_keeps_new_metadata(session: Session, project: Project, tmp_path: Path) -> None:
    blob = store_stream(tmp_path, io.BytesIO(b"cfg"), 1024)
    first = add_file(session, project, blob, original_name="config.yaml", caption="전체")
    second = add_file(session, project, blob, original_name="config.yaml", caption="seed=2")
    assert first.id != second.id and second.caption == "seed=2"
    assert len(_blobs(tmp_path)) == 1  # 내용은 한 번만 저장


def test_failed_dataset_save_leaves_no_orphan_blob(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/projects/1/datasets", data={"dataset_name": "small", "version_label": ""},
                           files={"attachment": ("small.csv", b"q,a\n")})
    assert response.status_code == 422
    assert _blobs(tmp_path) == []


def test_failed_upload_for_unknown_run_leaves_no_orphan_blob(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/projects/1/experiments/E1/files", data={"kind": "figure", "run": "R9"},
                           files={"upload": ("plot.png", PNG)})
    assert response.status_code == 422
    assert _blobs(tmp_path) == []


def test_upload_to_unknown_experiment_leaves_no_orphan_blob(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/projects/1/experiments/E9/files", files={"upload": ("plot.png", PNG)})
    assert response.status_code == 404
    assert _blobs(tmp_path) == []


def test_shared_blob_survives_failed_second_upload(client: TestClient, tmp_path: Path) -> None:
    client.post("/projects/1/experiments/E1/files", data={"kind": "figure"}, files={"upload": ("a.png", PNG)})
    client.post("/projects/1/experiments/E1/files", data={"kind": "figure", "run": "R9"},
                files={"upload": ("b.png", PNG)})
    assert len(_blobs(tmp_path)) == 1  # 첫 업로드가 쓰는 내용은 지우지 않음
    assert client.get("/projects/1/files/F1/content").status_code == 200


def test_upload_errors_are_combined_and_run_choice_kept(client: TestClient) -> None:
    response = client.post("/projects/1/experiments/E1/files", data={"kind": "figure", "run": "R1"})
    assert response.status_code == 422 and "첨부할 파일을 고르세요" in response.text
    assert '<option value="R1" selected>' in response.text
    both = client.post("/projects/1/experiments/E1/files", data={"kind": "figure", "run": "R9"})
    assert "첨부할 파일을 고르세요" in both.text and "실행을 찾을 수 없습니다" in both.text


# ---------------------------------------------------------------- 빠진 저장 파일


def test_file_page_survives_missing_blob(client: TestClient, tmp_path: Path) -> None:
    client.post("/projects/1/experiments/E1/files", data={"kind": "log"}, files={"upload": ("run.log", b"step 1\n")})
    for blob in _blobs(tmp_path):
        blob.unlink()
    page = client.get("/projects/1/files/F1")
    assert page.status_code == 200
    assert "저장된 파일이 없습니다" in page.text and "계보" in page.text


# ---------------------------------------------------------------- 크기 제한 (길이 없는 요청)


def test_chunked_upload_over_limit_is_cut_off(client: TestClient, tmp_path: Path) -> None:
    boundary = "rcompassboundary"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"upload\"; filename=\"big.bin\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def body():  # Content-Length 없이(청크) 보내기
        yield head
        for _ in range(40):
            yield b"x" * (64 * 1024)
        yield tail

    response = client.post("/projects/1/experiments/E1/files", content=body(),
                           headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    assert response.status_code == 413
    assert "요청이 너무 큽니다" in response.text and "<html" in response.text.lower()
    assert _blobs(tmp_path) == []


def test_413_is_korean_error_page(client: TestClient) -> None:
    response = client.post("/projects/1/experiments/E1/files", files={"upload": ("huge.bin", b"x" * (2 * 1024 * 1024))})
    assert response.status_code == 413
    assert "요청이 너무 큽니다" in response.text and "연구 목록으로" in response.text


# ---------------------------------------------------------------- 지문 계산: 경로 해석과 잠금


@pytest.mark.parametrize(
    "uri",
    ["https://example.org/a.jsonl", "gs://bucket/a.jsonl", "hf://datasets/x", "sftp://nas/a", "relative/a.jsonl",
     "./a.jsonl", "a.jsonl"],
)
def test_non_local_or_relative_locations_are_not_hashed(uri: str) -> None:
    assert local_path_of(uri) is None


def test_absolute_and_file_uris_are_local(tmp_path: Path) -> None:
    assert local_path_of(str(tmp_path / "a.jsonl")) == tmp_path / "a.jsonl"
    assert local_path_of((tmp_path / "a.jsonl").as_uri()) == tmp_path / "a.jsonl"


@pytest.mark.skipif(os.name != "nt", reason="UNC 경로는 Windows 전용")
def test_unc_file_uri_keeps_host() -> None:
    assert str(local_path_of("file://nas01/datasets/eval.jsonl")) == r"\\nas01\datasets\eval.jsonl"


def test_hashing_does_not_hold_write_lock(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """지문 계산 중에도 다른 저장 요청이 막히지 않아야 합니다."""
    from rcompass.services import datasets as datasets_module

    data = tmp_path / "eval.jsonl"
    data.write_bytes(b"line\n")
    client.post("/projects/1/datasets", data={"dataset_name": "eval", "version_label": "v1",
                                              "external_uri": str(data)})
    hashing = threading.Event()
    release = threading.Event()
    original = datasets_module.fingerprint_file

    def slow_fingerprint(path):
        hashing.set()
        assert release.wait(10)
        return original(path)

    monkeypatch.setattr(datasets_module, "fingerprint_file", slow_fingerprint)
    results: dict[str, int] = {}
    worker = threading.Thread(target=lambda: results.update(
        verify=client.post("/projects/1/datasets/1/verify", follow_redirects=False).status_code))
    worker.start()
    assert hashing.wait(10)
    # 해시 계산이 진행되는 동안 다른 쓰기 요청이 바로 처리돼야 합니다.
    write = client.post("/projects/1/problems", data={"title": "동시에 추가", "why": "잠금 확인"},
                        follow_redirects=False)
    release.set()
    worker.join(10)
    assert write.status_code == 303 and results["verify"] == 303


def test_concurrent_records_reference_same_blob(session: Session, project: Project, tmp_path: Path) -> None:
    blob = store_stream(tmp_path, io.BytesIO(PNG), 1024)
    add_file(session, project, blob, original_name="a.png")
    add_file(session, project, blob, original_name="b.png")
    session.commit()
    rows = session.scalars(select(FileObject.rel_path)).all()
    assert len(set(rows)) == 1
