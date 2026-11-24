"""구조도 등록 (계획서 5절 'Archify 사용 흐름').

앱은 Archify를 직접 호출하지 않습니다. 연구자가 Claude Code와 Archify 스킬로 만든 HTML(필요하면 JSON IR도)을
올리면, 근거 구분(계획 단계 예상 / 코드 근거), 커밋, 관련 파일, 한국어 설명을 붙여 보관합니다.
HTML은 sandbox iframe으로만 보여줍니다(routes/diagrams.py).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcompass.ids import next_short_id
from rcompass.models import (
    Baseline,
    BaselineChange,
    BaselineChangeKind,
    Diagram,
    DiagramBasis,
    DiagramKind,
    Experiment,
    FileKind,
    FileObject,
    Problem,
    Project,
)
from rcompass.services import ValidationError
from rcompass.services.files import StoredBlob, add_file, blob_path

KIND_LABELS = {
    DiagramKind.RESEARCH_FLOW: "연구 절차도",
    DiagramKind.APP_CODE: "웹앱 코드 구조도",
    DiagramKind.RESEARCH_REPO: "연구 저장소 코드·데이터 흐름도",
}
BASIS_LABELS = {
    DiagramBasis.PLANNED: "계획 단계 예상 구조",
    DiagramBasis.CODE_BACKED: "코드 근거",
}
HTML_SUFFIXES = {".html", ".htm"}
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA_PREFIX_RE = re.compile(r"(?=[0-9]*[a-f])[0-9a-f]{7,40}")  # 숫자만인 버전 메모(예: 20261124)는 빼기
MAX_PATHS = 50
MAX_PATH_LENGTH = 300
MAX_EXPLANATION = 4000
MAX_IR_BYTES = 8 * 1024 * 1024
_MAX_IR_DEPTH = 40


# ---------------------------------------------------------------- 입력 정리


def _clean_path(raw: str) -> str | None:
    """저장소 기준 상대 경로로 정리한 값. 빈 줄은 "", 쓸 수 없는 경로는 None."""
    item = raw.strip().replace("\\", "/")
    while item.startswith("./"):
        item = item[2:]
    if not item:
        return ""
    if (item.startswith("/") or re.match(r"^[A-Za-z]:", item) or ".." in PurePosixPath(item).parts
            or len(item) > MAX_PATH_LENGTH or re.search(r"[\x00-\x1f\x7f]", item)):
        return None
    return item


def normalize_paths(text: str | None) -> tuple[list[str], str | None]:
    """줄마다 하나인 저장소 상대 경로를 정리합니다. (경로 목록, 오류 메시지)."""
    paths: list[str] = []
    for raw in (text or "").splitlines():
        item = _clean_path(raw)
        if item is None:
            return [], f"관련 파일은 저장소 기준 상대 경로로 적으세요(예: src/app.py). 올바르지 않은 줄: {raw.strip()[:80]}"
        if item and item not in paths:
            paths.append(item)
    if len(paths) > MAX_PATHS:
        return [], f"관련 파일은 {MAX_PATHS}개까지 적을 수 있습니다."
    return paths, None


_SCP_REMOTE_RE = re.compile(r"git@([A-Za-z0-9.-]+):([A-Za-z0-9._/-]+)")


def _clean_repo_url(text: str | None) -> tuple[str | None, str | None]:
    url = (text or "").strip()
    if not url:
        return None, None
    scp = _SCP_REMOTE_RE.fullmatch(url)  # git@github.com:me/qa-bot.git → https://github.com/me/qa-bot
    if scp:
        url = f"https://{scp.group(1)}/{scp.group(2)}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or re.search(r"[\s\x00-\x1f\x7f]", url):
        return None, "저장소 주소는 http(s)로 시작하는 원격 주소입니다. 예: https://github.com/me/qa-bot"
    return url.removesuffix(".git").rstrip("/"), None


@dataclass(frozen=True)
class IrHints:
    """Archify JSON IR에서 읽은 코드 근거. 폼에서 비워 둔 칸을 채울 때만 씁니다."""

    repo_url: str | None = None
    commit_sha: str | None = None
    paths: list[str] = field(default_factory=list)


def read_ir_hints(path: Path) -> IrHints:
    """JSON IR의 ``meta.repository``(url, revision)와 노드 ``sources[].path``를 읽습니다. 모드에 상관없이 찾습니다."""
    try:
        if path.stat().st_size > MAX_IR_BYTES:
            raise ValidationError({"json_ir": f"JSON IR이 {MAX_IR_BYTES // (1024 * 1024)}MB보다 큽니다."})
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValidationError({"json_ir": f"JSON IR을 읽을 수 없습니다: {error}"}) from None
    if not isinstance(data, dict):
        raise ValidationError({"json_ir": "JSON IR의 최상위는 객체여야 합니다."})
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    repository = meta.get("repository") if isinstance(meta.get("repository"), dict) else {}
    url = repository.get("url") if isinstance(repository.get("url"), str) else None
    revision = repository.get("revision") if isinstance(repository.get("revision"), str) else None

    paths: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if depth > _MAX_IR_DEPTH or len(paths) > MAX_PATHS * 4:
            return
        if isinstance(node, dict):
            sources = node.get("sources")
            if isinstance(sources, list):
                for source in sources:
                    if isinstance(source, dict) and isinstance(source.get("path"), str):
                        paths.append(source["path"])
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(data, 0)
    # 힌트는 사용자가 비워 둔 칸을 채우는 값이므로, 쓸 수 없는 값은 오류 없이 버립니다.
    cleaned: list[str] = []
    for raw in paths:
        item = _clean_path(raw)
        if item and item not in cleaned:
            cleaned.append(item)
    clean_url, url_error = _clean_repo_url(url)
    sha = (revision or "").strip().lower()
    return IrHints(None if url_error else clean_url, sha if FULL_SHA_RE.fullmatch(sha) else None,
                   cleaned[:MAX_PATHS])


# ---------------------------------------------------------------- 등록


def register_diagram(
    session: Session,
    project: Project,
    *,
    title: str,
    kind: DiagramKind,
    basis: DiagramBasis,
    html_blob: StoredBlob | None,
    html_name: str | None,
    ir_blob: StoredBlob | None = None,
    ir_name: str | None = None,
    ir_hints: IrHints | None = None,
    repo_url: str | None = None,
    commit_sha: str | None = None,
    related_paths: str | None = None,
    explanation: str | None = None,
    problem: str | None = None,
    experiment: str | None = None,
    baseline: str | None = None,
) -> Diagram:
    """구조도를 등록합니다. 호출한 쪽이 커밋합니다.

    코드 근거 구조도는 전체 커밋 SHA(40자)와 관련 파일 목록이 필수입니다. 폼에서 비운 칸은 JSON IR의
    ``meta.repository``와 ``sources``로 채웁니다.
    """
    errors: dict[str, str] = {}
    hints = ir_hints or IrHints()
    name = (title or "").strip()
    if not name:
        errors["title"] = "구조도 제목을 입력하세요."
    elif len(name) > 200:
        errors["title"] = "제목은 200자까지입니다."
    if html_blob is None:
        errors["html"] = "Archify로 만든 HTML 파일을 고르세요."
    elif PurePosixPath((html_name or "").replace("\\", "/")).suffix.lower() not in HTML_SUFFIXES:
        errors["html"] = "구조도는 .html 파일이어야 합니다."
    if ir_blob is not None and PurePosixPath((ir_name or "").replace("\\", "/")).suffix.lower() != ".json":
        errors["json_ir"] = "JSON IR은 .json 파일이어야 합니다."

    url, url_error = _clean_repo_url(repo_url) if (repo_url or "").strip() else (hints.repo_url, None)
    if url_error:
        errors["repo_url"] = url_error
    sha = (commit_sha or "").strip().lower() or hints.commit_sha
    if sha and not FULL_SHA_RE.fullmatch(sha):
        errors["commit_sha"] = "커밋은 전체 SHA(16진수 40자)로 적으세요. `git rev-parse HEAD`로 확인할 수 있습니다."
    paths, paths_error = normalize_paths(related_paths) if (related_paths or "").strip() else (hints.paths, None)
    if paths_error:
        errors["related_paths"] = paths_error
    if basis is DiagramBasis.CODE_BACKED:
        if not sha and "commit_sha" not in errors:
            errors["commit_sha"] = "코드 근거 구조도는 만든 커밋의 전체 SHA가 필요합니다."
        if not paths and "related_paths" not in errors:
            errors["related_paths"] = "코드 근거 구조도는 근거가 된 파일 경로가 하나 이상 필요합니다."
    text = (explanation or "").strip()
    if len(text) > MAX_EXPLANATION:
        errors["explanation"] = f"설명은 {MAX_EXPLANATION}자까지입니다."

    linked_problem = linked_experiment = linked_baseline = None
    link_errors: list[str] = []
    if (problem or "").strip():
        linked_problem = session.scalars(select(Problem).where(
            Problem.project_id == project.id, Problem.short_id == problem.strip())).one_or_none()
        if linked_problem is None:
            link_errors.append("연결할 문제를 찾을 수 없습니다.")
    if (experiment or "").strip():
        linked_experiment = session.scalars(select(Experiment).where(
            Experiment.project_id == project.id, Experiment.short_id == experiment.strip())).one_or_none()
        if linked_experiment is None:
            link_errors.append("연결할 실험을 찾을 수 없습니다.")
    if (baseline or "").strip():
        linked_baseline = session.scalars(select(Baseline).where(
            Baseline.project_id == project.id, Baseline.short_id == baseline.strip())).one_or_none()
        if linked_baseline is None:
            link_errors.append("연결할 기준선을 찾을 수 없습니다.")
    if link_errors:
        errors["links"] = " ".join(link_errors)
    if errors:
        raise ValidationError(errors)

    html_file = add_file(session, project, html_blob, original_name=html_name or "diagram.html",
                         kind=FileKind.DIAGRAM, caption=name)
    ir_file = None
    if ir_blob is not None:
        ir_file = add_file(session, project, ir_blob, original_name=ir_name or "diagram.json",
                           kind=FileKind.DIAGRAM, caption=f"{name} (JSON IR)")
    diagram = Diagram(
        project_id=project.id,
        short_id=next_short_id(session, project.id, "G"),
        kind=kind,
        basis=basis,
        title=name,
        file_id=html_file.id,
        json_ir_file_id=ir_file.id if ir_file else None,
        repo_url=url,
        commit_sha=sha,
        related_paths="\n".join(paths) or None,
        explanation_ko=text or None,
        linked_problem_id=linked_problem.id if linked_problem else None,
        linked_experiment_id=linked_experiment.id if linked_experiment else None,
        linked_baseline_id=linked_baseline.id if linked_baseline else None,
    )
    session.add(diagram)
    session.flush()
    return diagram


# ---------------------------------------------------------------- 조회


def list_diagrams(session: Session, project_id: int) -> list[Diagram]:
    return list(session.scalars(
        select(Diagram).where(Diagram.project_id == project_id).order_by(Diagram.id.desc())
    ))


def get_diagram(session: Session, project_id: int, short_id: str) -> Diagram | None:
    return session.scalars(
        select(Diagram).where(Diagram.project_id == project_id, Diagram.short_id == short_id)
    ).one_or_none()


def diagram_html_path(session: Session, data_dir: Path, diagram: Diagram) -> Path | None:
    """보관한 HTML의 경로. 기록이 없거나 파일이 사라졌으면 None."""
    record = session.get(FileObject, diagram.file_id)
    if record is None or not record.rel_path:
        return None
    try:
        path = blob_path(data_dir, record.rel_path)
    except ValueError:
        return None
    return path if path.is_file() else None


def related_path_list(diagram: Diagram) -> list[str]:
    return [line for line in (diagram.related_paths or "").splitlines() if line.strip()]


def repo_href(diagram: Diagram) -> str | None:
    """화면 링크로 써도 되는 저장소 주소. 등록 때 검증하지만, 복구한 백업처럼 다른 경로로 들어온 기록도
    있으므로 그릴 때 http(s)인지 한 번 더 봅니다."""
    url = diagram.repo_url or ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or re.search(r"[\s\x00-\x1f\x7f]", url):
        return None
    return url


def source_link(diagram: Diagram, path: str) -> str | None:
    """GitHub·GitLab 원격이면 고정한 커밋의 파일 주소. 그 밖에는 None(경로만 표시)."""
    if repo_href(diagram) is None or not diagram.commit_sha or not FULL_SHA_RE.fullmatch(diagram.commit_sha):
        return None
    host = urlsplit(diagram.repo_url).netloc.lower()
    encoded = quote(path, safe="/")
    if host == "github.com" or host.endswith(".github.com"):
        return f"{diagram.repo_url}/blob/{diagram.commit_sha}/{encoded}"
    if "gitlab" in host:
        return f"{diagram.repo_url}/-/blob/{diagram.commit_sha}/{encoded}"
    return None


# ---------------------------------------------------------------- 오래됨 배지


@dataclass(frozen=True)
class Freshness:
    stale: bool
    message: str


def _last_baseline_move(session: Session, project: Project) -> BaselineChange | None:
    """현재 기준선이 정해진 마지막 변경(B0 스냅샷 갱신은 기준선을 옮기지 않으므로 뺍니다)."""
    return session.scalars(
        select(BaselineChange)
        .where(BaselineChange.project_id == project.id, BaselineChange.kind != BaselineChangeKind.SNAPSHOT_UPDATE)
        .order_by(BaselineChange.id.desc())
        .limit(1)
    ).first()


def baseline_commit(session: Session, baseline: Baseline) -> str | None:
    """기준선을 만든 실험의 코드 버전이 커밋 SHA처럼 보이면 그 값(소문자). 버전 메모면 None."""
    source = session.get(Experiment, baseline.source_experiment_id)
    code = ((source.code_ref if source else None) or "").strip().lower()
    return code if SHA_PREFIX_RE.fullmatch(code) else None


def freshness(session: Session, project: Project, diagram: Diagram) -> Freshness | None:
    """구조도가 현재 기준선과 맞는지. 비교할 근거가 없으면 None.

    - 연구 저장소 코드 근거 구조도: 현재 기준선 코드 커밋과 구조도 커밋을 비교합니다. 다르면, 기준선이
      구조도보다 나중에 정해졌을 때 "오래됨", 아니면 "다른 커밋"입니다. 앱은 Git을 읽지 않으므로 커밋의
      선후는 모르고, 앱에 기록된 시각만 씁니다.
    - 연구 절차도: 기준선이 구조도를 만든 뒤에 바뀌었으면 "오래됨"입니다.
    """
    if project.current_baseline_id is None:
        return None
    baseline = session.get(Baseline, project.current_baseline_id)
    move = _last_baseline_move(session, project)
    moved_after = move is not None and _after(move.created_at, diagram.created_at)
    if diagram.kind is DiagramKind.RESEARCH_REPO and diagram.basis is DiagramBasis.CODE_BACKED and diagram.commit_sha:
        code = baseline_commit(session, baseline)
        if code is None:
            return None
        if diagram.commit_sha.startswith(code):
            return Freshness(False, f"현재 기준선 {baseline.short_id}의 코드 {code[:7]}와 같은 커밋입니다.")
        if moved_after:
            return Freshness(True, f"구조도가 기준선보다 오래됨: 현재 기준선 {baseline.short_id}의 코드는 {code[:7]}, "
                                   f"구조도는 {diagram.commit_sha[:7]}입니다. 기준선 커밋에서 다시 만드세요.")
        return Freshness(True, f"현재 기준선 {baseline.short_id}의 코드 {code[:7]}와 다른 커밋"
                               f"({diagram.commit_sha[:7]})에서 만든 구조도입니다.")
    if diagram.kind is DiagramKind.RESEARCH_FLOW and moved_after:
        return Freshness(True, f"구조도가 기준선보다 오래됨: 만든 뒤 기준선이 {baseline.short_id}(으)로 바뀌었습니다. "
                               "새 기록으로 다시 만드세요.")
    return None


def _after(left: datetime, right: datetime) -> bool:
    return left.replace(tzinfo=None) > right.replace(tzinfo=None)
