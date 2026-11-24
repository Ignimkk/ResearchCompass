"""rcompass 명령줄 도구."""

from __future__ import annotations

import sys
from typing import Annotated, Optional, TextIO

import typer

from rcompass.config import SOURCE_LABELS, Settings, is_inside, resolve_data_dir, source_checkout_root

HOST = "127.0.0.1"  # 로컬 단일 사용자용: 외부 네트워크에 노출하지 않습니다.

app = typer.Typer(help="연구 나침반: 로컬 연구 기록 웹앱", no_args_is_help=True)


def ensure_utf8_output(*streams: TextIO) -> None:
    """콘솔이 아닌 출력(파일, 파이프)을 UTF-8로 바꿉니다.

    Windows에서 출력을 파일로 돌리면 기본 인코딩이 cp1252 등이 되어
    한국어 메시지 출력 시 UnicodeEncodeError가 납니다.
    """
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None or stream.isatty():
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
    if is_inside(resolved, source_checkout_root()):
        typer.secho(
            "경고: 데이터 디렉터리가 소스 저장소 안에 있습니다. "
            "개인 연구 데이터가 Git에 올라가지 않도록 저장소 밖 경로를 권장합니다.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    typer.echo(f"브라우저에서 http://{HOST}:{port} 을 여세요. (종료: Ctrl+C)")
    uvicorn.run(create_app(settings), host=HOST, port=port)


@app.command()
def migrate(data_dir: DataDirOption = None) -> None:
    """DB 스키마를 이 앱 버전에 맞게 올립니다."""
    from rcompass.db import STATUS_MESSAGES, SchemaStatus, check_schema, create_db_engine, upgrade_to_head

    resolved, source = resolve_data_dir(data_dir)
    settings = Settings(data_dir=resolved)
    typer.echo(f"데이터 디렉터리: {resolved} ({SOURCE_LABELS[source]})")
    status = check_schema(settings.db_path)
    if status is SchemaStatus.CURRENT:
        typer.echo("이미 최신 스키마입니다.")
        return
    if status in (SchemaStatus.UNVERSIONED, SchemaStatus.UNKNOWN):
        typer.secho(f"오류: {STATUS_MESSAGES[status]}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    engine = create_db_engine(settings.db_path)
    try:
        upgrade_to_head(engine)
    finally:
        engine.dispose()
    typer.echo("스키마를 최신 버전으로 올렸습니다.")


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


def _exit_if_schema_mismatch(settings: Settings) -> None:
    """서버를 띄우기 전에 기존 DB의 스키마를 확인합니다. 새 DB는 서버 시작 시 만들어집니다."""
    if not settings.db_path.exists():
        return
    from rcompass.db import STATUS_MESSAGES, SchemaStatus, check_schema

    status = check_schema(settings.db_path)
    if status not in (SchemaStatus.EMPTY, SchemaStatus.CURRENT):
        typer.secho(f"오류: {STATUS_MESSAGES[status]}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
