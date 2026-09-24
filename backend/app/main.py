"""MORAA GemVision - Main FastAPI Application Entry Point."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import auth, upload, analysis, history, reports, tracking
from app.api.routes import batch_upload
from app.api.routes import prompts
from app.api.routes import image_generation
from app.api.routes import prompt_fusion
from app.api.routes import earring_ecommerce
from app.api.routes import earring_close_up_ears
from app.api.routes import earring_scale_reference
from app.api.routes import earring_professional_shot
from app.api.routes import earring_complementary_shot
from app.api.routes import earring_ugc_style
from app.api.routes import earring_macro_shot
from app.api.routes import meta_webhook
from app.api.routes import payment_routes
from app.api.routes.health import router as health_router

# Import Celery task modules so they register with the Celery app
import app.tasks.prompt_tasks  # noqa: F401, E402
from app.config import settings
from app.database import init_db
from app.middleware.cors import setup_cors
from app.middleware.error_handler import setup_error_handlers
from app.middleware.logging_middleware import setup_logging_middleware
from app.middleware.rate_limit import setup_rate_limit
from app.utils.logger import logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown events."""
    # Startup
    setup_logging()
    logger.info(
        f"Starting {settings.APP_NAME} v{settings.APP_VERSION}",
        extra={"category": "system"},
    )

    # Initialize database
    init_db()
    logger.info("Database initialized", extra={"category": "system"})

    # Ensure upload and report directories exist
    settings.UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
    settings.REPORT_PATH.mkdir(parents=True, exist_ok=True)
    logger.info("Storage directories ready", extra={"category": "system"})

    # Storage hygiene — sweep upload directories that have no matching
    # Image record (crash/aborted-upload leftovers) so unused files do not
    # accumulate. Directories referenced by the database are permanent user
    # data and are never touched.
    try:
        from app.database import SessionLocal
        from app.models import Image
        from app.utils.file_helpers import cleanup_orphaned_directories

        with SessionLocal() as db:
            known_ids = {
                row[0] for row in db.query(Image.request_id).all()
            }
        cleaned = cleanup_orphaned_directories(
            settings.UPLOAD_PATH, known_ids
        )
        if cleaned:
            logger.info(
                f"Storage sweep: removed {cleaned} orphaned upload "
                f"director(ies)",
                extra={"category": "system"},
            )
    except Exception as e:
        logger.warning(
            f"Storage sweep skipped: {e}", extra={"category": "system"}
        )

    # Image provider startup diagnosis (best-effort, non-fatal). Probes the
    # configured Gemini image model once at boot and logs an actionable cause
    # when access is broken (invalid model name, disabled API, quota
    # exhaustion, key restrictions) instead of failing every request.
    try:
        from app.services.gemini_diagnostics import (
            log_startup_image_provider_diagnosis,
        )

        await log_startup_image_provider_diagnosis()
    except Exception as e:
        logger.warning(
            f"Image provider startup diagnosis skipped: {e}",
            extra={"category": "system"},
        )

    yield

    # Shutdown
    logger.info(
        f"Shutting down {settings.APP_NAME}",
        extra={"category": "system"},
    )


# Create FastAPI application
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=settings.APP_DESCRIPTION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# Setup middleware
setup_cors(app)
setup_logging_middleware(app)
setup_rate_limit(app)
setup_error_handlers(app)

# Include routers
app.include_router(health_router)
app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(analysis.router)
app.include_router(history.router)
app.include_router(reports.router)
app.include_router(tracking.router)
app.include_router(batch_upload.router)
app.include_router(prompts.router)
app.include_router(image_generation.router)
app.include_router(prompt_fusion.router)
app.include_router(earring_ecommerce.router)
app.include_router(earring_close_up_ears.router)
app.include_router(earring_scale_reference.router)
app.include_router(earring_professional_shot.router)
app.include_router(earring_complementary_shot.router)
app.include_router(earring_ugc_style.router)
app.include_router(earring_macro_shot.router)
app.include_router(meta_webhook.router)
app.include_router(payment_routes.router)

# Serve uploaded files statically. The directory is created here at import
# time so a cold server reboot can never silently skip the mount (the
# lifespan startup hook runs only after module import completes).
uploads_path = settings.UPLOAD_PATH
uploads_path.mkdir(parents=True, exist_ok=True)
app.mount(
    "/uploads",
    StaticFiles(directory=str(uploads_path)),
    name="uploads",
)


@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "description": settings.APP_DESCRIPTION,
        "docs": "/docs",
        "redoc": "/redoc",
        "health": "/health",
    }


# Entry point for running directly
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )
