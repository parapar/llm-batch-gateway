"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from batchsvc.config import Settings, load_settings
from batchsvc.db import build_database
from batchsvc.ledger import InsufficientBudgetError
from batchsvc.routers import admin, misc


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Batch Inference Service", version="0.1.0")
    app.state.settings = settings
    app.state.db = build_database(settings)

    app.include_router(admin.router)
    app.include_router(misc.router)

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
