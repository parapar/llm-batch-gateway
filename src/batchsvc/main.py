"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from batchsvc.config import Settings, load_settings
from batchsvc.db import build_database
from batchsvc.dispatcher import Dispatcher
from batchsvc.ldap_auth import LdapAuthenticator
from batchsvc.ledger import InsufficientBudgetError
from batchsvc.logging_setup import configure_logging
from batchsvc.portal import routes as portal_routes
from batchsvc.portal.session import SessionCodec
from batchsvc.retention import RetentionJob
from batchsvc.routers import admin, batches, files, metrics, misc
from batchsvc.routers.metrics import HTTP_REQUESTS_TOTAL

logger = logging.getLogger("batchsvc.main")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(json_output=settings.log_json)
    db = build_database(settings)
    # The dispatcher only actually runs (as a background task, below) when
    # nodes are configured -- an API-only deployment, or most test/dev
    # setups, has nothing for it to do and shouldn't pay for a polling loop.
    dispatcher = Dispatcher(db, settings) if settings.nodes else None
    # Unlike the dispatcher, expiry/cleanup make sense even with zero nodes
    # configured, so this always runs.
    retention_job = RetentionJob(db, settings)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        dispatcher_task: asyncio.Task | None = None
        if dispatcher is not None:
            dispatcher.startup()
            dispatcher_task = asyncio.create_task(dispatcher.run_forever())
            logger.info("dispatcher started", extra={"node_count": len(settings.nodes)})
        retention_task = asyncio.create_task(retention_job.run_forever())
        try:
            yield
        finally:
            retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retention_task
            if dispatcher_task is not None:
                dispatcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await dispatcher_task
            if dispatcher is not None:
                await dispatcher.aclose()

    app = FastAPI(title="Batch Inference Service", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.dispatcher = dispatcher
    app.state.ldap = LdapAuthenticator(settings.ldap) if settings.ldap else None
    # A blank secret leaves this None, and the portal routes then serve a
    # clear "not enabled" page -- better than signing session cookies with
    # a placeholder nobody remembered to change.
    app.state.portal_sessions = (
        SessionCodec(
            settings.portal.session_secret,
            lifetime_minutes=settings.portal.session_lifetime_minutes,
        )
        if settings.portal.enabled and settings.portal.session_secret
        else None
    )
    app.state.portal_login_limiter = portal_routes.LoginRateLimiter(
        settings.portal.login_attempts_per_minute
    )

    @app.middleware("http")
    async def log_and_count_requests(request: Request, call_next):  # noqa: ANN001
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        route = request.scope.get("route")
        path_template = route.path if route is not None else request.url.path
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method, path=path_template, status=response.status_code
        ).inc()
        logger.info(
            "request",
            extra={
                "http_method": request.method,
                "http_path": path_template,
                "http_status": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
        return response

    app.include_router(admin.router)
    app.include_router(misc.router)
    app.include_router(files.router)
    app.include_router(batches.router)
    app.include_router(metrics.router)
    app.include_router(portal_routes.router)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request, exc: StarletteHTTPException):  # noqa: ANN001
        # HTTPException.detail is already an OpenAI-shaped {"error": {...}}
        # for our own ApiError subclasses; wrap anything else (e.g. FastAPI's
        # own 404/422) into the same envelope so clients only handle one shape.
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            body = detail
        else:
            body = {
                "error": {
                    "message": str(detail),
                    "type": "invalid_request_error",
                    "param": None,
                    "code": None,
                }
            }
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(InsufficientBudgetError)
    async def insufficient_budget_handler(request, exc: InsufficientBudgetError):  # noqa: ANN001
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "message": (
                        f"This request needs {exc.requested} tokens but only "
                        f"{exc.available} remain in your budget."
                    ),
                    "type": "insufficient_quota_error",
                    "param": None,
                    "code": "insufficient_quota",
                }
            },
        )

    return app


# Run with: uvicorn batchsvc.main:create_app --factory
# (factory form, not a module-level `app`, so importing this module -- e.g.
# from the CLI or from tests -- never has the side effect of touching disk
# with the default config's database/blob paths.)
