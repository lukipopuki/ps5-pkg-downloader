"""FastAPI application factory and entry point."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import security
from .api.routes import router as api_router
from .config import Settings, load_settings
from .logging_setup import setup_logging
from .service import AppService
from .version import __version__

log = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    setup_logging(settings.log_level, settings.log_format)

    @contextlib.asynccontextmanager
    async def lifespan(application: FastAPI):
        service = AppService(settings)
        application.state.service = service
        await service.start()
        log.info(
            "ps5-patch-downloader started",
            extra={
                "version": __version__,
                "port": settings.port,
                "downloads": str(settings.download_dir),
                "config": str(settings.config_dir),
                "auth": "on" if settings.auth_enabled else "off",
            },
        )
        try:
            yield
        finally:
            log.info("Shutting down, pausing active downloads")
            await service.stop()
            log.info("Shutdown complete")

    application = FastAPI(
        title="PS5 Patch Downloader",
        version=__version__,
        description=(
            "Search PS5 game updates and download them straight from the official "
            "Sony CDN. Game update packages only - never system software."
        ),
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def auth_middleware(request: Request, call_next):
        try:
            security.authorize(request, settings)
        except Exception as exc:  # HTTPException
            detail = getattr(exc, "detail", "unauthorized")
            headers = getattr(exc, "headers", None) or {}
            return JSONResponse({"detail": detail}, status_code=getattr(exc, "status_code", 401), headers=headers)
        return await call_next(request)

    application.include_router(api_router)

    frontend = Path(settings.extra.get("frontend_dir") or FRONTEND_DIR)
    if frontend.is_dir():
        application.mount("/static", StaticFiles(directory=str(frontend)), name="static")

        @application.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(str(frontend / "index.html"))

        @application.get("/favicon.svg", include_in_schema=False)
        async def favicon() -> FileResponse:
            return FileResponse(str(frontend / "favicon.svg"))
    else:  # pragma: no cover - only when the image is built incorrectly
        log.warning("frontend directory %s not found; WebUI disabled", frontend)

    return application


# For `uvicorn --factory app.main:app`; the container uses run() below.
app = create_app


def run() -> None:
    """Console entry point: uvicorn with our own signal handling."""
    import uvicorn

    settings = load_settings()
    setup_logging(settings.log_level, settings.log_format)
    config = uvicorn.Config(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=45,
    )
    server = uvicorn.Server(config)

    async def serve() -> None:
        loop = asyncio.get_running_loop()

        def request_stop() -> None:
            # uvicorn's own handler triggers the lifespan shutdown, which
            # pauses running downloads and persists their progress.
            server.should_exit = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, request_stop)
        await server.serve()

    asyncio.run(serve())


if __name__ == "__main__":  # pragma: no cover
    run()
