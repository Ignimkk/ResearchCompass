"""FastAPI 앱 팩토리."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from rcompass import __version__
from rcompass.config import Settings
from rcompass.db import open_database, session_factory
from rcompass.web.routes import backups, diagrams, experiments, handoff, materials, problems, projects
from rcompass.web.security import LOCAL_HOSTS, BodySizeLimitMiddleware, LocalOnlyMiddleware
from rcompass.web.templating import WEB_DIR, templates

ERROR_TITLES = {404: "페이지를 찾을 수 없습니다", 405: "허용되지 않는 요청입니다", 413: "요청이 너무 큽니다"}


def _error_page(request: Request, status_code: int, title: str, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html", {"title": title, "message": message}, status_code=status_code
    )


def create_app(settings: Settings, *, allowed_hosts: Sequence[str] = LOCAL_HOSTS) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 서버가 실제로 시작될 때만 데이터 디렉터리와 DB를 만듭니다.
        engine = open_database(settings.db_path)
        try:
            app.state.engine = engine
            app.state.sessions = session_factory(engine)
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="연구 나침반", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    # 한 요청에 파일이 둘까지 올 수 있고(구조도 HTML + JSON IR), 폼 필드와 multipart 오버헤드를 위해 1MB 여유를
    # 둡니다. 파일마다 한도는 store_stream이 따로 지킵니다. 나중에 추가한 미들웨어가 바깥쪽입니다.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_attachment_bytes * 2 + 1024 * 1024)
    app.add_middleware(LocalOnlyMiddleware, allowed_hosts=allowed_hosts)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        title = ERROR_TITLES.get(exc.status_code, "요청을 처리할 수 없습니다")
        # 라우트가 직접 넣은 한국어 detail만 보여주고, 프레임워크 기본 영어 문구는 감춥니다.
        is_default = exc.detail in (None, "Not Found", "Method Not Allowed") or not isinstance(exc.detail, str)
        message = "주소를 다시 확인해 주세요." if is_default else exc.detail
        return _error_page(request, exc.status_code, title, message)

    @app.exception_handler(RequestValidationError)
    async def bad_address(request: Request, exc: RequestValidationError) -> HTMLResponse:
        # 폼은 직접 검증하므로 여기 오는 것은 /projects/abc 같은 잘못된 주소입니다.
        return _error_page(request, 404, ERROR_TITLES[404], "주소를 다시 확인해 주세요.")

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(projects.router)
    app.include_router(problems.router)
    app.include_router(experiments.router)
    app.include_router(materials.router)
    app.include_router(handoff.router)
    app.include_router(backups.router)
    app.include_router(diagrams.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
