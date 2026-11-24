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
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOCAL_HOSTS = ("127.0.0.1", "localhost")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _netloc(value: str) -> str | None:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return None
    return parts.netloc.lower()


def _hostname(host_header: str) -> str:
    return urlsplit(f"//{host_header}").hostname or ""


class BodySizeLimitMiddleware:
    """요청 본문이 상한을 넘으면 받는 도중에 413으로 끊습니다.

    Content-Length가 상한보다 크면 첫 조각을 읽을 때, 길이를 밝히지 않은(chunked) 요청은 누적 바이트가
    상한을 넘는 순간 중단합니다. 그래서 multipart 해석기가 거대한 본문을 임시 폴더에 다 받아 두는 일이 없습니다.
    HTTPException(413)으로 알리므로 앱의 한국어 오류 페이지로 보입니다(FastAPI는 본문 해석 중의
    HTTPException을 그대로 다시 던집니다).
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    def _too_large(self) -> HTTPException:
        limit_mb = max(1, self.max_bytes // (1024 * 1024))
        return HTTPException(
            status_code=413,
            detail=f"요청이 너무 큽니다(최대 약 {limit_mb}MB). 큰 데이터는 자료 화면에서 위치(경로나 URL)로 기록하세요.",
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope.get("headers") or []).get(b"content-length", b"")
        declared_too_large = length.isdigit() and int(length) > self.max_bytes
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            if declared_too_large:
                raise self._too_large()
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise self._too_large()
            return message

        await self.app(scope, limited_receive, send)


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
