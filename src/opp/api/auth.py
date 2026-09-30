"""Optional bearer-token auth (plan.md §11, §18, §29): a single shared secret for V1
remote use. Unset (the MVP default) means no auth at all, matching the localhost-bound,
single-operator posture; nothing changes for local/dev use. When a token is configured,
every request but ``/health`` (liveness probes must work unauthenticated) must present it —
either the normal ``Authorization: Bearer <token>`` header, or a ``?token=`` query
parameter, since the browser's native ``EventSource`` (the SSE progress stream) can't set
custom headers.

This is deliberately not a real auth system (no accounts, no rotation, no TLS here —
that's a reverse proxy's job, plan.md §29): one operator, one secret, checked in constant
time. CORS preflight (``OPTIONS``) is always let through unauthenticated so the browser can
still negotiate CORS before sending the real, checked request.
"""

import hmac

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

UNPROTECTED_PATHS = frozenset({"/health"})


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, token: str | None) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if (
            self._token is None
            or request.method == "OPTIONS"
            or request.url.path in UNPROTECTED_PATHS
        ):
            return await call_next(request)
        if not hmac.compare_digest(_presented_token(request), self._token):
            return JSONResponse(
                status_code=401,
                media_type="application/problem+json",
                content={
                    "title": "Unauthorized",
                    "status": 401,
                    "detail": "a valid bearer token is required",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


def _presented_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[len("bearer ") :].strip()
    return request.query_params.get("token", "")
