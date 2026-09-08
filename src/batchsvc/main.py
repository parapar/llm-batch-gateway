"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from batchsvc.config import Settings, load_settings
from batchsvc.db import build_database
from batchsvc.dispatcher import Dispatcher
from batchsvc.ledger import InsufficientBudgetError
from batchsvc.routers import admin, batches, files, misc

logger = logging.getLogger("batchsvc.main")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = build_database(settings)
    # The dispatcher only actually runs (as a background task, below) when
    # nodes are configured -- an API-only deployment, or most test/dev
    # setups, has nothing for it to do and shouldn't pay for a polling loop.
    dispatcher = Dispatcher(db, settings) if settings.nodes else None

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task | None = None
        if dispatcher is not None:
            dispatcher.startup()
            task = asyncio.create_task(dispatcher.run_forever())
            logger.info("dispatcher started with %d configured node(s)", len(settings.nodes))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if dispatcher is not None:
                await dispatcher.aclose()

    app = FastAPI(title="Batch Inference Service", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.dispatcher = dispatcher

    app.include_router(admin.router)
    app.include_router(misc.router)
    app.include_router(files.router)
    app.include_router(batches.router)

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
