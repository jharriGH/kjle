"""
KJLE — DNC inbound webhooks (Phase 3)
File: api/routes/dnc_webhooks.py

Routes (PREFIX /kjle/v1 added by main.py):
  POST /dnc/webhooks/reachinbox?secret=<...>  — query-param secret auth
                                                (constant-time compare)
  POST /dnc/webhooks/suppression              — x-api-key auth; generic intake

Closes the DNC feedback loop: opt-out signals from ReachInbox (and any future
empire app) flow automatically into KJLE's suppression list.

ReachInbox auth model (per their docs at https://docs.reachinbox.ai/webhooks):
  HTTPS only, NO request signing — URL secrecy IS the security boundary.
  We require a ?secret=<REACHINBOX_WEBHOOK_SECRET> query parameter and compare
  it constant-time against settings.REACHINBOX_WEBHOOK_SECRET. Both sides
  must be non-empty (empty=empty is rejected). If the env var is unset, all
  webhooks fail-closed with 401.

Internal helpers (private to this module):
  _add_phone_suppression()  — normalize, UPSERT dnc_suppressions, invalidate cache, audit
  _add_email_suppression()  — normalize, UPSERT email_suppressions, audit

ReachInbox event canonicalization (defensive against schema variation):
  unsubscribed → suppress phone + email
  bounced_hard → suppress email only
  replied      → run reply_parser; suppress phone + email if unsubscribe match
  complained   → suppress phone + email
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from ..config import settings
from ..database import get_db
from ..lib.phone_utils import normalize_phone
from ..lib.reply_parser import is_unsubscribe_reply

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Constants ────────────────────────────────────────────────────────────────

# Canonical event types → known string variants we accept (case-insensitive).
EVENT_TYPE_VARIANTS = {
    "unsubscribed": {"lead.unsubscribed", "unsubscribed", "lead_unsubscribed", "optout"},
    "bounced_hard": {"lead.bounced_hard", "hard_bounce", "bounced.hard"},
    "replied":      {"lead.replied", "replied", "reply.received"},
    "complained":   {"lead.complained", "spam_complaint", "complaint", "lead.complained_spam"},
}

# Auth shared with the rest of the codebase
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
# Suppression helpers (private, used by both webhook endpoints)
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
# ReachInbox webhook auth — query-param shared secret (constant-time compare)
# ─────────────────────────────────────────────────────────────────────────────

def _verify_query_secret(provided: Optional[str], expected: str) -> bool:
    """
    Constant-time comparison of the ?secret=... query param against the
    configured shared secret. Both sides must be non-empty — fail-closed
    when REACHINBOX_WEBHOOK_SECRET is unset (won't accidentally accept
    empty=empty).
    """
    if not expected:
        return False        # secret env var unset → fail-closed
    if not provided:
        return False        # query param missing or empty
    try:
        return hmac.compare_digest(expected, provided)
    except Exception:
        return False


def _canonicalize_event(raw_event: str, body: dict) -> Optional[str]:
    """Map a vendor event-type string to one of our canonical event names."""
    if not raw_event:
        return None
    e = raw_event.strip().lower()

    # Special case: "lead.bounced" qualifies as bounced_hard ONLY if
    # bounce_type=hard appears in the payload. Soft bounces are ignored.
    if e == "lead.bounced":
        bt = (
            (body.get("bounce_type")
             or body.get("data", {}).get("bounce_type")
             or "")
            .strip().lower()
        )
        return "bounced_hard" if bt == "hard" else None

    for canonical, variants in EVENT_TYPE_VARIANTS.items():
        if e in {v.lower() for v in variants}:
            return canonical
    return None


def _extract_phone_email(body: dict) -> tuple[Optional[str], Optional[str]]:
    """Pull phone + email defensively from common payload locations."""
    lead = body.get("lead") or {}
    data = body.get("data") or {}
    phone = lead.get("phone") or data.get("phone") or body.get("phone")
    email = lead.get("email") or data.get("email") or body.get("email")
    return phone, email


def _extract_reply_text(body: dict) -> Optional[str]:
    reply = body.get("reply") or {}
    return (
        reply.get("text")
        or reply.get("body")
        or body.get("reply_text")
        or body.get("text")
        or body.get("body")
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /dnc/webhooks/reachinbox
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/dnc/webhooks/reachinbox")
async def reachinbox_webhook(request: Request):
    """
    Inbound webhook from ReachInbox. Authenticated via ?secret=<...> query
    param (constant-time compare against REACHINBOX_WEBHOOK_SECRET).

    Example URL format:
      https://kjle-api.onrender.com/kjle/v1/dnc/webhooks/reachinbox?secret=<REACHINBOX_WEBHOOK_SECRET>

    Always returns 200 once the secret is verified — even on unknown event
    types or missing data — so ReachInbox doesn't loop on retries. Detailed
    diagnostics land in dnc_audit_log for postmortem.
    """
    body_bytes = await request.body()
    provided_secret = request.query_params.get("secret")
    expected_secret = settings.REACHINBOX_WEBHOOK_SECRET or ""

    if not _verify_query_secret(provided_secret, expected_secret):
        # Fail-closed. Log security event (no audit row — we don't know who sent this).
        # Intentionally NOT logging the secret value or its length.
        logger.warning(
            f"dnc_webhooks: ReachInbox secret INVALID — provided_present={bool(provided_secret)}, "
            f"secret_configured={bool(expected_secret)}, body_len={len(body_bytes)}"
        )
        raise HTTPException(status_code=401, detail="invalid_secret")

    # Parse JSON manually (we already consumed the raw body above for body_len logging)
    try:
        body = json.loads(body_bytes.decode("utf-8") or "{}") if body_bytes else {}
    except Exception as e:
        logger.error(f"dnc_webhooks: ReachInbox payload not JSON: {e}")
        _audit(phone=None, email=None, source="reachinbox", result="error",
               error=f"payload_not_json: {e}")
        return {"status": "received", "processed": False, "reason": "payload_not_json"}

    raw_event = (
        body.get("event")
        or body.get("type")
        or body.get("eventType")
        or ""
    )
    canonical = _canonicalize_event(raw_event, body)
    phone, email = _extract_phone_email(body)

    # Unknown event type — record and ack (don't 500)
    if canonical is None:
        _audit(
            phone=None, email=None, source="reachinbox",
            result="webhook_unknown_event",
            metadata={"raw_event": raw_event, "phone_present": bool(phone), "email_present": bool(email)},
        )
        return {"status": "received", "processed": False, "reason": "unknown_event_type", "raw_event": raw_event}

    summary = {
        "phone_added":   False,
        "email_added":   False,
        "reason":        f"reachinbox_{canonical}",
        "canonical_event": canonical,
    }

    # Reason mapping for the suppression record
    reason_text = {
        "unsubscribed": "reachinbox_unsubscribe",
        "bounced_hard": "reachinbox_bounced_hard",
        "replied":      "reachinbox_replied_unsubscribe",
        "complained":   "reachinbox_spam_complaint",
    }[canonical]

    base_metadata = {"raw_event": raw_event, "canonical_event": canonical}

    # Per-event handling
    if canonical == "unsubscribed" or canonical == "complained":
        if phone:
            r = await _add_phone_suppression(phone, reason_text, "reachinbox",
                                              metadata={**base_metadata})
            summary["phone_added"] = bool(r.get("added"))
        if email:
            r = await _add_email_suppression(email, reason_text, "reachinbox",
                                              metadata={**base_metadata})
            summary["email_added"] = bool(r.get("added"))

    elif canonical == "bounced_hard":
        # Hard bounce only suppresses the email — phone is unaffected.
        if email:
            r = await _add_email_suppression(email, reason_text, "reachinbox",
                                              metadata={**base_metadata})
            summary["email_added"] = bool(r.get("added"))

    elif canonical == "replied":
        reply_text = _extract_reply_text(body) or ""
        is_unsub, kw = is_unsubscribe_reply(reply_text)
        summary["reply_text_present"] = bool(reply_text)
        summary["matched_keyword"] = kw
        if is_unsub:
            md = {**base_metadata, "matched_keyword": kw, "reply_excerpt": reply_text[:200]}
            if phone:
                r = await _add_phone_suppression(phone, reason_text, "reachinbox",
                                                  metadata=md)
                summary["phone_added"] = bool(r.get("added"))
            if email:
                r = await _add_email_suppression(email, reason_text, "reachinbox",
                                                  metadata=md)
                summary["email_added"] = bool(r.get("added"))
        else:
            # Non-unsubscribe reply — log it for audit, don't suppress
            _audit(phone=None, email=None, source="reachinbox",
                   result="webhook_skipped",
                   metadata={**base_metadata, "reason": "reply_no_unsubscribe_match",
                             "reply_excerpt": reply_text[:200]})

    suppressions_added = int(summary.get("phone_added", False)) + int(summary.get("email_added", False))
    logger.info(
        f"dnc_webhooks: ReachInbox event processed — type={canonical}, "
        f"source=reachinbox, suppressions_added={suppressions_added}"
    )

    return {"status": "received", "processed": True, **summary}


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
