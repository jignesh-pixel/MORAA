"""
ERPNext Accounting Connector — isolated, zero-disruption service module.

This module is 100% standalone: it reads its configuration exclusively from
environment variables (with safe defaults), never raises on network/API
errors, and degrades to a logged no-op ("mock") mode when credentials are
absent. It does NOT touch the Razorpay flow, image pipeline, database
models, or any other production path.

Configuration (all optional — missing values activate no-op mode):
    ERPNEXT_BASE_URL     e.g. "https://erp.example.com"
    ERPNEXT_API_KEY      ERPNext API key (token auth)
    ERPNEXT_API_SECRET   ERPNext API secret (token auth)
    ERPNEXT_COMPANY      Company name invoices are booked against
"""

import asyncio
import os
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from app.utils.logger import logger


def _cfg(name: str, default: Any = "") -> Any:
    """Read a setting from the Pydantic ``settings`` (which loads backend/.env),
    falling back to the process environment. pydantic-settings never exports
    .env into os.environ, so a bare os.getenv() missed every .env value."""
    try:
        from app.config import settings

        value = getattr(settings, name, None)
    except Exception:  # noqa: BLE001 — config import must never break callers
        value = None
    if value is None or value == "":
        value = os.getenv(name, default)
    return value if value is not None else default


class ERPNextError(Exception):
    """Raised internally by the billing helpers; never escapes the public API."""

_LOG_CATEGORY = "accounting"

#: Environment variables required to leave no-op mode.
_REQUIRED_ENV_VARS = (
    "ERPNEXT_BASE_URL",
    "ERPNEXT_API_KEY",
    "ERPNEXT_API_SECRET",
    "ERPNEXT_COMPANY",
)

#: Strict timeout for every ERPNext HTTP call (seconds), applied to
#: connect / read / write / pool uniformly.
_DEFAULT_TIMEOUT_SECONDS = 8.0

# Process-level flag so the "credentials missing" warning is logged once
# instead of spamming the logs on every sync attempt.
_warned_missing_config = False


