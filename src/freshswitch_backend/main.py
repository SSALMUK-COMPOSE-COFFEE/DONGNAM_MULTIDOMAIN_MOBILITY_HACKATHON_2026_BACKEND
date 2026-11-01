from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from freshswitch_backend.api.router import api_router
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
    app.include_router(api_router)
    app.add_exception_handler(ReplanError, _replan_error)
    return app


app = create_app()
