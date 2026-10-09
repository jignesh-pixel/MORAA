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

# Images in a WhatsApp Catalog Pack: one per style in meta_whatsapp_service.CATALOG_PACK_STYLES
# (= pack_generation_count()); a daily cap below this cannot serve one Pack.
# Kept equal to len(CATALOG_PACK_STYLES) by tests/test_earring_stand_shot.py.
_PACK_IMAGE_COUNT = 7


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
_BUILTIN_RECHARGE_PAYMENT_URL = "https://rzp.io/rzp/FbuLh9je"
_MIN_SECRET_KEY_LENGTH = 32
_MIN_SECRET_KEY_DISTINCT_CHARS = 10


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
    # DEBUG only controls verbose tracebacks in logs. It never relaxes a security check.
    DEBUG: bool = False
    # Separate, explicit developer switches (they used to ride on DEBUG, so one flag changed three unrelated
    # things): print every SQL statement, and restart the server when code changes.
    SQL_ECHO: bool = False
    DEV_RELOAD: bool = False
    # Development-only escape hatch: accept Meta/Razorpay webhooks that carry
    # no signature when the matching secret is unset. Refused in production.
    ALLOW_UNSIGNED_WEBHOOKS: bool = False

    # Server
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    # (WORKERS was removed: nothing read it, so a value in .env gave a false sense of running several workers.
    # The number of worker processes is set where the server is started, e.g. WEB_CONCURRENCY with gunicorn.)

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
    # Largest webhook body accepted from a public client (3 MB; Meta/Razorpay send
    # small JSON; media arrive by id, not inline).
    MAX_WEBHOOK_BODY_BYTES: int = 3_000_000

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
    # (GEMINI_API_KEY used to be declared twice, the second time from os.getenv; one declaration is enough:
    # pydantic reads it from the environment / .env itself.)
    GEMINI_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""

    # Google Gemini (Primary AI). The previous default, gemini-2.5-flash, answers 404 "no longer available to
    # new users"; Google's own error names gemini-3.6-flash as the replacement.
    GEMINI_MODEL: str = "gemini-3.6-flash"

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
    GEMINI_IMAGE_MODEL: str = "gemini-3.1-flash-image"

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
    # When True a team message is an ops command only if it starts with the word "ops " ("ops start", "ops help"); the
    # word is removed before the message is forwarded. Plain "start" / "help" from a team number then reach the customer
    # flow like any other message (EXT-8). Off by default so the team's current habits keep working.
    OPS_EXPLICIT_PREFIX: bool = False

    # The photo download link Meta returns must point at Meta's own hosts before the access token is sent to it (SEC-11).
    META_MEDIA_HOST_CHECK: bool = True
    META_MEDIA_HOST_SUFFIXES: str = ".facebook.com,.fbsbx.com,.fbcdn.net,.whatsapp.net,.whatsapp.com"

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
    # Seconds between sweeps that ask Razorpay about recharge links whose payment webhook never arrived
    # (0 disables). Runs only while the Razorpay API keys are set.
    RAZORPAY_LINK_RECONCILE_INTERVAL_SECONDS: int = 300

    # --- Live GSTIN verification during onboarding ---
    # False (default) = onboarding behaves exactly as before (format check
    # only, no lookup, no GST buttons). GST_PROVIDER: "none" (default, every
    # lookup reports unavailable) or "mock" (local testing, DEBUG only);
    # real vendors are added in app/services/gst_service.py.
    GST_VERIFICATION_ENABLED: bool = False
    GST_PROVIDER: str = "none"
    GST_API_TIMEOUT_SECONDS: float = 8.0
    GST_API_URL: str = ""                     # GST_PROVIDER=http: https://vendor/.../{gstin}
    GST_API_KEY: str = ""
    GST_API_KEY_HEADER: str = "x-api-key"

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
    # Sales to a buyer in another state are IGST, not CGST+SGST (PRIV-4). Set the seller's two-digit GST state code
    # and the ERPNext tax template that carries IGST; until both are set every invoice uses ERPNEXT_TAX_TEMPLATE.
    ERPNEXT_COMPANY_STATE_CODE: str = ""
    ERPNEXT_TAX_TEMPLATE_INTERSTATE: str = ""
    ERPNEXT_MODE_OF_PAYMENT: str = ""
    ERPNEXT_PRINT_FORMAT: str = "Standard"
    ERPNEXT_PRICES_INCLUDE_TAX: bool = True
    # Upper bound for the whole background invoice job (all ERPNext calls).
    ERPNEXT_JOB_TIMEOUT_SECONDS: float = 60.0
    # Cap on every single ERPNext HTTP call (connect, read, write, pool). Short so an unreachable or suspended
    # ERPNext fails fast and the local invoice is sent at once; raise it if PDF downloads time out on a live ERPNext.
    ERPNEXT_HTTP_TIMEOUT_SECONDS: float = 2.0

    # --- Razorpay Payments (wallet recharge) ---
    # API credentials used to create dynamic recharge payment links.
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    # Webhook signing secret from the Razorpay dashboard. Verifies the
    # X-Razorpay-Signature header (HMAC-SHA256) on every inbound webhook.
    # When empty the webhook endpoint fails CLOSED and rejects all requests,
    # so an unconfigured deployment can never credit a wallet.
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # (ENABLE_ONBOARDING_GATE was removed: no code read it. Registration and the welcome flow always run; a
    # switch that appeared to turn them off did nothing.)
    # (ONBOARDING_PARSER_MODEL / _MAX_CALLS_PER_DAY / _QUOTA_COOLDOWN_SECONDS were removed: nothing read them, so
    # they suggested a cost cap on the onboarding parser that did not exist.)

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
    TRIAL_CREDITS_PER_WHITE_BG: int = 1   # Clean Studio Shot
    TRIAL_CREDITS_PER_PACK_1: int = 1     # E-Com Pack 1 (7 images)

    # Wallet recharge limits, in whole rupees (read through app/services/pricing.py).
    MIN_RECHARGE_RUPEES: int = 500
    MAX_RECHARGE_RUPEES: int = 50_000

    # --- Phase 8: SKU packs (app/services/pricing.py, app/services/sku_packs.py) ---
    # A pack of N SKUs = N white-background shots, paid once through one Razorpay link. Credits live in their own
    # ledger (customer_sku_credits), never in wallet_balance. Off by default: no menus, no catalogue orders, no pack
    # links. Credits already paid for are always honoured, even when this is off.
    SKU_PACKS_ENABLED: bool = False
    # Price of one SKU in whole rupees, GST included. Only the FIRST seed: once scripts/set_price.py has written a
    # price to the price_settings table, the database value is used.
    SKU_PRICE_RUPEES: int = 20
    # Price of one Catalog Pack SKU (sku_pack_N: one photo in 7 photoshoot styles) in whole rupees, GST included.
    # Its tiers are N x this price. Kept apart from SKU_PRICE_RUPEES, which prices Studio Shot (studio_sku_N).
    CATALOG_PACK_SKU_PRICE: int = 500
    SKU_PACK_SIZES: str = "1,5,20,50,100"
    # The 1-SKU pack can always be bought by retailer id (pack_1) but is only shown in menus when this is true.
    ECOM_PACK1_ENABLED: bool = False
    SKU_GST_PERCENT: int = 18                 # splits the GST-inclusive price into net + tax for invoices
    SKU_CREDIT_VALIDITY_DAYS: int = 90        # a new pack extends every unused credit to this many days
    SKU_ERPNEXT_ITEM_CODE: str = ""           # ERPNext item for packs; empty = ERPNEXT_RECHARGE_ITEM_CODE
    META_CATALOG_ID: str = ""                 # WhatsApp catalogue that scripts/set_price.py updates
    # Debug: when Meta refuses the Step-2 product_list message, False = log the exact Meta error and tell the customer
    # to try again (no silent radio list); True = fall back to the radio list menu.
    PRODUCT_LIST_FALLBACK_ENABLED: bool = False

    # --- Phase 8: Google Drive delivery (app/services/google_drive.py, drive_layout.py) ---
    # Finished white-background images go to {phone}/Images/{day}/ in the business shared drive instead of the chat,
    # followed by one "ready" message. Off by default: delivery stays on WhatsApp exactly as before.
    DRIVE_DELIVERY_ENABLED: bool = False
    # True = each customer's root Drive folder is also "anyone with the link can view" (so the link opens without
    # signing in). Customer images and invoices sit under that folder: set False to share by email address only.
    DRIVE_LINK_PUBLIC: bool = True
    GOOGLE_SA_KEY_FILE: str = ""              # path to the service-account JSON key (business Workspace); never logged
    DRIVE_SHARED_DRIVE_ID: str = ""
    DRIVE_IMAGES_RETENTION_DAYS: int = 90     # delivered images are deleted from Drive after this; invoices stay
    BATCH_NOTIFY_DELAY_SECONDS: int = 60      # quiet time before the one "your images are ready" message
    # Approved Meta utility template for out-of-window "ready" notices (parameters: folder link, SKUs left); empty =
    # alert only. Until Meta approves it, an out-of-window send fails and is retried, then alerted.
    BATCH_NOTIFY_TEMPLATE_NAME: str = "sku_batch_ready"
    BATCH_NOTIFY_TEMPLATE_LANG: str = "en"

    # Razorpay (or any PSP) payment-page URL used by the "Pay ₹<price>" CTA
    # URL button. Leave empty to fall back to the interactive reply button
    # ('recharge_500' / "💳 Recharge to use") that the onboarding flow already
    # uses. No payment gateway SDK or credential is required by this code —
    # the button only opens the PSP-hosted page.
    RECHARGE_PAYMENT_URL: str = _BUILTIN_RECHARGE_PAYMENT_URL

    # Zero-cost WhatsApp test mode. Must be a Settings field: pydantic-settings
    # reads backend/.env into this object only -- it never exports .env into
    # os.environ, so the old os.getenv() read silently ignored .env.
    DRY_RUN_IMAGE_MODE: bool = False

    # --- Generation spend guard (app/ai/image_generation_manager.py) ---
    # Hard kill switch: set False to pause all image generation immediately.
    GENERATION_ENABLED: bool = True
    # Daily ceiling on ImageGenerationManager.generate_image() calls.
    MAX_GENERATIONS_PER_DAY: int = 100000

    # --- Administrators ---
    # Usernames (comma-separated) that are administrators, in addition to users flagged is_admin in the database.
    # While NO administrator exists anywhere (this empty and no flagged user), admin-only endpoints stay open to
    # any logged-in user, as they were before, and log a warning: set this to lock them down.
    ADMIN_USERNAMES: str = ""

    # --- Operations (health, metrics, error tracking, alerts) ---
    # Sentry error tracking is off unless a DSN is set (and the sentry-sdk package installed).
    SENTRY_DSN: str = ""
    # Operations alerts go to these WhatsApp numbers (comma-separated, international format). Empty = log only.
    OPS_ALERT_WHATSAPP_NUMBERS: str = ""
    OPS_ALERT_COOLDOWN_MINUTES: int = 60
    OPS_ALERT_SWEEP_INTERVAL_SECONDS: int = 300
    # Alert when this share of today's generation ceiling is used.
    OPS_ALERT_SPEND_WARN_FRACTION: float = 0.8

    # --- Meta (WhatsApp) request retries (app/services/meta_whatsapp_service.py) ---
    # 429 / 5xx answers and connection failures are retried this many times with jittered waits. A timed-out
    # SEND is never retried (it may already have been delivered).
    META_REQUEST_RETRIES: int = 2
    META_RETRY_BACKOFF_BASE_SECONDS: float = 1.0
    META_RETRY_BACKOFF_CAP_SECONDS: float = 10.0
    # Razorpay API calls (payment-link creation) give up after this long; the static fallback link is used.
    RAZORPAY_API_TIMEOUT_SECONDS: float = 10.0

    # --- Worker thread pools (app/utils/executors.py); 0 = automatic ---
    CPU_WORKER_THREADS: int = 0
    IO_WORKER_THREADS: int = 0
    NET_WORKER_THREADS: int = 0

    # --- Database connection pool (PostgreSQL; app/database.py) ---
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT_SECONDS: float = 5.0
    DB_POOL_RECYCLE_SECONDS: int = 1800

    # --- Image provider timeouts and retries (app/ai/*) ---
    # Client-side limits, so a slow or hung provider can never hold a paid order forever.
    GEMINI_IMAGE_TIMEOUT_SECONDS: float = 90.0
    OPENAI_IMAGE_TIMEOUT_SECONDS: float = 120.0       # the OpenAI default is 600 s, longer than the stuck-order limit
    OPENAI_IMAGE_CONNECT_TIMEOUT_SECONDS: float = 10.0
    # Hard deadline the manager puts on ONE provider attempt (a backstop above the client timeouts).
    IMAGE_PROVIDER_TIMEOUT_SECONDS: float = 130.0
    # A rate limit (429) or temporary overload (503) is retried this many times, with jittered waits, on
    # the same provider before the next provider is tried. Billing / daily-quota exhaustion is never retried.
    IMAGE_RATE_LIMIT_RETRIES: int = 2
    # Circuit breaker per provider (EXT-6): this many outage-type failures in a row marks the provider down for the
    # cool-down; 0 turns the breaker off. IMAGE_PROVIDER_RPM sizes a token bucket to the provider's quota (calls per
    # minute, 0 = off) and a call that would wait longer than IMAGE_RATE_WAIT_MAX_SECONDS fails fast instead.
    # Durable background jobs (ops forwards, invoice sends) are recorded in the database and retried (Q-5).
    OUTBOX_ENABLED: bool = True
    # How many paid provider calls may run at once in this process (0 = no limit) and how many paid orders one
    # customer may have in progress (0 = no limit). Size the first to the provider quota (Q-6).
    MAX_CONCURRENT_PROVIDER_CALLS: int = 0
    # Team (ADMIN) orders are not part of the customers' daily ceiling but have a ceiling of their own, so a mistake
    # or a loop on a team phone cannot spend without limit (COST-2). 0 = no limit.
    MAX_ADMIN_GENERATIONS_PER_DAY: int = 200
    # Photo bursts become one bulk order (UX-1). A photo within BULK_WINDOW_SECONDS of another waiting photo is part of a
    # burst; after BULK_QUIET_SECONDS without a new photo the customer gets ONE "N photos, Rs X: confirm?" message.
    # Needs OUTBOX_ENABLED. At most BULK_MAX_PHOTOS per confirmation; photos older than BULK_LOOKBACK_MINUTES are not grouped.
    # The WhatsApp chat dashboard's record (every message, every image we produced, every invoice). Chats and the images
    # we produced are kept RETENTION_CHAT_DAYS days (with RETENTION_ENABLED), like customer photos.
    # Dashboard access: usernames in ADMIN_USERNAMES / is_admin users, plus these Google or login emails (comma-separated).
    # GOOGLE_CLIENT_ID turns on "Sign in with Google" (the dashboard sends Google's ID token to /api/auth/google).
    DASHBOARD_ALLOWED_EMAILS: str = ""
    GOOGLE_CLIENT_ID: str = ""
    DASHBOARD_HISTORY_DAYS: int = 90
    # Google Drive archive of customer photos and the images we produced (see app/services/drive_archive.py). OFF until
    # DRIVE_ENABLED=true and the service account is set up (GOOGLE_SA_KEY_FILE, DRIVE_SHARED_DRIVE_ID above). Files go
    # into DRIVE_FOLDER_ID (a folder inside the shared drive), else the top of the shared drive. The personal-OAuth
    # settings GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET and GOOGLE_DRIVE_REFRESH_TOKEN were removed; an old
    # .env that still sets them loads fine (extra="ignore").
    DRIVE_ENABLED: bool = False
    DRIVE_FOLDER_ID: str = ""
    CHAT_LOG_ENABLED: bool = True
    RETENTION_CHAT_DAYS: int = 90
    BULK_ENABLED: bool = True
    BULK_WINDOW_SECONDS: int = 45
    BULK_QUIET_SECONDS: int = 8
    BULK_MAX_PHOTOS: int = 50
    BULK_CONCURRENCY: int = 4                 # photos of one bulk order worked on at the same time
    BULK_LOOKBACK_MINUTES: int = 15
    # What one successful image call costs, in rupees, per provider (COST-4). 0 = unknown: calls are still counted.
    # OPS_ALERT_DAILY_COST_RUPEES warns the owner on WhatsApp when a day's estimated AI spend passes this (0 = off).
    COST_PER_CALL_GEMINI_RUPEES: float = 0.0
    COST_PER_CALL_OPENAI_RUPEES: float = 0.0
    OPS_ALERT_DAILY_COST_RUPEES: int = 0

    # Data retention (DATA-7). OFF until the owner confirms the periods: it deletes customer photos from disk.
    # Financial records (wallet ledger, payments, refunds, invoices) are never touched by retention.
    RETENTION_ENABLED: bool = False
    RETENTION_MEDIA_DAYS: int = 90            # customer photos of finished orders
    RETENTION_AUDIT_MASK_DAYS: int = 30       # phone numbers inside audit-log details are masked after this long
    # "DELETE MY DATA" (PRIV-3): lets a customer erase their photos and personal details by WhatsApp message.
    ERASURE_COMMAND_ENABLED: bool = True
    # Consent to the data notice before personal data is collected (PRIV-2, DPDP Act). OFF until the owner provides
    # the notice wording in CONSENT_NOTICE_TEXT; raise CONSENT_VERSION when the notice changes (everyone agrees again).
    CONSENT_REQUIRED: bool = False
    CONSENT_VERSION: str = "1"
    CONSENT_NOTICE_TEXT: str = ""
    MAX_INFLIGHT_ORDERS_PER_CUSTOMER: int = 3
    CIRCUIT_BREAKER_FAILURES: int = 5
    CIRCUIT_BREAKER_COOLDOWN_SECONDS: float = 60.0
    IMAGE_PROVIDER_RPM: int = 0
    IMAGE_RATE_WAIT_MAX_SECONDS: float = 20.0
    IMAGE_RETRY_BACKOFF_BASE_SECONDS: float = 2.0
    IMAGE_RETRY_BACKOFF_CAP_SECONDS: float = 15.0
    # Whole Catalog Pack: styles still running after this long are dropped and the pack ships with the
    # styles that finished (a pack whose styles all fail is failed and refunded as before).
    # Pack styles are single attempts (no retry, no fallback), so one style is bounded by IMAGE_PROVIDER_TIMEOUT_SECONDS
    # plus any wait for a provider slot; this leaves room for that wait and, with the Meta upload that follows
    # (up to ~190 s), stays under the 10-minute stuck-order limit.
    PACK_GENERATION_DEADLINE_SECONDS: float = 360.0

    # --- Image calls per order ---
    # A Clean Studio Shot makes one Gemini image call; a Catalog Pack makes one call per style in
    # meta_whatsapp_service.CATALOG_PACK_STYLES (every style, fixed in code), each a single attempt with no retry
    # and no fallback provider. The photo pre-check makes no Gemini call (image_prevalidation_service approves
    # every non-empty photo). The settings MAX_STYLES_PER_PACK, IMAGE_PREVALIDATION_MODEL and
    # IMAGE_PREVALIDATION_FAIL_OPEN were removed; an old .env that still sets them loads fine (extra="ignore").
    IMAGE_PREVALIDATION_ENABLED: bool = True

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
        "<magenta>{extra[request_id]}</magenta> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )
    # One JSON object per line on the console (for a log collector) instead of the readable format.
    LOG_JSON: bool = False

    # --- Celery / Task Queue ---
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"

    # When True, tasks run synchronously (no broker needed) — ideal for dev.
    # Set to False in production when a real Redis/RabbitMQ broker is available.
    CELERY_TASK_ALWAYS_EAGER: bool = True

    # Name of the Celery task queue for analysis jobs
    CELERY_ANALYSIS_QUEUE: str = "analysis"
    # Per-task limits for analysis / prompt tasks (Q-4): soft first, then hard.
    CELERY_TASK_SOFT_TIME_LIMIT_SECONDS: int = 240
    CELERY_TASK_TIME_LIMIT_SECONDS: int = 300

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
            elif len(set(secret_key)) < _MIN_SECRET_KEY_DISTINCT_CHARS:
                problems.append("SECRET_KEY has too few distinct characters to be random")
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
            # Things worth fixing but not worth refusing to start over (changing them blindly could break a
            # working production setup): say so loudly in the log.
            if self.RECHARGE_PAYMENT_URL == _BUILTIN_RECHARGE_PAYMENT_URL:
                _config_logger.warning(
                    "RECHARGE_PAYMENT_URL is not set: the built-in static payment link from the source code is "
                    "used as the fallback. Set RECHARGE_PAYMENT_URL to your own link in the environment."
                )
            if self.CELERY_TASK_ALWAYS_EAGER:
                _config_logger.warning(
                    "CELERY_TASK_ALWAYS_EAGER is true in production: analysis tasks run inside the web process. "
                    "Set it to false and run a real worker with a broker."
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
