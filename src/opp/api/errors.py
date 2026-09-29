"""HTTP error shape for the API: RFC-7807-style JSON problems (plan.md §11).

Routers raise :class:`Problem`; the registered handler renders it. FastAPI's own
validation errors are re-shaped to the same envelope so every error looks the same."""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_MEDIA_TYPE = "application/problem+json"


class Problem(Exception):
    """An HTTP error carrying a status code and a human-readable detail."""

    def __init__(self, status_code: int, detail: str, *, title: str | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.title = title or _TITLES.get(status_code, "Error")


_UNPROCESSABLE = 422

_TITLES = {
    status.HTTP_400_BAD_REQUEST: "Bad Request",
    status.HTTP_404_NOT_FOUND: "Not Found",
    status.HTTP_409_CONFLICT: "Conflict",
    _UNPROCESSABLE: "Unprocessable Entity",
    status.HTTP_503_SERVICE_UNAVAILABLE: "Service Unavailable",
}


def _body(status_code: int, title: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"title": title, "status": status_code, "detail": detail, **extra}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _on_problem(_: Request, exc: Problem) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            media_type=_MEDIA_TYPE,
            content=_body(exc.status_code, exc.title, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _on_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        code = _UNPROCESSABLE
        return JSONResponse(
            status_code=code,
            media_type=_MEDIA_TYPE,
            content=_body(
                code,
                _TITLES[code],
                "the request body did not validate",
                errors=jsonable_encoder(exc.errors()),
            ),
        )
