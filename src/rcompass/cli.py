"""rcompass 명령줄 도구."""

from __future__ import annotations

import os
import sys
from typing import Annotated, NoReturn, Optional, TextIO

import typer

from rcompass.config import SOURCE_LABELS, Settings, is_inside, resolve_data_dir, source_checkout_root

HOST = "127.0.0.1"  # 로컬 단일 사용자용: 외부 네트워크에 노출하지 않습니다.

app = typer.Typer(help="연구 나침반: 로컬 연구 기록 웹앱", no_args_is_help=True)


def ensure_utf8_output(*streams: TextIO) -> None:
    """콘솔이 아닌 출력(파일, 파이프)을 UTF-8로 바꿉니다.

    Windows에서 출력을 파일로 돌리면 기본 인코딩이 cp1252 등이 되어
    한국어 메시지 출력 시 UnicodeEncodeError가 납니다. Windows의 ``NUL``(``> /dev/null``)은 문자 장치라
    ``isatty()``가 True이므로, Windows에서는 isatty를 보지 않습니다. 실제 콘솔은 이미 UTF-8입니다(PEP 528).
    """
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None or (stream.isatty() and os.name != "nt"):
            continue
        if (stream.encoding or "").lower().replace("-", "") != "utf8":
            reconfigure(encoding="utf-8", errors="replace")


def run() -> None:
    """콘솔 스크립트 진입점. Click이 도움말을 출력하기 전에 인코딩을 맞춥니다."""
    ensure_utf8_output(sys.stdout, sys.stderr)
    app()


@app.callback()
def main() -> None:
    """연구 나침반 명령 모음."""


# Path 타입을 쓰면 빈 문자열이 Path(".")(현재 폴더)로 바뀌므로 문자열로 받습니다.
DataDirOption = Annotated[
    Optional[str],
    typer.Option("--data-dir", help="데이터 디렉터리 (기본: RCOMPASS_DATA_DIR 또는 사용자 데이터 경로)"),
]


@app.command()
def serve(
    port: Annotated[int, typer.Option(min=1, max=65535, help="웹 서버 포트")] = 8765,
    data_dir: DataDirOption = None,
) -> None:
    """웹앱을 http://127.0.0.1:<port> 에서 실행합니다."""
    import uvicorn

    from rcompass.web.app import create_app

    resolved, source = resolve_data_dir(data_dir)
    settings = Settings(data_dir=resolved)
    typer.echo(f"데이터 디렉터리: {resolved} ({SOURCE_LABELS[source]})")
    _exit_if_schema_mismatch(settings)
    _warn_if_inside_checkout(resolved)
    typer.echo(f"브라우저에서 http://{HOST}:{port} 을 여세요. (종료: Ctrl+C)")
    uvicorn.run(create_app(settings), host=HOST, port=port)


@app.command()
def migrate(data_dir: DataDirOption = None) -> None:
    """DB 스키마를 이 앱 버전에 맞게 올립니다. 올리기 전에 자동으로 백업합니다."""
    from rcompass.db import STATUS_MESSAGES, SchemaStatus, check_schema, create_db_engine, upgrade_to_head
    from rcompass.services.backup import BackupError, create_backup

    resolved, source = resolve_data_dir(data_dir)
    settings = Settings(data_dir=resolved)
    typer.echo(f"데이터 디렉터리: {resolved} ({SOURCE_LABELS[source]})")
    status = check_schema(settings.db_path)
    if status is SchemaStatus.CURRENT:
        typer.echo("이미 최신 스키마입니다.")
        return
    if status in (SchemaStatus.UNVERSIONED, SchemaStatus.UNKNOWN):
        _fail(STATUS_MESSAGES[status])
    if status is SchemaStatus.OUTDATED:  # 빈 DB는 백업할 기록이 없습니다.
        try:
            backup = create_backup(resolved)
        except BackupError as error:
            _fail(f"업데이트 전 백업을 만들지 못해 중단합니다. {error}")
        typer.echo(f"업데이트 전 백업: {backup.path}")
    engine = create_db_engine(settings.db_path)
    try:
        upgrade_to_head(engine)
    finally:
        engine.dispose()
    typer.echo("스키마를 최신 버전으로 올렸습니다.")
    if status is SchemaStatus.OUTDATED:
        typer.echo("문제가 생기면 위 백업 파일로 `rcompass restore <zip> --force`를 실행해 되돌릴 수 있습니다.")


