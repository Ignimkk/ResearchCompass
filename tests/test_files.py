"""5단계 서비스: 첨부 파일 저장, 외부 참조 지문, 계보."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.models import FileKind, FileObject, Project, StorageKind, file_derivations
from rcompass.services import ValidationError
from rcompass.services.datasets import VerifyResult, add_dataset_version, get_version, verify_external
from rcompass.services.experiments import create_experiment, update_inputs
from rcompass.services.files import (
    FileTooLarge,
    add_file,
    blob_path,
    preview_kind,
    safe_original_name,
    store_stream,
)
from rcompass.services.lineage import file_lineage
from rcompass.services.problems import add_problem
from rcompass.services.runs import get_run, parse_results_table, record_results


def _blobs(data_dir: Path) -> list[Path]:
    return [p for p in (data_dir / "files" / "sha256").rglob("*") if p.is_file()]


# ---------------------------------------------------------------- 저장


def test_same_content_is_stored_once(tmp_path: Path) -> None:
    first = store_stream(tmp_path, io.BytesIO(b"figure-bytes"), 1024)
    second = store_stream(tmp_path, io.BytesIO(b"figure-bytes"), 1024)
    assert first == second
    assert first.rel_path == f"files/sha256/{first.sha256[:2]}/{first.sha256}"
    assert len(_blobs(tmp_path)) == 1
    assert list((tmp_path / "files" / "tmp").iterdir()) == []  # 임시 파일 정리


def test_size_limit_stops_and_leaves_nothing(tmp_path: Path) -> None:
    with pytest.raises(FileTooLarge):
        store_stream(tmp_path, io.BytesIO(b"x" * 2049), 2048)
    assert _blobs(tmp_path) == []
    assert list((tmp_path / "files" / "tmp").iterdir()) == []


@pytest.mark.parametrize(
    "rel_path",
    ["../rcompass.db", "files/sha256/ab/../../../rcompass.db", "files/other/ab/" + "a" * 64,
     "files/sha256/zz/" + "a" * 64, "files/sha256/aa/" + "A" * 64, "/etc/passwd"],
)
def test_blob_path_rejects_anything_outside_store(tmp_path: Path, rel_path: str) -> None:
    with pytest.raises(ValueError):
        blob_path(tmp_path, rel_path)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("../../evil.png", "evil.png"), ("C:\\Users\\me\\plot.png", "plot.png"), ("a\x00b\nc.csv", "abc.csv"),
     ("", "file"), ("   ", "file")],
)
def test_original_names_lose_path_parts(name: str, expected: str) -> None:
    assert safe_original_name(name) == expected


# ---------------------------------------------------------------- 파일 기록


@pytest.fixture
def setup(session: Session, project: Project, tmp_path: Path):
    add_problem(session, project, title="장문 지문 오답", why="18개 중 7개가 장문")
    experiment = create_experiment(session, project, problem_short_id="P1", title="문단 요약 후 답변",
                                   hypothesis="문단별 요약 후 답하면 장문 오답이 준다",
                                   decision_rule="평균 오답 ≤ 17이고 다른 유형 악화 없음 → 채택")
    version = add_dataset_version(session, project, dataset_name="eval-100", version_label="v1.0",
                                  external_uri="D:/data/eval100_v1.jsonl", source="내부 QA 수집")
    update_inputs(session, experiment, dataset_version_ids=[version.version.id], code_ref="a1b2c3d",
                  config_text="configs/summarize.yaml")
    record_results(session, project, experiment,
                   parse_results_table("label\twrong_count\nseed=1\t11\nseed=2\t12\nseed=3\t10"),
                   dataset_version_id=version.version.id)
    session.commit()
    return experiment, version


def test_run_must_belong_to_experiment(session: Session, project: Project, tmp_path: Path, setup) -> None:
    experiment, _ = setup
    other = create_experiment(session, project, problem_short_id="P1", title="다른", hypothesis="h", decision_rule="r")
    record_results(session, project, other, parse_results_table("label\twrong_count\nx\t1"))
    blob = store_stream(tmp_path, io.BytesIO(b"png"), 1024)
    with pytest.raises(ValidationError):
        add_file(session, project, blob, original_name="a.png", experiment=experiment, run=get_run(session, other, "R1"))


@pytest.mark.parametrize(
    ("name", "kind"),
    [("plot.png", "image"), ("plot.JPG", "image"), ("plot.svg", "none"), ("page.html", "none"),
     ("wrong.csv", "text"), ("config.yaml", "text"), ("report.pdf", "none")],
)
def test_preview_kind_only_inlines_safe_types(session: Session, project: Project, tmp_path: Path,
                                              name: str, kind: str) -> None:
    blob = store_stream(tmp_path, io.BytesIO(name.encode()), 1024)
    assert preview_kind(add_file(session, project, blob, original_name=name)) == kind


def test_lineage_reproduces_plan_example(session: Session, project: Project, tmp_path: Path, setup) -> None:
    """계획서 4절: 그래프 → 실행 → 실험(문제, 가설, 판정 기준) → 데이터 버전 → 코드·설정 → 결과."""
    experiment, version = setup
    config = add_file(session, project, store_stream(tmp_path, io.BytesIO(b"model: x"), 1024),
                      original_name="summarize.yaml", kind=FileKind.CONFIG, experiment=experiment)
    figure = add_file(session, project, store_stream(tmp_path, io.BytesIO(b"png"), 1024),
                      original_name="오답유형_E2.png", kind=FileKind.FIGURE, experiment=experiment,
                      run=get_run(session, experiment, "R1"), derived_from_version_ids=[version.version.id])
    lineage = file_lineage(session, figure)
    assert lineage.run.label == "seed=1"
    assert lineage.experiment.id == experiment.id
    assert [p.title for p in lineage.path] == ["장문 지문 오답"]
    assert [v.label for v in lineage.derived_from] == ["DS1 eval-100 v1.0"]
    assert lineage.config_files == [config]
    assert lineage.results.metrics[0].display == "11.0 ± 1.0 (n=3)"
    assert lineage.decisions == [] and lineage.baselines == []


# ---------------------------------------------------------------- 데이터 버전: 첨부와 지문


def test_attached_dataset_version(session: Session, project: Project, tmp_path: Path) -> None:
    blob = store_stream(tmp_path, io.BytesIO(b"q1,a1\n"), 1024)
    record = add_file(session, project, blob, original_name="small.csv", kind=FileKind.DATA)
    view = add_dataset_version(session, project, dataset_name="small", version_label="v1", attached_file=record)
    assert view.version.storage is StorageKind.ATTACHED
    assert view.version.sha256 == blob.sha256 and view.version.external_uri is None
    with pytest.raises(ValidationError):
        verify_external(session, view)
    with pytest.raises(ValidationError) as error:
        add_dataset_version(session, project, dataset_name="small", version_label="v2", external_uri="x",
                            attached_file=record)
    assert "external_uri" in error.value.errors


def test_fingerprint_record_match_mismatch_missing_and_url(session: Session, project: Project,
                                                           tmp_path: Path) -> None:
    data = tmp_path / "eval100.jsonl"
    data.write_bytes(b'{"q": 1}\n')
    view = add_dataset_version(session, project, dataset_name="eval-100", version_label="v1.0",
                               external_uri=str(data))
    assert verify_external(session, view) is VerifyResult.RECORDED
    recorded = view.version.sha256
    assert recorded and view.version.size_bytes == 9 and view.version.last_verified_at
    assert verify_external(session, view) is VerifyResult.MATCH
    data.write_bytes(b'{"q": 2}\n')
    assert verify_external(session, view) is VerifyResult.MISMATCH
    assert view.version.sha256 == recorded  # 불일치여도 기록은 그대로
    assert data.read_bytes() == b'{"q": 2}\n'  # 원본은 읽기만

    data.unlink()
    assert verify_external(session, view) is VerifyResult.MISSING
    url = add_dataset_version(session, project, dataset_name="remote", version_label="v1",
                              external_uri="https://example.org/eval.jsonl")
    assert verify_external(session, url) is VerifyResult.NOT_LOCAL


def test_derivation_rows_are_unique(session: Session, project: Project, tmp_path: Path, setup) -> None:
    experiment, version = setup
    record = add_file(session, project, store_stream(tmp_path, io.BytesIO(b"t"), 1024), original_name="t.csv",
                      experiment=experiment, derived_from_version_ids=[version.version.id, version.version.id])
    rows = session.execute(select(file_derivations).where(file_derivations.c.file_id == record.id)).all()
    assert len(rows) == 1
    assert get_version(session, project.id, version.version.id) is not None
    assert session.scalars(select(FileObject)).all()
