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
from app.api.routes import dashboard
from app.api.routes import earring_ecommerce
from app.api.routes import earring_close_up_ears
from app.api.routes import earring_scale_reference
from app.api.routes import earring_professional_shot
from app.api.routes import earring_complementary_shot
from app.api.routes import earring_ugc_style
from app.api.routes import earring_macro_shot
from app.api.routes import earring_stand_shot
from app.api.routes import meta_webhook
from app.api.routes import payment_routes
from app.api.routes.health import router as health_router

# Import Celery task modules so they register with the Celery app
import app.tasks.prompt_tasks  # noqa: F401, E402
from app.config import ENV_FILE, settings
from app.database import MONEY_ONCE_INDEX, ensure_schema_ready, money_once_index_present
from app.middleware.cors import setup_cors
from app.middleware.error_handler import setup_error_handlers
from app.middleware.logging_middleware import setup_logging_middleware
from app.middleware.rate_limit import setup_rate_limit
from app.middleware.public_host_guard import setup_public_host_guard
from app.utils.logger import logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown events."""
    # Startup
    setup_logging()
    logger.bind(category="system").info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    logger.bind(category="system").info(f"Config: environment={settings.ENVIRONMENT} "
        f"env_file={ENV_FILE or 'none'} debug={settings.DEBUG}")

    from app.services.error_tracking import init_error_tracking

    init_error_tracking()

    # Initialize database
    # The schema is owned by Alembic: verify it, never create_all at startup.
    schema = ensure_schema_ready()
    logger.bind(category="system").info(f"Database schema at revision {schema.current}")

    # Payment credits and refunds rely on this index to move money at most
    # once. Without it every claim silently succeeds twice under a race.
    if money_once_index_present() is False:
        message = (
            f"{MONEY_ONCE_INDEX} is missing: payment credits and refunds are NOT "
            "protected against double processing. Run `alembic upgrade head`."
        )
        if settings.IS_PRODUCTION:
            raise RuntimeError(message)
        logger.bind(category="system").error(message)

    # Ensure upload and report directories exist
    settings.UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
    settings.REPORT_PATH.mkdir(parents=True, exist_ok=True)
    logger.bind(category="system").info("Storage directories ready")

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
            logger.bind(category="system").info(f"Storage sweep: removed {cleaned} orphaned upload "
                f"director(ies)")
    except Exception as e:
        logger.bind(category="system").warning(f"Storage sweep skipped: {e}")

    # Paid-order recovery (best-effort, non-fatal): background jobs do not
    # survive a restart, so a paid order left queued/processing longer than
    # the existing stuck threshold is failed and refunded exactly once.
    try:
        from app.api.routes.meta_webhook import STUCK_WHITE_AFTER
        from app.services.meta_whatsapp_service import recover_stuck_paid_orders

        from app.services.scheduler_lease import holds_lease

        # Only the process that owns order recovery refunds stuck orders, so a worker that restarts cannot refund an
        # order that a sibling worker is still generating (ARC-2). The other recovery steps are always safe to run.
        if await holds_lease("order_recovery", 90):
            recovered = await recover_stuck_paid_orders(STUCK_WHITE_AFTER)
            if recovered:
                logger.bind(category="system").warning(f"Recovered {recovered} stuck paid order(s) at startup")
        else:
            logger.bind(category="system").info("Another process owns order recovery; skipping the startup refund pass")
        from app.services.meta_whatsapp_service import (
            FAILED_REFUND_GRACE,
            CHOICE_CLAIM_GRACE,
            recover_unrefunded_failed_orders,
            release_abandoned_choice_claims,
        )

        await recover_unrefunded_failed_orders(FAILED_REFUND_GRACE)
        await release_abandoned_choice_claims(CHOICE_CLAIM_GRACE)
    except Exception as e:
        logger.bind(category="system").warning(f"Stuck paid order recovery skipped: {e}")

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
        logger.bind(category="system").warning(f"Image provider startup diagnosis skipped: {e}")

    # WhatsApp Pay reconcile sweep (best-effort): credits in-chat payments
    # whose webhook was missed. Loop is idle while WHATSAPP_PAY_ENABLED=false.
    import asyncio

    reconcile_task = None
    try:
        from app.services.whatsapp_pay_service import run_reconcile_sweep_forever

        reconcile_task = asyncio.create_task(run_reconcile_sweep_forever())
    except Exception as e:
        logger.bind(category="system").warning(f"WhatsApp Pay reconcile sweep not started: {e}")

    alert_task = None
    try:
        from app.services.alert_service import run_alert_sweep_forever

        alert_task = asyncio.create_task(run_alert_sweep_forever())
    except Exception as e:
        logger.bind(category="system").warning(f"Operations alert sweep not started: {e}")

    link_reconcile_task = None
    try:
        from app.services.razorpay_link_reconcile import run_payment_link_reconcile_forever

        link_reconcile_task = asyncio.create_task(run_payment_link_reconcile_forever())
    except Exception as e:
        logger.bind(category="system").warning(f"Razorpay payment link reconcile sweep not started: {e}")

    outbox_task = None
    try:
        from app.services.outbox import run_outbox_forever

        outbox_task = asyncio.create_task(run_outbox_forever())
    except Exception as e:
        logger.bind(category="system").warning(f"Outbox worker not started: {e}")

    retention_task = None
    try:
        from app.services.data_lifecycle import run_retention_forever

        retention_task = asyncio.create_task(run_retention_forever())
    except Exception as e:
        logger.bind(category="system").warning(f"Retention sweep not started: {e}")

    recovery_task = None
    try:
        from app.api.routes.meta_webhook import STUCK_WHITE_AFTER as _STUCK_AFTER
        from app.services.meta_whatsapp_service import run_recovery_sweep_forever

        recovery_task = asyncio.create_task(run_recovery_sweep_forever(_STUCK_AFTER))
    except Exception as e:
        logger.bind(category="system").warning(f"Paid-order recovery sweep not started: {e}")

    # Everything imported and created so far lives for the whole life of the process. Freezing it keeps
    # Python's garbage collector from re-scanning it on every full collection, which paused the event loop for
    # 50-100 ms at a time under load (standard practice for long-running web servers).
    import gc

    gc.collect()
    gc.freeze()

    yield

    # Shutdown. Order matters: stop the periodic jobs first, let orders the outbox sweep started finish (they still
    # need the worker pools and the provider clients), give the periodic-job leases back, and only then close the
    # pools and clients.
    for sweep in (reconcile_task, alert_task, link_reconcile_task, recovery_task, outbox_task, retention_task):
        if sweep is not None:
            sweep.cancel()
    try:
        from app.services.outbox import wait_for_detached

        await wait_for_detached(100)          # orders the outbox sweep started get time to finish (DEP-2)
    except Exception as e:  # noqa: BLE001
        logger.bind(category="system").warning(f"Waiting for background orders skipped: {e}")
    try:
        from app.services.scheduler_lease import release_leases

        await release_leases()                # a restarted process can take the periodic jobs over at once
    except Exception as e:  # noqa: BLE001
        logger.bind(category="system").warning(f"Lease release skipped: {e}")
    try:
        from app.ai.providers.gemini_image_provider import close_gemini_client
        from app.ai.providers.openai_image_provider import close_openai_client

        await close_gemini_client()
        await close_openai_client()
    except Exception as e:  # noqa: BLE001 -- shutdown must never fail on this
        logger.bind(category="system").warning(f"Provider client close skipped: {e}")
    try:
        from app.utils.executors import shutdown_executors

        shutdown_executors()
    except Exception as e:  # noqa: BLE001
        logger.bind(category="system").warning(f"Worker pool shutdown skipped: {e}")
    logger.bind(category="system").info(f"Shutting down {settings.APP_NAME}")


# Create FastAPI application
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=settings.APP_DESCRIPTION,
    lifespan=lifespan,
    # The interactive API documentation is a development convenience; a production server does not advertise it.
    docs_url=None if settings.IS_PRODUCTION else "/docs",
    redoc_url=None if settings.IS_PRODUCTION else "/redoc",
    openapi_url=None if settings.IS_PRODUCTION else "/openapi.json",
)

# Setup middleware
setup_cors(app)
setup_logging_middleware(app)
setup_rate_limit(app)
setup_error_handlers(app)
setup_public_host_guard(app)

# Include routers
app.include_router(health_router)
app.include_router(auth.router)
app.include_router(dashboard.router)
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
app.include_router(earring_stand_shot.router)
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
    info = {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "description": settings.APP_DESCRIPTION,
        "health": "/health",
    }
    if not settings.IS_PRODUCTION:               # the API docs are switched off in production
        info["docs"] = "/docs"
        info["redoc"] = "/redoc"
    return info


# Entry point for running directly
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEV_RELOAD,
        log_level=settings.LOG_LEVEL.lower(),
    )
