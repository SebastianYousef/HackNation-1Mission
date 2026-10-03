"""ApiError body ({error:{code,message,request_id}}) for every non-2xx response."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("atlas_api")

ErrorCode = Literal["not_found", "bad_request", "rate_limited", "upstream_unavailable", "internal"]
_STATUS_CODE: dict[int, ErrorCode] = {
    400: "bad_request", 404: "not_found", 405: "bad_request", 413: "bad_request",
    415: "bad_request", 422: "bad_request", 429: "rate_limited", 502: "upstream_unavailable",
    503: "upstream_unavailable", 504: "upstream_unavailable",
}


class ApiError(Exception):
    def __init__(self, status: int, code: ErrorCode, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def bad_request(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def unavailable(message: str, status: int = 503) -> ApiError:
    return ApiError(status, "upstream_unavailable", message)


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


def error_response(request: Request, status: int, code: ErrorCode, message: str,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    h = dict(headers or {})
    # Set here too: 500s are produced outside the header middleware.
    h.setdefault("X-Request-Id", request_id(request))
    served_by = getattr(request.app.state, "instance_id", None)
    if served_by:
        h.setdefault("X-Served-By", served_by)
    h.setdefault("Cache-Control", "no-store")
    return JSONResponse({"error": {"code": code, "message": message, "request_id": request_id(request)}},
                        status_code=status, headers=h)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(request, exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODE.get(exc.status_code, "internal" if exc.status_code >= 500 else "bad_request")
        msg = "route not found" if exc.status_code == 404 else str(exc.detail)
        return error_response(request, exc.status_code, code, msg, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        parts = []
        for e in exc.errors()[:5]:
            loc = ".".join(str(x) for x in e.get("loc", ()) if x not in ("body", "query", "path"))
            parts.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
        return error_response(request, 422, "bad_request", "; ".join(parts) or "invalid request")

    @app.exception_handler(Exception)
    async def _internal(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error request_id=%s", request_id(request))
        return error_response(request, 500, "internal", "internal server error")
