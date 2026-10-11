from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, chat, dev, orders, public, webhooks_dial, webhooks_resend
from app.config import Settings, get_settings
from app.container import Services, build_services
from app.jobs import ensure_daily_insights_jobs

log = logging.getLogger(__name__)


def create_app(services: Services | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or (services.settings if services else get_settings())

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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.base_url, "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        code = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            422: "validation_error",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {"code": code.get(exc.status_code, "error"), "message": str(exc.detail)}
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
            media_type="application/javascript",
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
