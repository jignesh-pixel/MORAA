"""Read-only API for the WhatsApp chat dashboard (owner, business partner and developer only).

Every route except the signed image address needs a logged-in ADMINISTRATOR (ADMIN_USERNAMES / is_admin / an email in
DASHBOARD_ALLOWED_EMAILS). Unlike the retry endpoint there is NO open-until-configured fallback: this exposes every
customer's data, so with nobody configured it answers 403.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.dependencies import is_admin_user, require_auth
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.services import dashboard_service as svc

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


def allowed_emails() -> set:
    return {e.strip().lower() for e in (settings.DASHBOARD_ALLOWED_EMAILS or "").split(",") if e.strip()}


def require_dashboard_user(user: User = Depends(require_auth), db: Session = Depends(get_db)) -> User:
    # An email in DASHBOARD_ALLOWED_EMAILS only counts for a VERIFIED account (created by Google sign-in or by the
    # create_dashboard_user script): a password sign-up never proves ownership of its email address.
    verified_email = bool(getattr(user, "is_verified", False)) and str(getattr(user, "email", "") or "").lower() in allowed_emails()
    if is_admin_user(user, db) or verified_email:
        return user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This dashboard is for the owners only")


@router.get("/me", summary="Who am I (also checks the dashboard may be used)")
def me(user: User = Depends(require_dashboard_user)) -> Dict[str, Any]:
    return {"username": user.username, "email": user.email, "history_days": int(settings.DASHBOARD_HISTORY_DAYS)}


@router.get("/customers", summary="Customers with a chat, newest activity first")
def customers(
    q: str = Query("", max_length=60), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    _: User = Depends(require_dashboard_user), db: Session = Depends(get_db),
) -> Dict[str, Any]:
    return {"customers": svc.list_customers(db, q, limit, offset)}


@router.get("/customers/{phone}/timeline", summary="One customer's chat with payments and invoices in it")
def timeline(
    phone: str, before: Optional[datetime] = None, limit: int = Query(150, ge=1, le=500),
    _: User = Depends(require_dashboard_user), db: Session = Depends(get_db),
) -> Dict[str, Any]:
    return svc.timeline(db, phone, before, limit)


@router.get("/customers/{phone}/profile", summary="One customer's details, payments, orders and invoices")
def profile(phone: str, _: User = Depends(require_dashboard_user), db: Session = Depends(get_db)) -> Dict[str, Any]:
    data = svc.profile(db, phone)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such customer")
    return data


@router.get("/audit", summary="Weekly audit: what each customer sent and what they got")
def audit(
    days: int = Query(7, ge=1, le=90), status_filter: Optional[str] = Query(None, alias="status", max_length=30),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    _: User = Depends(require_dashboard_user), db: Session = Depends(get_db),
) -> Dict[str, Any]:
    return {"orders": svc.audit(db, days, status_filter, limit, offset)}


@router.get("/stats", summary="SKU packs sold, their revenue and the credits customers still hold")
def stats(_: User = Depends(require_dashboard_user), db: Session = Depends(get_db)) -> Dict[str, Any]:
    return svc.sku_stats(db)


@router.get("/media/{kind}/{media_id}", summary="One image, by a short-lived signed address")
def media(kind: str, media_id: str, exp: int, sig: str, db: Session = Depends(get_db)):
    if not svc.media_signature_ok(kind, media_id, exp, sig):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This link has expired")
    found = svc.resolve_media_file(db, kind, media_id)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not available (it may be past the 90-day limit)")
    return FileResponse(found["path"], media_type=found["mime"], headers={"Cache-Control": "private, max-age=300"})
