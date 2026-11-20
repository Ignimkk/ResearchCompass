"""로컬 전용 앱을 위한 요청 출처 검사.

인증이 없는 127.0.0.1 서버라도 브라우저가 열어 둔 다른 웹사이트가 이 서버로 폼을 보내거나
(교차 사이트 요청 위조), DNS 리바인딩으로 접근할 수 있습니다. 그래서
- Host 헤더는 127.0.0.1/localhost만 받고,
- 상태를 바꾸는 요청은 Origin(없으면 Referer)이 이 서버와 같을 때만 받습니다.
Origin과 Referer가 모두 없는 요청(curl 등 브라우저가 아닌 클라이언트)은 허용합니다.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

LOCAL_HOSTS = ("127.0.0.1", "localhost")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _netloc(value: str) -> str | None:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return None
    return parts.netloc.lower()


def _hostname(host_header: str) -> str:
    return urlsplit(f"//{host_header}").hostname or ""


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, allowed_hosts: Sequence[str] = LOCAL_HOSTS) -> None:
        super().__init__(app)
        self.allowed_hosts = {host.lower() for host in allowed_hosts}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = request.headers.get("host", "").lower()
        if _hostname(host) not in self.allowed_hosts:
            return PlainTextResponse(
                "이 주소로는 접속할 수 없습니다. http://127.0.0.1 로 접속하세요.", status_code=400
            )
        if request.method in UNSAFE_METHODS:
            source = request.headers.get("origin") or request.headers.get("referer")
            if source is not None and (source == "null" or _netloc(source) != host):
                return PlainTextResponse("다른 사이트에서 보낸 요청은 받지 않습니다.", status_code=403)
        return await call_next(request)
