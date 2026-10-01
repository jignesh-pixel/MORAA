"""Application configuration management using Pydantic v2 Settings."""

import logging
import os
import secrets
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_config_logger = logging.getLogger(__name__)

# backend/ -- .env and logs resolve against this folder, never against the
# process working directory (a start from another folder used to silently
# skip .env and run on unsafe defaults).
BASE_DIR = Path(__file__).resolve().parent.parent

# Images in a WhatsApp Catalog Pack (CATALOG_PACK_STYLES in
# meta_whatsapp_service.py); a daily cap below this cannot serve one Pack.
_PACK_IMAGE_COUNT = 6


def _resolve_env_file() -> Optional[str]:
    """Absolute path of backend/.env.

    MORAA_ENV_FILE overrides it (a relative value resolves against backend/);
    an empty MORAA_ENV_FILE loads no file at all (tests and the load harness
    use this so live secrets are never read). A named file that does not
    exist is an error, never a silent fall back to defaults.
    """
    override = os.environ.get("MORAA_ENV_FILE")
    if override is not None:
        if not override.strip():
            return None
        path = Path(override)
        if not path.is_absolute():
            path = BASE_DIR / path
        if not path.is_file():
            raise RuntimeError(f"MORAA_ENV_FILE points at a missing file: {path}")
        return str(path)
    default = BASE_DIR / ".env"
    if not default.is_file():
        _config_logger.warning(
            f"No env file at {default}: running on built-in defaults "
            "(ENVIRONMENT=development, SQLite, no webhook secrets)."
        )
    return str(default)


ENV_FILE: Optional[str] = _resolve_env_file()