class ERPNextService:
    """Async ERPNext REST client that fails silent, never fatal.

    Every public method returns a plain value (``str`` / ``dict``) and
    swallows all network/API errors after logging them, so a broken or
    unconfigured ERPNext instance can never crash an upstream caller
    (e.g. the Celery worker or a request handler).
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        company: Optional[str] = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Initialise the connector.

        Args:
            base_url: Overrides ``ERPNEXT_BASE_URL`` (dependency-injection
                hook for tests). Defaults to the environment variable.
            api_key: Overrides ``ERPNEXT_API_KEY``.
            api_secret: Overrides ``ERPNEXT_API_SECRET``.
            company: Overrides ``ERPNEXT_COMPANY``.
            timeout_seconds: Strict HTTP timeout applied to all phases.

        Raises:
            Nothing — invalid configuration simply activates no-op mode.
        """
        self.base_url: str = str(
            base_url if base_url is not None else _cfg("ERPNEXT_BASE_URL")
        ).rstrip("/")
        self.api_key: str = api_key if api_key is not None else str(_cfg("ERPNEXT_API_KEY"))
        self.api_secret: str = (
            api_secret if api_secret is not None else str(_cfg("ERPNEXT_API_SECRET"))
        )
        self.company: str = company if company is not None else str(_cfg("ERPNEXT_COMPANY"))
        # Strict 8-second timeout: uniform cap on connect, read, write, pool.
        self.timeout: httpx.Timeout = httpx.Timeout(timeout_seconds)

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """True when all four ERPNext settings are present and non-empty."""
        return bool(self.base_url and self.api_key and self.api_secret and self.company)

    def _log_missing_config_once(self) -> None:
        """Warn exactly once per process when ERPNext is unconfigured."""
        global _warned_missing_config
        if not _warned_missing_config:
            _warned_missing_config = True
            missing = [
                var
                for var, value in zip(
                    _REQUIRED_ENV_VARS,
                    (self.base_url, self.api_key, self.api_secret, self.company),
                )
                if not value
            ]
            logger.bind(category=_LOG_CATEGORY).warning(
                "ERPNext connector is NOT configured "
                f"(missing: {', '.join(missing)}). "
                "Running in no-op mode — accounting sync calls will be skipped.",
            )

    def _auth_headers(self) -> Dict[str, str]:
        """Build ERPNext token-auth headers."""
        return {
            "Authorization": f"token {self.api_key}:{self.api_secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_or_create_customer(
        self,
        name: str,
        phone: str,
        email: Optional[str] = None,
    ) -> str:
        """Find an ERPNext Customer by ``customer_name`` or create it.

        Args:
            name: Customer display name (used as the lookup key).
            phone: Contact phone number stored on the customer record.
            email: Optional contact email address.

        Returns:
            The ERPNext customer document ``name`` (ID string), or an empty
            string when the connector is in no-op mode or the request fails.
        """
        # Input validation — fail silent with a warning, never raise.
        if not isinstance(name, str) or not name.strip():
            logger.bind(category=_LOG_CATEGORY).warning(
                "get_or_create_customer called with an empty customer name — skipping."
            )
            return ""
        if not isinstance(phone, str) or not phone.strip():
            logger.bind(category=_LOG_CATEGORY).warning(
                f"get_or_create_customer called without a phone for '{name}' — skipping."
            )
            return ""

        if not self.is_configured:
            self._log_missing_config_once()
            return ""

        try:
            # 1) Lookup by customer_name (filters are JSON-encoded query params).
            params: Dict[str, str] = {
                "filters": json_dumps_filters([["customer_name", "=", name.strip()]]),
                "fields": json_dumps_filters(["name", "customer_name"]),
                "limit_page_length": "1",
            }
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/api/resource/Customer",
                    headers=self._auth_headers(),
                    params=params,
                )
                response.raise_for_status()
                records: Any = (response.json() or {}).get("data") or []
                if records:
                    customer_id: str = str(records[0].get("name", ""))
                    logger.bind(category=_LOG_CATEGORY).info(
                        f"ERPNext customer found: '{name}' -> {customer_id}"
                    )
                    return customer_id

                # 2) Not found — create it.
                customer_doc: Dict[str, Any] = {
                    "doctype": "Customer",
                    "customer_name": name.strip(),
                    "customer_type": "Individual",
                    "mobile_no": phone.strip(),
                }
                if email and email.strip():
                    customer_doc["email_id"] = email.strip()
                create_response = await client.post(
                    f"{self.base_url}/api/resource/Customer",
                    headers=self._auth_headers(),
                    json=customer_doc,
                )
                create_response.raise_for_status()
                created: str = str(
                    ((create_response.json() or {}).get("data") or {}).get("name", "")
                )
                logger.bind(category=_LOG_CATEGORY).info(
                    f"ERPNext customer created: '{name}' -> {created}"
                )
                return created
        except httpx.HTTPError as exc:
            # Covers timeouts, connection errors, and HTTP status errors.
            # A "duplicate customer" race is resolved by one final lookup.
            logger.bind(category=_LOG_CATEGORY).warning(
                f"ERPNext customer upsert issue for '{name}': {exc} — retrying lookup."
            )
            return await self._lookup_customer(name)
        except Exception as exc:  # noqa: BLE001 — never propagate to callers.
            logger.bind(category=_LOG_CATEGORY).error(
                f"Unexpected ERPNext error in get_or_create_customer('{name}'): {exc}"
            )
            return ""

    async def create_sales_invoice(
        self,
        customer_id: str,
        item_code: str,
        amount: float,
        transaction_id: str,
    ) -> Dict[str, Any]:
        """Create a single-line Sales Invoice in ERPNext.

        Args:
            customer_id: ERPNext customer document ``name`` (as returned by
                :meth:`get_or_create_customer`).
            item_code: ERPNext Item code billed on the invoice.
            amount: Invoice line rate (must be a finite, positive number).
            transaction_id: External payment reference recorded in remarks
                for reconciliation.

        Returns:
            ``{"success": True, "invoice_name": ..., ...}`` on success, or
            ``{"success": False, ...}`` on no-op/validation/error — never raises.
        """
        # Input validation — fail silent with a warning, never raise.
        if not isinstance(customer_id, str) or not customer_id.strip():
            logger.bind(category=_LOG_CATEGORY).warning(
                "create_sales_invoice called with an empty customer id — skipping."
            )
            return self._failure_result("empty_customer_id", transaction_id)
        if not isinstance(item_code, str) or not item_code.strip():
            logger.bind(category=_LOG_CATEGORY).warning(
                "create_sales_invoice called with an empty item code — skipping."
            )
            return self._failure_result("empty_item_code", transaction_id)
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not amount > 0:
            logger.bind(category=_LOG_CATEGORY).warning(
                f"create_sales_invoice called with invalid amount {amount!r} — skipping."
            )
            return self._failure_result("invalid_amount", transaction_id)
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            logger.bind(category=_LOG_CATEGORY).warning(
                "create_sales_invoice called with an empty transaction id — skipping."
            )
            return self._failure_result("empty_transaction_id", transaction_id)

        if not self.is_configured:
            self._log_missing_config_once()
            return self._failure_result("noop_mode", transaction_id)

        invoice_doc: Dict[str, Any] = {
            "doctype": "Sales Invoice",
            "company": self.company,
            "customer": customer_id.strip(),
            "items": [
                {
                    "item_code": item_code.strip(),
                    "qty": 1,
                    "rate": float(amount),
                    "amount": float(amount),
                }
            ],
            "remarks": f"Transaction {transaction_id.strip()}",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/resource/Sales Invoice",
                    headers=self._auth_headers(),
                    json=invoice_doc,
                )
                response.raise_for_status()
                data: Any = (response.json() or {}).get("data") or {}
                invoice_name: str = str(data.get("name", ""))
                logger.bind(category=_LOG_CATEGORY).info(
                    f"ERPNext sales invoice created: {invoice_name} "
                    f"(customer={customer_id}, amount={amount}, "
                    f"transaction={transaction_id})"
                )
                return {
                    "success": True,
                    "invoice_name": invoice_name,
                    "customer": customer_id.strip(),
                    "amount": float(amount),
                    "transaction_id": transaction_id.strip(),
                    "company": self.company,
                }
        except httpx.HTTPError as exc:
            logger.bind(category=_LOG_CATEGORY).error(
                f"ERPNext sales invoice failed "
                f"(customer={customer_id}, transaction={transaction_id}): {exc}"
            )
            return self._failure_result("http_error", transaction_id, detail=str(exc))
        except Exception as exc:  # noqa: BLE001 — never propagate to callers.
            logger.bind(category=_LOG_CATEGORY).error(
                f"Unexpected ERPNext error in create_sales_invoice "
                f"(transaction={transaction_id}): {exc}"
            )
            return self._failure_result("unexpected_error", transaction_id, detail=str(exc))

    # ------------------------------------------------------------------
    # Billing flow: customer -> submitted Sales Invoice -> Payment Entry -> PDF
    # ------------------------------------------------------------------

    async def create_paid_invoice_pdf(
        self,
        whatsapp_id: str,
        customer_name: str,
        gstin: Optional[str],
        amount: float,
        payment_id: str,
    ) -> Optional[Tuple[bytes, str]]:
        """Bill one captured payment in ERPNext and return (pdf_bytes, invoice_name).

        Idempotent on ``payment_id`` (stored in the invoice's ``po_no``): a
        retry reuses the existing invoice and never bills twice. Returns None
        on ANY failure/timeout/no-op (logged) so the caller can fall back to
        the local receipt — never raises.
        """
        if not self.is_configured:
            self._log_missing_config_once()
            return None
        item_code = str(_cfg("ERPNEXT_RECHARGE_ITEM_CODE")).strip()
        if not (whatsapp_id and payment_id and item_code) or not amount or amount <= 0:
            logger.bind(category=_LOG_CATEGORY).warning(
                f"ERPNext billing skipped: missing input (payment={payment_id!r}, item={item_code!r})"
            )
            return None
        try:
            job_timeout = float(_cfg("ERPNEXT_JOB_TIMEOUT_SECONDS", 60.0) or 60.0)
            return await asyncio.wait_for(
                self._bill_payment(whatsapp_id, customer_name, gstin, float(amount), payment_id, item_code),
                timeout=job_timeout,
            )
        except Exception as exc:  # noqa: BLE001 — includes TimeoutError / ERPNextError / httpx errors
            logger.bind(category=_LOG_CATEGORY).warning(
                f"ERPNext billing failed for payment={payment_id}: {type(exc).__name__}: {exc}"
            )
            return None

    async def _bill_payment(
        self,
        whatsapp_id: str,
        customer_name: str,
        gstin: Optional[str],
        amount: float,
        payment_id: str,
        item_code: str,
    ) -> Tuple[bytes, str]:
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self._auth_headers(), timeout=self.timeout
        ) as client:
            invoice = await self._find_invoice_by_payment(client, payment_id)
            if invoice is None:
                customer = await self._upsert_billing_customer(client, whatsapp_id, customer_name, gstin)
                invoice_name = await self._create_submitted_invoice(
                    client, customer, item_code, amount, payment_id
                )
            else:
                invoice_name = invoice["name"]
                logger.bind(category=_LOG_CATEGORY).info(
                    f"ERPNext invoice reused for payment={payment_id}: {invoice_name}"
                )
                if int(invoice.get("docstatus") or 0) == 0:
                    await self._call(client, "PUT", self._doc_path("Sales Invoice", invoice_name), json={"docstatus": 1})
            await self._record_payment(client, invoice_name, payment_id)
            pdf = await self._download_pdf(client, invoice_name)
            return pdf, invoice_name

    # -- HTTP helpers --------------------------------------------------

    @staticmethod
    def _doc_path(doctype: str, name: str = "") -> str:
        path = f"/api/resource/{quote(doctype, safe='')}"
        return f"{path}/{quote(name, safe='')}" if name else path

    @staticmethod
    async def _call(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> Any:
        response = await client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise ERPNextError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise ERPNextError(f"{method} {path} returned non-JSON") from exc

    async def _get_list(
        self, client: httpx.AsyncClient, doctype: str, filters: List[Any], fields: List[str]
    ) -> List[Dict[str, Any]]:
        body = await self._call(
            client,
            "GET",
            self._doc_path(doctype),
            params={
                "filters": json_dumps_filters(filters),
                "fields": json_dumps_filters(fields),
                "limit_page_length": "1",
            },
        )
        return (body or {}).get("data") or []

    # -- steps -----------------------------------------------------------

    async def _find_invoice_by_payment(
        self, client: httpx.AsyncClient, payment_id: str
    ) -> Optional[Dict[str, Any]]:
        rows = await self._get_list(
            client,
            "Sales Invoice",
            [["po_no", "=", payment_id], ["docstatus", "!=", 2], ["company", "=", self.company]],
            ["name", "docstatus", "outstanding_amount"],
        )
        return rows[0] if rows else None

    async def _upsert_billing_customer(
        self, client: httpx.AsyncClient, whatsapp_id: str, customer_name: str, gstin: Optional[str]
    ) -> str:
        """Customer keyed on mobile_no = WhatsApp number. GSTIN goes to the
        India Compliance ``gstin`` field (with gst_category), else ``tax_id``."""
        mobile = whatsapp_id.lstrip("+").strip()
        rows = await self._get_list(client, "Customer", [["mobile_no", "=", mobile]], ["name"])
        gst_fields = await self._gst_fields(client, gstin)
        if rows:
            name = rows[0]["name"]
            if gst_fields:
                try:
                    await self._call(client, "PUT", self._doc_path("Customer", name), json=gst_fields)
                except ERPNextError as exc:
                    logger.bind(category=_LOG_CATEGORY).warning(f"ERPNext GSTIN update skipped for {name}: {exc}")
            return name

        doc: Dict[str, Any] = {
            "customer_name": (customer_name or f"WhatsApp {mobile}")[:140],
            "customer_type": "Company" if gstin else "Individual",
            "mobile_no": mobile,
        }
        try:
            body = await self._call(client, "POST", self._doc_path("Customer"), json={**doc, **gst_fields})
        except ERPNextError as exc:
            if not gst_fields:
                raise
            # e.g. GSTIN rejected by India Compliance validation: still bill as B2C.
            logger.bind(category=_LOG_CATEGORY).warning(f"ERPNext customer with GSTIN rejected, retrying without: {exc}")
            body = await self._call(client, "POST", self._doc_path("Customer"), json=doc)
        return str(((body or {}).get("data") or {}).get("name", ""))

    async def _gst_fields(self, client: httpx.AsyncClient, gstin: Optional[str]) -> Dict[str, Any]:
        if not gstin:
            return {}
        if getattr(self, "_has_gstin_field", None) is None:
            probe = await client.get(
                self._doc_path("Customer"),
                params={"fields": json_dumps_filters(["name", "gstin"]), "limit_page_length": "1"},
            )
            self._has_gstin_field = probe.status_code == 200
        if self._has_gstin_field:
            return {"gstin": gstin, "gst_category": "Registered Regular"}
        return {"tax_id": gstin}

    async def _tax_rows(self, client: httpx.AsyncClient, template: str) -> List[Dict[str, Any]]:
        body = await self._call(client, "GET", self._doc_path("Sales Taxes and Charges Template", template))
        inclusive = 1 if bool(_cfg("ERPNEXT_PRICES_INCLUDE_TAX", True)) else 0
        rows = []
        for tax in ((body or {}).get("data") or {}).get("taxes") or []:
            row = {k: tax.get(k) for k in ("charge_type", "account_head", "rate", "description", "cost_center") if tax.get(k) is not None}
            row["included_in_print_rate"] = inclusive
            rows.append(row)
        return rows

    async def _create_submitted_invoice(
        self, client: httpx.AsyncClient, customer: str, item_code: str, amount: float, payment_id: str
    ) -> str:
        if not customer:
            raise ERPNextError("customer upsert returned no name")
        doc: Dict[str, Any] = {
            "company": self.company,
            "customer": customer,
            "po_no": payment_id,
            "remarks": f"Moraa GemVision wallet recharge. Razorpay payment {payment_id}",
            "items": [{"item_code": item_code, "qty": 1, "rate": float(amount)}],
            "docstatus": 1,  # insert + submit in one request (rolled back together on error)
        }
        template = str(_cfg("ERPNEXT_TAX_TEMPLATE")).strip()
        if template:
            doc["taxes_and_charges"] = template
            doc["taxes"] = await self._tax_rows(client, template)
        debtors = str(_cfg("ERPNEXT_DEFAULT_DEBTORS_ACCOUNT")).strip()
        if debtors:
            doc["debit_to"] = debtors
        body = await self._call(client, "POST", self._doc_path("Sales Invoice"), json=doc)
        name = str(((body or {}).get("data") or {}).get("name", ""))
        if not name:
            raise ERPNextError("Sales Invoice created without a name")
        logger.bind(category=_LOG_CATEGORY).info(
            f"ERPNext Sales Invoice submitted: {name} (customer={customer}, payment={payment_id})"
        )
        return name

    async def _record_payment(self, client: httpx.AsyncClient, invoice_name: str, payment_id: str) -> None:
        """Payment Entry against the invoice (skipped when already paid)."""
        body = await self._call(client, "GET", self._doc_path("Sales Invoice", invoice_name))
        outstanding = float(((body or {}).get("data") or {}).get("outstanding_amount") or 0)
        if outstanding <= 0:
            return
        existing = await self._get_list(
            client, "Payment Entry", [["reference_no", "=", payment_id], ["docstatus", "=", 1]], ["name"]
        )
        if existing:
            return
        made = await self._call(
            client,
            "POST",
            "/api/method/erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
            json={"dt": "Sales Invoice", "dn": invoice_name},
        )
        entry = (made or {}).get("message") or {}
        if not entry:
            raise ERPNextError("get_payment_entry returned nothing")
        mode = str(_cfg("ERPNEXT_MODE_OF_PAYMENT")).strip()
        if mode:
            entry["mode_of_payment"] = mode
        paid_to = str(_cfg("ERPNEXT_PAYMENT_ACCOUNT")).strip()
        if paid_to:
            entry["paid_to"] = paid_to
        entry["reference_no"] = payment_id
        entry["reference_date"] = date.today().isoformat()
        entry["docstatus"] = 1
        entry.pop("name", None)
        entry.pop("__islocal", None)
        await self._call(client, "POST", self._doc_path("Payment Entry"), json=entry)
        logger.bind(category=_LOG_CATEGORY).info(f"ERPNext Payment Entry recorded for {invoice_name} ({payment_id})")

    async def _download_pdf(self, client: httpx.AsyncClient, invoice_name: str) -> bytes:
        response = await client.get(
            "/api/method/frappe.utils.print_format.download_pdf",
            params={
                "doctype": "Sales Invoice",
                "name": invoice_name,
                "format": str(_cfg("ERPNEXT_PRINT_FORMAT", "Standard") or "Standard"),
                "no_letterhead": "0",
            },
        )
        if response.status_code >= 400 or not response.content.startswith(b"%PDF"):
            raise ERPNextError(f"PDF download failed for {invoice_name}: HTTP {response.status_code}")
        return response.content

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _lookup_customer(self, name: str) -> str:
        """Single best-effort customer lookup used for duplicate-race recovery."""
        try:
            params: Dict[str, str] = {
                "filters": json_dumps_filters([["customer_name", "=", name.strip()]]),
                "fields": json_dumps_filters(["name"]),
                "limit_page_length": "1",
            }
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/api/resource/Customer",
                    headers=self._auth_headers(),
                    params=params,
                )
                response.raise_for_status()
                records: Any = (response.json() or {}).get("data") or []
                if records:
                    found: str = str(records[0].get("name", ""))
                    logger.bind(category=_LOG_CATEGORY).info(
                        f"ERPNext customer recovered after race: '{name}' -> {found}"
                    )
                    return found
        except Exception as exc:  # noqa: BLE001 — best effort only.
            logger.bind(category=_LOG_CATEGORY).error(
                f"ERPNext customer recovery lookup failed for '{name}': {exc}"
            )
        return ""

    @staticmethod
    def _failure_result(
        reason: str, transaction_id: str, detail: str = ""
    ) -> Dict[str, Any]:
        """Build a consistent, safe failure payload (never raises)."""
        result: Dict[str, Any] = {
            "success": False,
            "reason": reason,
            "transaction_id": transaction_id,
        }
        if detail:
            result["detail"] = detail
        return result


def json_dumps_filters(value: Any) -> str:
    """Serialise an ERPNext filters/fields value into a JSON query string."""
    import json

    return json.dumps(value, separators=(",", ":"))


def get_erpnext_service() -> ERPNextService:
    """Factory returning a connector instance bound to current env vars.

    Kept as a module-level helper so callers (and tests) can monkeypatch a
    single symbol without touching any other module.
    """
    return ERPNextService()
