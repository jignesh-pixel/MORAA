"""Application configuration management using Pydantic v2 Settings."""

import logging
import os
import secrets
from pathlib import Path
from typing import List, Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_config_logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings loaded from environment variables/.env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    APP_NAME: str = "MORAA GemVision"
    APP_VERSION: str = "1.0.0"
    APP_DESCRIPTION: str = "AI-Powered Jewellery Image Analysis Platform"
    DEBUG: bool = True

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 4

    # Database
    DATABASE_URL: str = "sqlite:///./data/moraa_gemvision.db"

    # JWT Authentication
    # No hardcoded default — set via SECRET_KEY in the environment/.env for
    # production so JWTs stay valid across restarts and multiple workers. If
    # unset, a random per-process key is generated (see _default_secret_key
    # below) so the app never ships a known, guessable secret.
    SECRET_KEY: Optional[str] = None
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # File Uploads
    UPLOAD_DIR: str = "app/uploads"
    REPORT_DIR: str = "app/reports"
    MAX_UPLOAD_SIZE_MB: int = 10
    ALLOWED_EXTENSIONS: str = "jpg,jpeg,png,webp"

    # CORS
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_REQUESTS: int = 100
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    # --- AI Engine ---
    # Options: "mock" (random), "vision" (PIL-based real analysis).
    # Add new engines in app/ai/engine_factory.py
    AI_ENGINE_TYPE: str = "vision"

    # --- AI Provider Manager ---
    # Primary provider for image analysis.
    # Options: "gemini", "openai", "claude", "local_vision"
    PRIMARY_AI_PROVIDER: str = "gemini"

    # Backup provider if primary fails.
    BACKUP_AI_PROVIDER: str = "openai"

    # Fallback provider if backup also fails.
    FALLBACK_AI_PROVIDER: str = "local_vision"

    # --- API Keys for AI Providers ---
    GEMINI_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""

    # Google Gemini (Primary AI)
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # --- Model Settings ---
    # Model name for OpenAI Vision API (e.g. "gpt-4o", "gpt-4o-mini")
    OPENAI_MODEL: str = "gpt-4o-mini"

    # --- Image Generation ---

    # Primary provider for image generation.
    PRIMARY_IMAGE_PROVIDER: str = "gemini"

    # Fallback provider for image generation if primary fails.
    # Options: "gemini", "openai"
    FALLBACK_IMAGE_PROVIDER: str = "openai"

    # Model name for Gemini image generation. Default is the current
    # generation image model ("gemini-3.1-flash-image" / Nano Banana 2),
    # which delivers significantly better reference-image fidelity than the
    # deprecated "gemini-2.5-flash-image" (Nano Banana v1). Must support
    # image output via generate_content with response_modalities=["IMAGE"].
    GEMINI_IMAGE_MODEL: str = "gemini-2.5-flash-image"

    # Model name for OpenAI image generation. Default "gpt-image-1" is the
    # ChatGPT image model — it supports reference-image editing
    # (/v1/images/edits) which is required for the PRIMARY path to preserve
    # the uploaded product. "dall-e-3" remains available via env override
    # (text-to-image only, reference image ignored).
    OPENAI_IMAGE_MODEL: str = "gpt-image-1"

    # Operator-facing startup probe: when True, the backend verifies once at
    # boot (once per process) that the configured Gemini image model is
    # actually accessible and logs an actionable diagnosis when it is not
    # (see app/services/gemini_diagnostics.py). Never blocks startup and
    # stays off by default so tests/dev never issue billable probes.
    IMAGE_PROVIDER_STARTUP_DIAGNOSTICS: bool = False

    # --- Prompt Fusion Intelligence Engine (PFIE) — DISABLED ---
    # Prompt Fusion is no longer used: the final image-generation prompt
    # comes ONLY from the Gemini-driven structured prompt generation
    # (analysis → category prompt), never from a merged/hybrid prompt.
    # The /api/fuse-prompt endpoint remains registered for backward
    # compatibility, but it no longer calls the ChatGPT creative director
    # and the frontend never invokes it.
    PFIE_ENABLED: bool = False

    # ChatGPT model used as the Luxury Jewellery Creative Director.
    PFIE_CREATIVE_MODEL: str = "gpt-4o-mini"

    # Sampling temperature for the creative director (0-1; higher = more varied).
    PFIE_CREATIVE_TEMPERATURE: float = 0.8

    # Jewellery Preservation & Scale Control (JSR enhancement).
    # When enabled, EVERY fused prompt includes the jewellery scale-control
    # block (preserve size/proportions, fit human anatomy) and category-based
    # fitting rules (ear-to-earring ratio, finger proportions, wrist proportion,
    # collarbone placement). This toggle only controls the scale-control block
    # and category fitting rules; the strengthened lifestyle removals (no cards,
    # labels, text, packaging, hands holding product) are applied independently.
    PFIE_SCALE_CONTROL_ENABLED: bool = True

    # --- Meta WhatsApp Cloud API ---
    # Webhook verification token (set in Meta developer dashboard)
    META_VERIFY_TOKEN: str = ""
    # Permanent access token for the WhatsApp Business account
    META_WHATSAPP_TOKEN: str = ""
    # Phone number ID from the Meta Business account
    META_PHONE_NUMBER_ID: str = ""
    # App secret for webhook signature validation (X-Hub-Signature-256)
    META_APP_SECRET: str = ""
    # Maximum WhatsApp image download size (bytes) — 5 MB safety limit
    META_MAX_MEDIA_BYTES: int = 5 * 1024 * 1024

    # --- Razorpay Payments (wallet recharge) ---
    # API credentials used to create dynamic recharge payment links.
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    # Webhook signing secret from the Razorpay dashboard. Verifies the
    # X-Razorpay-Signature header (HMAC-SHA256) on every inbound webhook.
    # When empty the webhook endpoint fails CLOSED and rejects all requests,
    # so an unconfigured deployment can never credit a wallet.
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # --- WhatsApp Onboarding Gate (new customer onboarding) ---
    # When False (DEFAULT) the WhatsApp pipeline behaves EXACTLY as before:
    # inbound text messages are logged and ignored, and the image →
    # style-selection → generation flow is untouched.
    # When True, text messages from unregistered WhatsApp users are routed
    # through app/services/onboarding_service.py (welcome → registration →
    # recharge CTA). Registered users always bypass onboarding.
    ENABLE_ONBOARDING_GATE: bool = False

    # Gemini text model used ONLY to extract structured registration fields
    # from free-form WhatsApp registration messages. This parser never
    # generates conversational replies — it returns strict JSON only.
    ONBOARDING_PARSER_MODEL: str = "gemini-1.5-flash"

    # Cost safety net for the onboarding parser above (audit: paid Gemini
    # call per registration message, no cap). Only matters once
    # ENABLE_ONBOARDING_GATE is turned on -- defaults are generous so
    # nothing changes for existing behavior until someone lowers them.
    ONBOARDING_PARSER_MAX_CALLS_PER_DAY: int = 500
    ONBOARDING_PARSER_QUOTA_COOLDOWN_SECONDS: int = 300

    # --- Wallet gate (Scenarios 2, 3 and 4) ---
    # The wallet-balance gate in app/api/routes/meta_webhook.py is always
    # active (there is no toggle for it) — every inbound image is checked
    # against the customer's wallet balance before it is processed. A
    # previous ENABLE_WALLET_GATE flag was removed here because it gated
    # nothing (the live webhook route never read it) and its presence
    # falsely implied wallet checks could be switched off.

    # Price charged per generated image, in whole Indian Rupees.
    WALLET_IMAGE_PRICE_RUPEES: int = 500

    # Razorpay (or any PSP) payment-page URL used by the "Pay ₹<price>" CTA
    # URL button. Leave empty to fall back to the interactive reply button
    # ('recharge_500' / "💳 Recharge to use") that the onboarding flow already
    # uses. No payment gateway SDK or credential is required by this code —
    # the button only opens the PSP-hosted page.
    RECHARGE_PAYMENT_URL: str = "https://rzp.io/rzp/FbuLh9je"

    # Zero-cost WhatsApp test mode. Must be a Settings field: pydantic-settings
    # reads backend/.env into this object only -- it never exports .env into
    # os.environ, so the old os.getenv() read silently ignored .env.
    DRY_RUN_IMAGE_MODE: bool = False

    # --- Generation spend guard (app/ai/image_generation_manager.py) ---
    # Hard kill switch: set False to pause all image generation immediately.
    GENERATION_ENABLED: bool = True
    # Daily ceiling on ImageGenerationManager.generate_image() calls.
    MAX_GENERATIONS_PER_DAY: int = 100000

    # Styles generated per WhatsApp Earring Catalog Pack (6 styles exist).
    # 1 = current throttled behaviour; empty/None = all styles.
    MAX_STYLES_PER_PACK: Optional[int] = 1

    # --- AI image pre-validation (Scenario 4) ---
    # Fast Gemini quality inspection of a funded image before generation.
    IMAGE_PREVALIDATION_ENABLED: bool = True
    IMAGE_PREVALIDATION_MODEL: str = "gemini-2.5-flash"
    # When the inspector cannot run (no API key, outage, unparseable reply):
    # True  -> allow the image through (never block a paying customer)
    # False -> reject the image and ask the customer to resend
    IMAGE_PREVALIDATION_FAIL_OPEN: bool = True

    # --- Image Preprocessing ---
    # Max dimension (pixels) for image resizing before AI analysis
    PREPROCESS_MAX_DIMENSION: int = 2048
    # JPEG compression quality (0-100) for image compression
    PREPROCESS_COMPRESSION_QUALITY: int = 85

    # Logging
    LOG_LEVEL: str = "DEBUG"
    LOG_FORMAT: str = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    # --- Celery / Task Queue ---
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"

    # When True, tasks run synchronously (no broker needed) — ideal for dev.
    # Set to False in production when a real Redis/RabbitMQ broker is available.
    CELERY_TASK_ALWAYS_EAGER: bool = True

    # Name of the Celery task queue for analysis jobs
    CELERY_ANALYSIS_QUEUE: str = "analysis"

    # Number of Celery worker processes (only used when not eager)
    CELERY_WORKER_CONCURRENCY: int = 2

    @model_validator(mode="after")
    def _default_secret_key(self) -> "Settings":
        """Generate a random per-process SECRET_KEY when none is configured.

        Never falls back to a hardcoded/known value. A generated key means
        JWTs won't survive a restart or be shared across multiple workers —
        set SECRET_KEY explicitly in production to avoid that.
        """
        if not self.SECRET_KEY:
            self.SECRET_KEY = secrets.token_urlsafe(32)
            _config_logger.warning(
                "SECRET_KEY is not set — generated a random per-process key. "
                "Set SECRET_KEY in the environment for production so JWTs "
                "remain valid across restarts and multiple workers."
            )
        return self

    @property
    def ALLOWED_EXTENSIONS_LIST(self) -> List[str]:
        """Return allowed extensions as a list."""
        return [ext.strip().lower() for ext in self.ALLOWED_EXTENSIONS.split(",")]

    @property
    def MAX_UPLOAD_SIZE_BYTES(self) -> int:
        """Return max upload size in bytes."""
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @property
    def CORS_ORIGINS_LIST(self) -> List[str]:
        """Return CORS origins as a list."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

    @property
    def IS_SQLITE(self) -> bool:
        """Check if using SQLite database."""
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def UPLOAD_PATH(self) -> Path:
        """Return upload directory as Path."""
        return Path(self.UPLOAD_DIR)

    @property
    def REPORT_PATH(self) -> Path:
        """Return report directory as Path."""
        return Path(self.REPORT_DIR)


settings = Settings()
