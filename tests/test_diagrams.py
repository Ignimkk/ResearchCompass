"""10단계: 구조도 등록, 근거 배지, 오래됨 배지, sandbox 열람, 인계 연결."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rcompass.config import Settings
from rcompass.db import open_database, session_factory
from rcompass.example import seed_example
from rcompass.models import Diagram, DiagramBasis, DiagramKind, FileKind, FileObject, Project
from rcompass.services import ValidationError
from rcompass.services.diagrams import (
    freshness,
    normalize_paths,
    read_ir_hints,
    register_diagram,
    source_link,
)
from rcompass.services.files import store_stream
from rcompass.services.handoff import HandoffOptions, build_handoff
from rcompass.web.app import create_app
from rcompass.web.routes.diagrams import DIAGRAM_HEADERS

BASE_URL = "http://127.0.0.1:8765"
SHA = "a1b2c3d" + "4" * 33
OTHER_SHA = "9" * 40
HTML = b"<!doctype html><title>t</title><script>document.title='x'</script><p>diagram</p>"
IR = {
    "schema_version": "1",
    "diagram_type": "architecture",
    "meta": {"title": "qa-bot", "repository": {"url": "https://github.com/me/qa-bot.git", "revision": SHA.upper()}},
    "components": [
        {"id": "a", "label": "A", "sources": [{"path": "src/app.py", "line": 3}, {"path": "./src/pipe.py"}]},
        {"id": "b", "label": "B", "sources": [{"path": "src/app.py"}]},
    ],
}


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    engine = open_database(data / "rcompass.db")
    with session_factory(engine)() as session:
        seed_example(session, data, max_bytes=50 * 1024 * 1024)
        session.commit()
    engine.dispose()
    return data


@pytest.fixture
def db(data_dir: Path):
    engine = open_database(data_dir / "rcompass.db")
    with session_factory(engine)() as session:
        yield session
    engine.dispose()


def _blob(data_dir: Path, content: bytes):
    import io

    return store_stream(data_dir, io.BytesIO(content), 10 * 1024 * 1024)


def _register(db, data_dir: Path, **overrides) -> Diagram:
    project = db.scalars(select(Project)).one()
    kwargs = dict(title="qa-bot 구조", kind=DiagramKind.RESEARCH_REPO, basis=DiagramBasis.CODE_BACKED,
                  html_blob=_blob(data_dir, HTML), html_name="qa-bot.html", commit_sha=SHA,
                  related_paths="src/app.py", repo_url="https://github.com/me/qa-bot")
    kwargs.update(overrides)
    diagram = register_diagram(db, project, **kwargs)
    db.commit()
    return diagram


# ---------------------------------------------------------------- 서비스


def test_code_backed_requires_full_sha_and_paths(db, data_dir: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        _register(db, data_dir, commit_sha="", related_paths="")
    assert set(caught.value.errors) == {"commit_sha", "related_paths"}
    db.rollback()
    with pytest.raises(ValidationError) as caught:
        _register(db, data_dir, commit_sha="a1b2c3d")
    assert "40자" in caught.value.errors["commit_sha"]
    db.rollback()
    planned = _register(db, data_dir, basis=DiagramBasis.PLANNED, commit_sha="", related_paths="",
                        kind=DiagramKind.APP_CODE)
    assert planned.commit_sha is None and planned.short_id == "G1"


@pytest.mark.parametrize("bad", ["/etc/passwd", "C:/x.py", "src/../../x", "a\x00b"])
def test_related_paths_must_be_repo_relative(bad: str) -> None:
    paths, error = normalize_paths(f"src/ok.py\n{bad}")
    assert error and paths == []
    assert normalize_paths(" ./src\\a.py \n\nsrc/a.py\n") == (["src/a.py"], None)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///C:/x", "ftp://h/x", "https://"])
def test_repo_url_must_be_http(db, data_dir: Path, url: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _register(db, data_dir, repo_url=url)
    assert "repo_url" in caught.value.errors


def test_html_and_json_suffixes_are_checked(db, data_dir: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        _register(db, data_dir, html_name="diagram.svg", ir_blob=_blob(data_dir, b"{}"), ir_name="ir.txt")
    assert set(caught.value.errors) == {"html", "json_ir"}


def test_ir_hints_fill_blank_fields(db, data_dir: Path) -> None:
    ir_blob = _blob(data_dir, json.dumps(IR).encode())
    hints = read_ir_hints(data_dir / ir_blob.rel_path)
    assert hints.repo_url == "https://github.com/me/qa-bot"
    assert hints.commit_sha == SHA
    assert hints.paths == ["src/app.py", "src/pipe.py"]
    diagram = _register(db, data_dir, repo_url="", commit_sha="", related_paths="", ir_blob=ir_blob,
                        ir_name="qa-bot.json", ir_hints=hints)
    assert diagram.repo_url == "https://github.com/me/qa-bot"
    assert diagram.commit_sha == SHA
    assert diagram.related_paths == "src/app.py\nsrc/pipe.py"
    ir_file = db.get(FileObject, diagram.json_ir_file_id)
    assert ir_file.kind is FileKind.DIAGRAM and ir_file.original_name == "qa-bot.json"
    assert source_link(diagram, "src/app.py") == f"https://github.com/me/qa-bot/blob/{SHA}/src/app.py"


def test_bad_ir_is_reported(data_dir: Path) -> None:
    blob = _blob(data_dir, b"{not json")
    with pytest.raises(ValidationError) as caught:
        read_ir_hints(data_dir / blob.rel_path)
    assert "json_ir" in caught.value.errors


def test_freshness_against_current_baseline(db, data_dir: Path) -> None:
    project = db.scalars(select(Project)).one()
    same = _register(db, data_dir)  # 예제의 B1은 E2(code a1b2c3d)에서 채택
    assert freshness(db, project, same).stale is False

    other = _register(db, data_dir, commit_sha=OTHER_SHA)
    result = freshness(db, project, other)
    assert result.stale and "다른 커밋" in result.message

    other.created_at = datetime(2000, 1, 1)  # 기준선이 정해지기 전에 만든 구조도
    db.commit()
    result = freshness(db, project, other)
    assert result.stale and "오래됨" in result.message

    flow = _register(db, data_dir, kind=DiagramKind.RESEARCH_FLOW, basis=DiagramBasis.PLANNED,
                     commit_sha="", related_paths="")
    assert freshness(db, project, flow) is None
    flow.created_at = datetime(2000, 1, 1)
    db.commit()
    assert "오래됨" in freshness(db, project, flow).message

    app_code = _register(db, data_dir, kind=DiagramKind.APP_CODE, commit_sha=OTHER_SHA)
    assert freshness(db, project, app_code) is None


def test_handoff_lists_latest_diagram_per_kind(db, data_dir: Path) -> None:
    project = db.scalars(select(Project)).one()
    old = _register(db, data_dir, title="옛 구조", commit_sha=OTHER_SHA)
    old.created_at -= timedelta(days=1)
    _register(db, data_dir, title="qa-bot 구조")
    text = build_handoff(db, project, HandoffOptions(today=date(2026, 11, 24)))
    assert "- 연구 저장소 코드·데이터 흐름도: qa-bot 구조 (코드 근거, a1b2c3d) — G2 qa-bot.html" in text
    assert "옛 구조" not in text


# ---------------------------------------------------------------- 화면


def _client(data_dir: Path) -> TestClient:
    return TestClient(create_app(Settings(data_dir=data_dir)), base_url=BASE_URL)


def _form(**overrides) -> dict:
    form = {"title": "qa-bot 구조", "kind": "research_repo", "basis": "code_backed",
            "repo_url": "https://github.com/me/qa-bot", "commit_sha": SHA, "related_paths": "src/app.py",
            "explanation": "요청은 A에서 B로 갑니다.\n둘째 줄", "experiment": "E2", "baseline": "B1"}
    form.update(overrides)
    return form


def test_register_and_view_in_sandbox(data_dir: Path) -> None:
    with _client(data_dir) as client:
        response = client.post("/projects/1/diagrams", data=_form(),
                               files={"html": ("qa-bot.html", HTML, "text/html")}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/projects/1/diagrams/G1"

        page = client.get("/projects/1/diagrams/G1").text
        assert 'sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads"' in page
        assert "allow-same-origin" not in page
        assert "코드 근거" in page and "요청은 A에서 B로 갑니다." in page
        assert f"https://github.com/me/qa-bot/blob/{SHA}/src/app.py" in page
        assert "현재 기준선 B1의 코드 a1b2c3d와 같은 커밋입니다." in page

        view = client.get("/projects/1/diagrams/G1/view")
        assert view.status_code == 200 and view.content == HTML
        assert view.headers["content-type"].startswith("text/html")
        csp = view.headers["content-security-policy"]
        assert csp == DIAGRAM_HEADERS["Content-Security-Policy"]
        assert csp.startswith("sandbox allow-scripts ") and "allow-same-origin" not in csp
        assert "connect-src 'none'" in csp and "frame-ancestors 'self'" in csp
        assert view.headers["x-content-type-options"] == "nosniff"

        listing = client.get("/projects/1/diagrams").text
        assert "G1" in listing and "qa-bot 구조" in listing

        # 파일 화면의 원본 내려받기는 여전히 첨부(attachment)로만 나갑니다.
        content = client.get("/projects/1/files/F5/content")
        assert content.headers["content-disposition"].startswith("attachment")


def test_register_errors_keep_values_and_discard_blobs(data_dir: Path) -> None:
    blobs_before = sorted(p for p in (data_dir / "files" / "sha256").rglob("*") if p.is_file())
    with _client(data_dir) as client:
        response = client.post("/projects/1/diagrams", data=_form(commit_sha="abc", title=""),
                               files={"html": ("new.html", b"<p>unique content</p>", "text/html")})
    assert response.status_code == 422
    assert "커밋은 전체 SHA" in response.text and "구조도 제목을 입력하세요" in response.text
    assert "src/app.py" in response.text  # 입력값 유지
    assert sorted(p for p in (data_dir / "files" / "sha256").rglob("*") if p.is_file()) == blobs_before


def test_register_with_ir_only_fills_code_basis(data_dir: Path) -> None:
    with _client(data_dir) as client:
        response = client.post(
            "/projects/1/diagrams", data=_form(repo_url="", commit_sha="", related_paths=""),
            files={"html": ("qa-bot.html", HTML, "text/html"),
                   "json_ir": ("qa-bot.json", json.dumps(IR).encode(), "application/json")},
            follow_redirects=False)
        assert response.status_code == 303
        page = client.get("/projects/1/diagrams/G1").text
        assert SHA in page and "src/pipe.py" in page


def test_unknown_diagram_and_missing_file(data_dir: Path) -> None:
    with _client(data_dir) as client:
        assert client.get("/projects/1/diagrams/G9").status_code == 404
        assert client.get("/projects/1/diagrams/G9/view").status_code == 404
        client.post("/projects/1/diagrams", data=_form(), files={"html": ("a.html", HTML, "text/html")})
        for blob in (data_dir / "files" / "sha256").rglob("*"):
            if blob.is_file() and blob.read_bytes() == HTML:
                blob.unlink()
        assert client.get("/projects/1/diagrams/G1/view").status_code == 404
        assert "저장된 구조도 파일이 없습니다" in client.get("/projects/1/diagrams/G1").text


def test_cross_origin_register_is_rejected(data_dir: Path) -> None:
    with _client(data_dir) as client:
        for origin in ("null", "http://evil.example"):
            response = client.post("/projects/1/diagrams", data=_form(), headers={"Origin": origin},
                                   files={"html": ("a.html", HTML, "text/html")})
            assert response.status_code == 403


# ---------------------------------------------------------------- 코드 리뷰 회귀


def test_ir_hints_skip_bad_values_instead_of_failing(db, data_dir: Path) -> None:
    ir = {"meta": {"repository": {"url": "git@github.com:me/qa-bot.git", "revision": "HEAD"}},
          "components": [{"sources": [{"path": "/abs/x.py"}, {"path": "../up.py"}]}]
          + [{"sources": [{"path": f"src/m{i}.py"}]} for i in range(60)]}
    blob = _blob(data_dir, json.dumps(ir).encode())
    hints = read_ir_hints(data_dir / blob.rel_path)
    assert hints.repo_url == "https://github.com/me/qa-bot"  # SSH 원격을 웹 주소로
    assert hints.commit_sha is None  # 전체 SHA가 아닌 revision은 버림
    assert hints.paths[:2] == ["src/m0.py", "src/m1.py"] and len(hints.paths) == 50
    planned = _register(db, data_dir, basis=DiagramBasis.PLANNED, repo_url="", commit_sha="", related_paths="",
                        ir_blob=blob, ir_name="ir.json", ir_hints=hints)
    assert planned.repo_url == "https://github.com/me/qa-bot" and planned.commit_sha is None


def test_numeric_version_memo_is_not_a_commit(db, data_dir: Path) -> None:
    from rcompass.models import Baseline, Experiment

    project = db.scalars(select(Project)).one()
    baseline = db.get(Baseline, project.current_baseline_id)
    db.get(Experiment, baseline.source_experiment_id).code_ref = "20261124"
    db.commit()
    assert freshness(db, project, _register(db, data_dir, commit_sha=OTHER_SHA)) is None


def test_all_link_errors_are_reported(db, data_dir: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        _register(db, data_dir, problem="P9", experiment="E9", baseline="B9")
    message = caught.value.errors["links"]
    assert "문제" in message and "실험" in message and "기준선" in message


def test_oversized_ir_error_is_on_ir_field(tmp_path: Path, data_dir: Path) -> None:
    with TestClient(create_app(Settings(data_dir=data_dir, max_attachment_bytes=1024)), base_url=BASE_URL) as client:
        response = client.post("/projects/1/diagrams", data=_form(),
                               files={"html": ("a.html", HTML, "text/html"),
                                      "json_ir": ("big.json", b" " * 2048, "application/json")})
    assert response.status_code == 422
    assert 'id="json_ir-error"' in response.text


def test_links_from_other_sources_are_checked_when_drawn(db, data_dir: Path) -> None:
    """복구한 백업처럼 등록 검증을 거치지 않은 기록의 javascript: 주소는 링크로 그리지 않습니다."""
    diagram = _register(db, data_dir)
    diagram.repo_url = "javascript:alert(1)"
    db.commit()
    assert source_link(diagram, "src/app.py") is None
    with _client(data_dir) as client:
        page = client.get(f"/projects/1/diagrams/{diagram.short_id}").text
    assert 'href="javascript:' not in page and "javascript:alert(1)" in page
