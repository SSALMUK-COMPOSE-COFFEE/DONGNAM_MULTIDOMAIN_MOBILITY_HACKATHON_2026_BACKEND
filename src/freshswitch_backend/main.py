from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from freshswitch_backend.api import (
    analysis,
    compare,
    demo,
    health,
    intake,
    plans,
    scenarios,
    settlement,
    vessels,
)
from freshswitch_backend.config import get_settings
from freshswitch_backend.replan import ReplanError


async def _replan_error(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def create_app() -> FastAPI:
    app = FastAPI(
        title="FreshSwitch API",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    api = APIRouter(prefix="/api")
    api.include_router(health.router)
    api.include_router(scenarios.router)
    api.include_router(plans.router)
    api.include_router(vessels.router)
    api.include_router(compare.router)
    api.include_router(demo.router)
    api.include_router(analysis.router)
    api.include_router(intake.router)
    api.include_router(settlement.router)
    app.include_router(api)
    app.add_exception_handler(ReplanError, _replan_error)
    return app


app = create_app()
