from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api import auth, chat, dev, jobs, orders, public, webhooks_dial, webhooks_resend
from app.config import Settings, get_settings
from app.container import Services, build_services
from app.jobs import ensure_daily_insights_jobs
from app.logging_setup import configure_logging

log = logging.getLogger(__name__)

ERROR_CODES = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "too_large",
    415: "unsupported_media_type",
    422: "validation_error",
}

# Five 8 MB files plus multipart framing; anything declared larger is refused before it is read.
UPLOAD_MAX_CONTENT_LENGTH = 45 * 1024 * 1024
_UPLOAD_PATH = re.compile(r"^/v1/chat/sessions/[^/]+/attachments$")


class UploadSizeGuard:
    """Pure ASGI middleware: 413 for upload requests whose Content-Length is over the cap.

    Runs before the body is read, so a client cannot make the server buffer a huge multipart
    form just to be told the files are too big.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = UPLOAD_MAX_CONTENT_LENGTH) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and _UPLOAD_PATH.match(scope["path"])
            and self._declared_length(scope) > self.max_bytes
        ):
            response = JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": ERROR_CODES[413],
                        "message": f"upload is over {self.max_bytes // (1024 * 1024)} MB",
                    }
                },
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _declared_length(scope: Scope) -> int:
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return 0
        return 0


def create_app(services: Services | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or (services.settings if services else get_settings())
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = services or build_services(settings)
        app.state.services = svc
        task: asyncio.Task[None] | None = None
        if settings.scheduler_enabled and settings.env != "test":
            try:
                await ensure_daily_insights_jobs(svc.session_factory, svc.clock.now())
            except Exception:
                log.exception("could not schedule insights jobs")
            task = asyncio.create_task(svc.scheduler.run_forever(settings.scheduler_poll_seconds))
        try:
            yield
        finally:
            if task is not None:
                svc.scheduler.stop()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    log.debug("scheduler task cancelled")
                except Exception:
                    log.exception("scheduler task ended with an error")
            if services is None:
                await svc.engine.dispose()

    app = FastAPI(title="Caseloop", version="0.1.0", lifespan=lifespan)
    # The widget runs on any third-party site, so CORS stays open and credential-free. The
    # dashboard is served from this same origin (or proxied by Vite in dev), so its session cookie
    # never crosses origins and needs no CORS credentials. CORS is added last so it wraps the
    # upload guard and its 413 still carries CORS headers for the widget.
    app.add_middleware(UploadSizeGuard)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": ERROR_CODES.get(exc.status_code, "error"),
                    "message": str(exc.detail),
                }
            },
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(p) for p in e.get("loc", [])),
                "message": e.get("msg"),
                "code": e.get("type"),
            }
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "details": details,
                }
            },
        )

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(public.router)
    app.include_router(auth.router)
    app.include_router(orders.router)
    app.include_router(jobs.router)
    app.include_router(chat.router)
    app.include_router(webhooks_resend.router)
    app.include_router(webhooks_dial.router)

    if settings.is_dev:
        app.include_router(dev.router)

    repo_root = Path(__file__).resolve().parents[2]
    widget_path = repo_root / "widget" / "widget.js"
    dashboard_dist = repo_root / "dashboard" / "dist"

    @app.get("/widget.js", tags=["meta"], include_in_schema=False)
    async def widget_js() -> FileResponse:
        return FileResponse(
            widget_path,
            media_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=300"},
        )

    if dashboard_dist.is_dir():
        app.mount(
            "/dashboard/assets",
            StaticFiles(directory=dashboard_dist / "assets"),
            name="dashboard-assets",
        )

        @app.get("/dashboard/{path:path}", include_in_schema=False)
        async def dashboard_spa(path: str) -> FileResponse:
            return FileResponse(dashboard_dist / "index.html")

        @app.get("/dashboard", include_in_schema=False)
        async def dashboard_root() -> FileResponse:
            return FileResponse(dashboard_dist / "index.html")

    return app


app = create_app()