@app.command()
def init(
    example: Annotated[bool, typer.Option("--example", help="예제 프로젝트 \"QA 평가 오답 줄이기\"를 함께 만듭니다")] = False,
    data_dir: DataDirOption = None,
) -> None:
    """데이터 디렉터리와 빈 DB를 만듭니다. --example이면 예제 프로젝트도 심습니다."""
    from sqlalchemy import select

    from rcompass.db import SchemaError, open_database, session_factory
    from rcompass.example import EXAMPLE_TITLE, seed_example
    from rcompass.models import Project

    resolved, source = resolve_data_dir(data_dir)
    settings = Settings(data_dir=resolved)
    typer.echo(f"데이터 디렉터리: {resolved} ({SOURCE_LABELS[source]})")
    _warn_if_inside_checkout(resolved)
    existed = settings.db_path.exists()
    try:
        engine = open_database(settings.db_path)
    except SchemaError as error:
        _fail(str(error))
    try:
        typer.echo("기존 DB를 그대로 씁니다." if existed else "새 DB를 만들었습니다.")
        if example:
            with session_factory(engine)() as session:
                found = session.scalars(select(Project).where(Project.title == EXAMPLE_TITLE)).first()
                session.rollback()  # 첨부 파일을 저장하는 동안 쓰기 잠금을 잡지 않습니다.
                if found is not None:
                    typer.echo(f"예제 프로젝트가 이미 있습니다: /projects/{found.id}")
                else:
                    project = seed_example(session, resolved, max_bytes=settings.max_attachment_bytes)
                    session.commit()
                    typer.echo(f"예제 프로젝트를 만들었습니다: /projects/{project.id}")
    finally:
        engine.dispose()
    typer.echo("`rcompass serve`로 서버를 띄운 뒤 브라우저에서 http://127.0.0.1:8765 을 여세요.")


@app.command()
def backup(
    output: Annotated[Optional[str], typer.Option("--output", "-o", help="백업 zip을 둘 폴더 (기본: <데이터 디렉터리>/backups)")] = None,
    data_dir: DataDirOption = None,
) -> None:
    """DB와 첨부 파일을 zip 하나로 백업합니다. 서버가 켜져 있어도 됩니다."""
    from pathlib import Path

    from rcompass.services.backup import BackupError, create_backup

    resolved, source = resolve_data_dir(data_dir)
    typer.echo(f"데이터 디렉터리: {resolved} ({SOURCE_LABELS[source]})")
    _warn_if_inside_checkout(resolved)
    try:
        result = create_backup(resolved, dest_dir=Path(output).expanduser().resolve() if output else None)
    except BackupError as error:
        _fail(str(error))
    manifest = result.manifest
    typer.echo(f"백업을 만들었습니다: {result.path}")
    typer.echo(f"  레코드 {manifest.total_records}개, 첨부 파일 {len(manifest.files)}개, 스키마 {manifest.schema_revision}")
    for warning in manifest.warnings:
        typer.secho(f"  경고: {warning}", fg=typer.colors.YELLOW, err=True)
    if manifest.external_refs:
        typer.echo(f"  외부 참조 {len(manifest.external_refs)}건은 백업에 들어 있지 않습니다. 별도로 보관하세요:")
        for ref in manifest.external_refs:
            typer.echo(f"  - {ref.ref}: {ref.uri}")


@app.command()
def restore(
    zip_file: Annotated[str, typer.Argument(help="rcompass backup으로 만든 zip 파일")],
    force: Annotated[bool, typer.Option("--force", help="기존 기록이 있어도 먼저 백업한 뒤 바꿉니다")] = False,
    skip_hash: Annotated[bool, typer.Option("--skip-hash", help="외부 참조의 sha256 계산을 건너뜁니다(큰 파일)")] = False,
    check_network: Annotated[bool, typer.Option(
        "--check-network", help="NAS 같은 네트워크 경로(UNC)의 외부 참조도 확인합니다. 직접 만든 백업에만 쓰세요")] = False,
    data_dir: DataDirOption = None,
) -> None:
    """백업 zip을 검증하고 데이터 디렉터리로 복구합니다. 서버를 끈 상태에서 실행하세요."""
    from pathlib import Path

    from rcompass.services.backup import BackupError, restore_backup

    resolved, source = resolve_data_dir(data_dir)
    typer.echo(f"데이터 디렉터리: {resolved} ({SOURCE_LABELS[source]})")
    _warn_if_inside_checkout(resolved)
    try:
        result = restore_backup(Path(zip_file).expanduser(), resolved, force=force,
                                verify_external_hash=not skip_hash, check_network=check_network,
                                progress=typer.echo)
    except BackupError as error:
        _fail(str(error))
    manifest = result.manifest
    if result.pre_restore_backup:
        typer.echo(f"기존 데이터 백업: {result.pre_restore_backup}")
    typer.echo(f"복구했습니다: 레코드 {manifest.total_records}개와 첨부 파일 {len(manifest.files)}개의 수와 sha256이 "
               f"백업({manifest.created_local})과 일치합니다.")
    if result.migrated:
        typer.echo(f"이전 버전 백업(스키마 {manifest.schema_revision})이라 최신 스키마로 올렸습니다.")
    for warning in [*manifest.warnings, *result.warnings]:
        typer.secho(f"경고: {warning}", fg=typer.colors.YELLOW, err=True)
    if result.external:
        typer.echo("외부 참조 점검:")
        for check in result.external:
            typer.echo(f"  - [{check.label}] {check.ref.ref}: {check.ref.uri}")