# Production JWT signing keys shorter than this are refused at boot.
_MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Application settings loaded from environment variables/.env file."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # A refused boot must never echo setting values (secrets) to logs.
        hide_input_in_errors=True,
    )

    # Application
    APP_NAME: str = "MORAA GemVision"
    APP_VERSION: str = "1.0.0"
    APP_DESCRIPTION: str = "AI-Powered Jewellery Image Analysis Platform"
    # "production" refuses to boot on unsafe config (see _enforce_safe_runtime).
    ENVIRONMENT: Literal["development", "production"] = "development"
    # DEBUG only controls developer conveniences (SQL echo, auto-reload,
    # verbose tracebacks). It never relaxes a security check.
    DEBUG: bool = False
    # Development-only escape hatch: accept Meta/Razorpay webhooks that carry
    # no signature when the matching secret is unset. Refused in production.
    ALLOW_UNSIGNED_WEBHOOKS: bool = False

    # Server
    HOST: str = "127.0.0.1"
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
    # The public webhook paths get their own, much larger per-IP budget:
    # Meta and Razorpay send from few IPs (several status callbacks per
    # message), so the dashboard limit would drop real traffic, but an
    # unlimited public endpoint invites floods and verify-token guessing.
    WEBHOOK_RATE_LIMIT_REQUESTS: int = 3000
    # Largest webhook body accepted from a public client (Meta/Razorpay send
    # small JSON; media arrive by id, not inline).
    MAX_WEBHOOK_BODY_BYTES: int = 1_000_000

    # --- Public host guard ---
    # Requests arriving through a public tunnel/host (e.g. ngrok) may only
    # reach the two provider webhooks; everything else is local-only.
    PUBLIC_HOST_GUARD_ENABLED: bool = True
    # Hosts treated as local (comma-separated). Add a LAN IP here if the
    # dashboard is opened from another machine on the network.
    LOCAL_API_HOSTS: str = "localhost,127.0.0.1,::1,0.0.0.0"
    # Extra socket peer addresses treated as this machine (loopback is always
    # local). A request is local only when its real peer qualifies -- the Host
    # header alone is never trusted (anyone can send "Host: localhost"). Add
    # a LAN IP here, alongside LOCAL_API_HOSTS, to open the dashboard from
    # another machine.
    LOCAL_PEER_ADDRESSES: str = "127.0.0.1,::1"

    # --- Auth ---
    # Public self-signup is off by default; set ALLOW_SIGNUP=true to enable.
    ALLOW_SIGNUP: bool = False

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
    # WhatsApp Flow used for new-customer registration (Name / Brand /
    # Address / GSTIN). Leave META_REGISTRATION_FLOW_ID empty to keep the
    # plain-text "Quick Setup" registration message. The screen is the ID of
    # the Flow's first screen (defined in the Flow JSON, not by Meta).
    # META_REGISTRATION_FLOW_MODE: "draft" (default, unpublished Flow) or
    # "published" once the Flow is published in WhatsApp Manager.
    META_REGISTRATION_FLOW_ID: str = ""
    META_REGISTRATION_FLOW_SCREEN: str = "REGISTRATION"
    META_REGISTRATION_FLOW_MODE: str = "draft"

    # --- Studioo Ops (internal team channel) ---
    # OFF by default. When True, messages from OPS_TEAM numbers that look like
    # ops commands are forwarded to OPS_INBOUND_URL (Next.js /api/ops/inbound)
    # in the background and skip the customer pipeline. Everyone else is
    # unaffected. See app/services/ops_forward.py.
    OPS_ENABLED: bool = False
    OPS_TEAM: str = ""  # JSON: {"name": "91XXXXXXXXXX", ...}
    OPS_SECRET: str = ""  # shared with the Next.js app (x-ops-secret header)
    OPS_INBOUND_URL: str = ""

    # --- WhatsApp Pay (native in-chat order_details, India) ---
    # OFF by default: while False nothing below is used and every recharge
    # prompt behaves exactly as before (Razorpay payment link CTA / text).
    # When True, recharge prompts send a native order_details message using
    # the Razorpay payment configuration named WHATSAPP_PAY_CONFIGURATION_NAME
    # (must match the name created in WhatsApp Manager exactly, max 60 chars);
    # any dispatch failure falls back to the Razorpay link CTA unless
    # WHATSAPP_PAY_STRICT is on.
    WHATSAPP_PAY_ENABLED: bool = False
    WHATSAPP_PAY_CONFIGURATION_NAME: str = ""
    # Comma-separated WhatsApp numbers for a pilot; empty = everyone.
    WHATSAPP_PAY_ALLOWLIST: str = ""
    WHATSAPP_PAY_GOODS_TYPE: str = "digital-goods"
    WHATSAPP_PAY_ITEM_NAME: str = "Moraa Studio Wallet Recharge"
    WHATSAPP_PAY_ORDER_EXPIRY_SECONDS: int = 900  # Meta minimum is 300
    # Tax line (required by Meta). Default 0 = price is the full amount;
    # confirm the GST treatment with your accountant before going live.
    WHATSAPP_PAY_TAX_PERCENT: int = 0
    WHATSAPP_PAY_TAX_DESCRIPTION: str = "Inclusive of taxes"
    # Required by Meta when no catalog_id is used (per order item).
    WHATSAPP_PAY_COUNTRY_OF_ORIGIN: str = "IN"
    WHATSAPP_PAY_IMPORTER_NAME: str = "MORAA STUDIO"
    WHATSAPP_PAY_IMPORTER_ADDRESS_LINE1: str = ""
    WHATSAPP_PAY_IMPORTER_ADDRESS_LINE2: str = ""
    WHATSAPP_PAY_IMPORTER_CITY: str = ""
    WHATSAPP_PAY_IMPORTER_ZONE_CODE: str = ""  # state code, e.g. "MH", "GJ"
    WHATSAPP_PAY_IMPORTER_POSTAL_CODE: str = ""
    # Strict native mode: never send a Razorpay link / URL button for a wallet
    # recharge. If native order_details cannot be sent, the customer gets an
    # in-chat "temporarily unavailable" text and an ALERT is logged.
    # Turn on ONLY after native pay is live and tested; before that it means
    # nobody can recharge.
    WHATSAPP_PAY_STRICT: bool = False
    # Background sweep for orders whose payment webhook was missed (seconds;
    # 0 disables). Runs only while WHATSAPP_PAY_ENABLED is true.
    WHATSAPP_PAY_RECONCILE_INTERVAL_SECONDS: int = 300

    # --- Live GSTIN verification during onboarding ---
    # False (default) = onboarding behaves exactly as before (format check
    # only, no lookup, no GST buttons). GST_PROVIDER: "none" (default, every
    # lookup reports unavailable) or "mock" (local testing, DEBUG only);
    # real vendors are added in app/services/gst_service.py.
    GST_VERIFICATION_ENABLED: bool = False
    GST_PROVIDER: str = "none"
    GST_API_TIMEOUT_SECONDS: float = 8.0

    # --- Earring e-commerce prompt version ---
    # "v1" (default) = the original live prompt, byte-for-byte unchanged.
    # "v2" = de-duplicated prompt with the same rules (opt-in, A/B first).
    EARRING_PROMPT_VERSION: str = "v1"

    # --- ERPNext billing (Frappe Cloud) ---
    # Read by app/services/erpnext_service.py (with os.getenv fallback).
    # While ERPNEXT_INVOICE_ENABLED is False the local ReportLab receipt is
    # sent exactly as before; any ERPNext error/timeout also falls back to it.
    ERPNEXT_BASE_URL: str = ""
    ERPNEXT_API_KEY: str = ""
    ERPNEXT_API_SECRET: str = ""
    ERPNEXT_COMPANY: str = ""
    ERPNEXT_DEFAULT_DEBTORS_ACCOUNT: str = ""
    ERPNEXT_PAYMENT_ACCOUNT: str = ""
    ERPNEXT_INVOICE_ENABLED: bool = False
    ERPNEXT_RECHARGE_ITEM_CODE: str = ""
    ERPNEXT_TAX_TEMPLATE: str = ""
    ERPNEXT_MODE_OF_PAYMENT: str = ""
    ERPNEXT_PRINT_FORMAT: str = "Standard"
    ERPNEXT_PRICES_INCLUDE_TAX: bool = True
    ERPNEXT_TIMEOUT_SECONDS: float = 8.0
    # Upper bound for the whole background invoice job (all ERPNext calls).
    ERPNEXT_JOB_TIMEOUT_SECONDS: float = 60.0

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

    # White Background E-Commerce Image (1 image, pure #FFFFFF) — selected by
    # a WhatsApp image caption of exactly "white" / "plain white". Separate
    # from WALLET_IMAGE_PRICE_RUPEES, which stays the E-Com Pack 1 price.
    WHITE_BG_PRICE_RUPEES: int = 50

    # Trial credits consumed per order (tier = TRIAL). A credit is one
    # complimentary order; the owner sets trial_credits_total per customer.
    TRIAL_CREDITS_PER_WHITE_BG: int = 1   # ₹50 Clean Studio Shot
    TRIAL_CREDITS_PER_PACK_1: int = 1     # ₹500 E-Com Pack 1 (6 images)

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
    # None = all styles (production: the full 6-shot E-Com Pack 1).
    # Set a number (e.g. 1) only as a temporary testing throttle.
    MAX_STYLES_PER_PACK: Optional[int] = None

    # --- AI image pre-validation (Scenario 4) ---
    # Fast Gemini quality inspection of a funded image before generation.
    IMAGE_PREVALIDATION_ENABLED: bool = True
    # gemini-2.5-flash now returns 404 "no longer available to new users";
    # Google's error names gemini-3.6-flash as the replacement.
    IMAGE_PREVALIDATION_MODEL: str = "gemini-3.6-flash"
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
    # Relative values resolve against backend/ (see LOG_PATH).
    LOG_DIR: str = "logs"
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
    def _enforce_safe_runtime(self) -> "Settings":
        """Refuse to boot in production on unsafe config; warn in development.

        Runs before the SECRET_KEY fallback below so a missing production key
        is an error, not a silently generated one.
        """
        if self.ENVIRONMENT == "production":
            problems: List[str] = []
            secret_key = (self.SECRET_KEY or "").strip()
            if not secret_key:
                problems.append("SECRET_KEY is not set")
            elif len(secret_key) < _MIN_SECRET_KEY_LENGTH:
                problems.append(
                    f"SECRET_KEY is shorter than {_MIN_SECRET_KEY_LENGTH} characters"
                )
            if not self.META_APP_SECRET.strip():
                problems.append("META_APP_SECRET is not set")
            if not self.RAZORPAY_WEBHOOK_SECRET.strip():
                problems.append("RAZORPAY_WEBHOOK_SECRET is not set")
            if self.IS_SQLITE:
                problems.append("DATABASE_URL points at SQLite")
            if self.DEBUG:
                problems.append("DEBUG is true")
            if self.ALLOW_UNSIGNED_WEBHOOKS:
                problems.append("ALLOW_UNSIGNED_WEBHOOKS is true")
            if problems:
                raise ValueError(
                    "Refusing to start with ENVIRONMENT=production: "
                    + "; ".join(problems)
                )
        elif self.ALLOW_UNSIGNED_WEBHOOKS:
            _config_logger.warning(
                "ALLOW_UNSIGNED_WEBHOOKS is on: webhooks without a signature "
                "are accepted when their secret is unset. Development only."
            )

        if self.MAX_GENERATIONS_PER_DAY < _PACK_IMAGE_COUNT:
            _config_logger.warning(
                f"MAX_GENERATIONS_PER_DAY={self.MAX_GENERATIONS_PER_DAY} is "
                f"below one Pack ({_PACK_IMAGE_COUNT} images): Pack orders "
                "will be refused once charged."
            )

        if Path.cwd().resolve() != BASE_DIR:
            relative_db = self.IS_SQLITE and "///./" in self.DATABASE_URL
            _config_logger.warning(
                f"Working directory is {Path.cwd()}, not {BASE_DIR}. Uploads "
                f"and reports use paths relative to the working directory "
                f"({self.UPLOAD_DIR}, {self.REPORT_DIR}"
                f"{', and the SQLite database' if relative_db else ''}); start "
                "the server from backend/ so stored file paths keep resolving."
            )
        return self

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

    @property
    def LOG_PATH(self) -> Path:
        """Log directory; a relative LOG_DIR resolves against backend/."""
        path = Path(self.LOG_DIR)
        return path if path.is_absolute() else BASE_DIR / path

    @property
    def IS_PRODUCTION(self) -> bool:
        """True when ENVIRONMENT=production."""
        return self.ENVIRONMENT == "production"


settings = Settings()
