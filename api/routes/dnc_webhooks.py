"""
KJLE — DNC inbound webhooks
File: api/routes/dnc_webhooks.py

Routes (PREFIX /kjle/v1 added by main.py):
  POST /dnc/webhooks/suppression  — x-api-key auth; generic intake for any
                                    empire app (DemoEnginez, CEz, VoiceDrop, etc.)

Closes the DNC feedback loop: opt-out signals from any sending platform flow
automatically into KJLE's suppression list.

Internal helpers (private to this module):
  _add_phone_suppression()  — normalize, UPSERT dnc_suppressions, invalidate cache, audit
  _add_email_suppression()  — normalize, UPSERT email_suppressions, audit
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..database import get_db
from ..lib.phone_utils import normalize_phone

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Constants ────────────────────────────────────────────────────────────────

API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class GenericSuppressionRequest(BaseModel):
    phone:    Optional[str]  = None
    email:    Optional[str]  = None
    reason:   str
    source:   str
    lead_id:  Optional[str]  = None
    notes:    Optional[str]  = None
    metadata: Optional[dict] = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_email(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).strip().lower()
    if not s or "@" not in s or s.startswith("@") or s.endswith("@"):
        return None
    return s


def _audit(
    *,
    phone: Optional[str],
    email: Optional[str],
    source: str,
    result: str,
    is_dnc: Optional[bool] = None,
    requesting_lead_id: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Insert dnc_audit_log row. Best-effort — never raises."""
    md = dict(metadata or {})
    if email:
        md.setdefault("email", email)
    try:
        db = get_db()
        db.table("dnc_audit_log").insert({
            "phone":              phone,
            "source":             source,
            "result":             result,
            "is_dnc":             is_dnc,
            "cost_usd":           0.0,
            "requesting_lead_id": requesting_lead_id,
            "error":              error,
            "metadata":           md,
            "occurred_at":        _now_iso(),
        }).execute()
    except Exception as e:
        logger.error(f"dnc_webhooks: audit insert failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Suppression helpers (private, used by the webhook endpoint)
# ─────────────────────────────────────────────────────────────────────────────

async def _add_phone_suppression(
    phone: str,
    reason: str,
    source: str,
    lead_id: Optional[str] = None,
    notes: Optional[str]   = None,
    metadata: Optional[dict] = None,
) -> dict:
    """
    Normalize → UPSERT dnc_suppressions → invalidate dnc_cache → audit.
    Returns {"added": bool, "phone_normalized": str|None, "error": str|None}.
    Never raises — webhook endpoints must always 200.
    """
    norm = normalize_phone(phone)
    if not norm:
        msg = f"invalid_phone_format: {phone!r}"
        logger.warning(f"dnc_webhooks: {msg}")
        _audit(phone=None, email=None, source=source, result="webhook_skipped",
               error=msg, metadata={"input_phone": phone, "reason": reason})
        return {"added": False, "phone_normalized": None, "error": msg}

    db = get_db()
    payload = {
        "phone":         norm,
        "reason":        reason,
        "source":        source,
        "suppressed_at": _now_iso(),
        "notes":         notes,
        "metadata":      metadata or {},
    }
    try:
        db.table("dnc_suppressions").upsert(payload, on_conflict="phone").execute()
    except Exception as e:
        logger.error(f"dnc_webhooks: phone suppression upsert failed for {norm}: {e}")
        _audit(phone=norm, email=None, source=source, result="error",
               error=f"upsert_failed: {e}", requesting_lead_id=lead_id)
        return {"added": False, "phone_normalized": norm, "error": str(e)}

    # Invalidate any cache entry — set expires_at to past so subsequent /check
    # will re-evaluate via the suppression hit.
    try:
        past = "2000-01-01T00:00:00+00:00"
        db.table("dnc_cache").update({"expires_at": past}).eq("phone", norm).execute()
    except Exception as e:
        logger.warning(f"dnc_webhooks: cache invalidate failed for {norm} (non-fatal): {e}")

    _audit(
        phone=norm, email=None, source=source, result="internal_suppression",
        is_dnc=True, requesting_lead_id=lead_id,
        metadata={"action": "added_via_webhook", "reason": reason, "notes": notes, **(metadata or {})},
    )
    return {"added": True, "phone_normalized": norm, "error": None}


async def _add_email_suppression(
    email: str,
    reason: str,
    source: str,
    lead_id: Optional[str] = None,
    notes: Optional[str]   = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Normalize → UPSERT email_suppressions → audit. Never raises."""
    norm = _normalize_email(email)
    if not norm:
        msg = f"invalid_email_format: {email!r}"
        logger.warning(f"dnc_webhooks: {msg}")
        _audit(phone=None, email=None, source=source, result="webhook_skipped",
               error=msg, metadata={"input_email": email, "reason": reason})
        return {"added": False, "email_normalized": None, "error": msg}

    db = get_db()
    payload = {
        "email":         norm,
        "reason":        reason,
        "source":        source,
        "suppressed_at": _now_iso(),
        "notes":         notes,
        "metadata":      metadata or {},
    }
    try:
        db.table("email_suppressions").upsert(payload, on_conflict="email").execute()
    except Exception as e:
        logger.error(f"dnc_webhooks: email suppression upsert failed for {norm}: {e}")
        _audit(phone=None, email=norm, source=source, result="error",
               error=f"upsert_failed: {e}", requesting_lead_id=lead_id)
        return {"added": False, "email_normalized": norm, "error": str(e)}

    _audit(
        phone=None, email=norm, source=source, result="internal_suppression",
        is_dnc=None, requesting_lead_id=lead_id,
        metadata={"action": "added_via_webhook", "kind": "email", "reason": reason,
                  "notes": notes, **(metadata or {})},
    )
    return {"added": True, "email_normalized": norm, "error": None}


# ─────────────────────────────────────────────────────────────────────────────
# POST /dnc/webhooks/suppression
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/dnc/webhooks/suppression")
async def generic_suppression_webhook(
    body: GenericSuppressionRequest,
    x_api_key: str = Header(...),
):
    """
    Generic suppression intake for any empire app. x-api-key authenticated.
    Requires at least one of phone/email plus reason+source.
    """
    verify_api_key(x_api_key)

    if not (body.phone or body.email):
        raise HTTPException(
            status_code=400,
            detail="at_least_one_of_phone_or_email_required",
        )

    summary = {"phone_added": False, "email_added": False}

    if body.phone:
        r = await _add_phone_suppression(
            body.phone, body.reason, body.source,
            lead_id=body.lead_id, notes=body.notes, metadata=body.metadata,
        )
        summary["phone_added"]      = bool(r.get("added"))
        summary["phone_normalized"] = r.get("phone_normalized")
        if r.get("error"):
            summary["phone_error"] = r["error"]

    if body.email:
        r = await _add_email_suppression(
            body.email, body.reason, body.source,
            lead_id=body.lead_id, notes=body.notes, metadata=body.metadata,
        )
        summary["email_added"]      = bool(r.get("added"))
        summary["email_normalized"] = r.get("email_normalized")
        if r.get("error"):
            summary["email_error"] = r["error"]

    return {"status": "success", **summary}