@app.command()
def handoff(
    project_id: Annotated[Optional[int], typer.Argument(help="프로젝트 번호 (프로젝트가 하나면 생략 가능)")] = None,
    experiment: Annotated[Optional[str], typer.Option("--experiment", "-e", help="결과 보고 형식에 넣을 실험 ID (예: E3)")] = None,
    output: Annotated[Optional[str], typer.Option("--output", "-o", help="Markdown을 저장할 파일 (기본: 화면 출력)")] = None,
    data_dir: DataDirOption = None,
) -> None:
    """Claude Code 새 세션에 붙여넣을 인계 Markdown을 만듭니다. DB는 읽기만 합니다."""
    from datetime import date
    from pathlib import Path

    from rcompass.db import STATUS_MESSAGES, SchemaStatus, check_schema, create_db_engine, session_factory
    from rcompass.services.experiments import get_experiment
    from rcompass.services.handoff import HandoffOptions, build_handoff
    from rcompass.services.projects import get_project, list_projects

    resolved, _ = resolve_data_dir(data_dir)
    settings = Settings(data_dir=resolved)
    if not settings.db_path.exists():
        typer.secho(f"오류: {settings.db_path}에 기록이 없습니다. --data-dir을 확인하세요.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    status = check_schema(settings.db_path)
    if status is not SchemaStatus.CURRENT:
        typer.secho(f"오류: {STATUS_MESSAGES[status]}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    engine = create_db_engine(settings.db_path)
    try:
        with session_factory(engine)() as session:
            if project_id is None:
                projects = list_projects(session)
                if len(projects) != 1:
                    names = ", ".join(f"{p.id} {p.title}" for p in projects) or "없음"
                    typer.secho(f"오류: 프로젝트 번호를 적어 주세요. 프로젝트: {names}", fg=typer.colors.RED, err=True)
                    raise typer.Exit(code=2)
                project = projects[0]
            else:
                project = get_project(session, project_id)
                if project is None:
                    typer.secho(f"오류: 프로젝트 {project_id}을(를) 찾을 수 없습니다.", fg=typer.colors.RED, err=True)
                    raise typer.Exit(code=1)
            target = None
            if experiment:
                target = get_experiment(session, project.id, experiment.strip().upper())
                if target is None:
                    typer.secho(f"오류: 실험 {experiment}을(를) 찾을 수 없습니다.", fg=typer.colors.RED, err=True)
                    raise typer.Exit(code=1)
            text = build_handoff(session, project, HandoffOptions(today=date.today(), experiment=target))
            session.rollback()
    finally:
        engine.dispose()
    if output:
        Path(output).write_text(text, encoding="utf-8")
        typer.echo(f"인계 문서를 {output}에 저장했습니다.", err=True)
    else:
        typer.echo(text, nl=False)


def _warn_if_inside_checkout(data_dir) -> None:
    if is_inside(data_dir, source_checkout_root()):
        typer.secho(
            "경고: 데이터 디렉터리가 소스 저장소 안에 있습니다. "
            "개인 연구 데이터가 Git에 올라가지 않도록 저장소 밖 경로를 권장합니다.",
            fg=typer.colors.YELLOW,
            err=True,
        )


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.secho(f"오류: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def _exit_if_schema_mismatch(settings: Settings) -> None:
    """서버를 띄우기 전에 기존 DB의 스키마를 확인합니다. 새 DB는 서버 시작 시 만들어집니다."""
    if not settings.db_path.exists():
        return
    from rcompass.db import STATUS_MESSAGES, SchemaStatus, check_schema

    status = check_schema(settings.db_path)
    if status not in (SchemaStatus.EMPTY, SchemaStatus.CURRENT):
        typer.secho(f"오류: {STATUS_MESSAGES[status]}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
