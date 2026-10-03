"""FastAPI app factory. Run: uvicorn atlas_api.main:app"""
from __future__ import annotations

import asyncio
import logging
import re
import signal
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout

from .ai import AI
from .cache import Store
from .config import Settings, get_settings
from .data import Data, Db
from .errors import error_response, install_error_handlers
from .routes import router

log = logging.getLogger("atlas_api")
_RID_OK = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_LOCAL_ORIGINS = r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?"


def cors_regex(origins: str) -> str:
    """'https://*.lovable.app,https://x.com' -> one anchored regex (+ localhost dev ports)."""
    parts = [_LOCAL_ORIGINS]
    for o in (o.strip().rstrip("/") for o in origins.split(",") if o.strip()):
        if o == "*":
            return ".*"
        parts.append(re.escape(o).replace(r"\*", r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*"))
    return "^(" + "|".join(parts) + ")$"


def _install_drain_hook(app: FastAPI, grace: float) -> None:
    """On SIGTERM: flip /readyz to 503 at once, then hand over to uvicorn's handler
    after `grace` seconds so the load balancer stops routing here before we stop."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        prev = signal.getsignal(sig)
        if not callable(prev):
            continue

        def handler(signum, frame, prev=prev):  # type: ignore[no-untyped-def]
            if not app.state.draining:
                log.info("signal %s: draining for %.1fs", signum, grace)
            app.state.draining = True
            loop.call_soon_threadsafe(loop.call_later, grace, prev, signum, frame)

        signal.signal(sig, handler)


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    logging.basicConfig(level=s.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = None
        if s.needs_db:
            if not s.database_url:
                log.error("DATA_MODE=%s / DB_ENDPOINTS=%r need DATABASE_URL", s.data_mode, s.db_endpoints)
            else:
                db = Db(s.database_url, s.db_pool_size)
                await db.open()
        app.state.store = Store.from_url(s.redis_url)
        app.state.data = Data(s, db)
        app.state.ai = AI(s, app.state.data)
        try:
            _install_drain_hook(app, s.shutdown_grace_seconds)
        except (ValueError, RuntimeError):  # not main thread (e.g. TestClient)
            pass
        log.info("instance=%s data_mode=%s db_endpoints=%r redis=%s", s.instance_id, s.data_mode,
                 s.db_endpoints, bool(s.redis_url))
        yield
        app.state.draining = True
        if db is not None:
            await db.close()
        await app.state.store.close()

    app = FastAPI(title="Rare Disease Atlas API", version="1.0.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.settings = s
    app.state.instance_id = s.instance_id
    app.state.draining = False
    install_error_handlers(app)

    async def _db_down(request: Request, exc: Exception) -> JSONResponse:
        log.warning("database unavailable: %s", exc)
        return error_response(request, 503, "upstream_unavailable", "database unavailable")

    app.add_exception_handler(psycopg.OperationalError, _db_down)
    app.add_exception_handler(PoolTimeout, _db_down)
    app.include_router(router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "instance": s.instance_id}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        st = request.app.state
        checks: dict[str, bool] = {"accepting": not st.draining}
        if s.needs_db:
            checks["db"] = st.data.db is not None and await st.data.db.ping()
        else:
            checks["fixtures"] = s.fixtures_dir.is_dir()
        if s.redis_url:
            checks["redis"] = await st.store.ping()
        ok = all(checks.values())
        return JSONResponse({"ready": ok, "instance": s.instance_id, "checks": checks},
                            status_code=200 if ok else 503, headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _RID_OK.match(incoming) else uuid.uuid4().hex
        request.state.request_id = rid
        t0 = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        response.headers["X-Served-By"] = s.instance_id
        if request.url.path not in ("/healthz", "/readyz"):
            log.info("%s %s %s %.1fms rid=%s client=%s", request.method, request.url.path, response.status_code,
                     (time.perf_counter() - t0) * 1000, rid, request.client.host if request.client else "-")
        return response

    app.add_middleware(
        CORSMiddleware, allow_origin_regex=cors_regex(s.cors_origins), allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-Id", "If-None-Match"],
        expose_headers=["X-Request-Id", "X-Served-By", "X-Cache", "ETag"], max_age=600)
    return app


app = create_app()
