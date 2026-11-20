"""FastAPI 앱 팩토리."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from rcompass import __version__
from rcompass.config import Settings, is_inside, source_checkout_root
from rcompass.db import open_database, session_factory

WEB_DIR = Path(__file__).parent


def create_app(settings: Settings) -> FastAPI:
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
    data_dir_inside_checkout = is_inside(settings.data_dir, source_checkout_root())

    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "home_empty.html",
            {
                "data_dir": str(settings.data_dir),
                "data_dir_inside_checkout": data_dir_inside_checkout,
                "version": __version__,
            },
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
