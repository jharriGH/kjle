"""
Webhook System
KJLE - King James Lead Empire
Routes: /kjle/v1/webhooks/*

Registers, manages, and fires outbound webhooks for KJLE events.
Supports HMAC-SHA256 payload signing via per-webhook secrets.

Public export: fire_event(event_type, payload) — importable by any route module.
"""

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SUPPORTED_EVENTS = {
    "lead.hot_classified",
    "lead.enriched",
    "batch.push_complete",
    "export.csv_complete",
    "export.reachinbox_complete",
    "segment.classified",
}

WEBHOOK_TIMEOUT = 10.0      # seconds per outbound call
WEBHOOK_TABLE   = "webhooks"


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class WebhookRegisterRequest(BaseModel):
    name: str
    url: str
    event_type: str
    secret: Optional[str] = None


class WebhookUpdateRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    is_active: Optional[bool] = None
    secret: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_event_type(event_type: str) -> None:
    if event_type not in SUPPORTED_EVENTS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unsupported event_type '{event_type}'. "
                f"Supported: {sorted(SUPPORTED_EVENTS)}"
            ),
        )


def _sign_payload(payload_bytes: bytes, secret: str) -> str:
    """Returns HMAC-SHA256 hex digest of the payload using the webhook secret."""
    return hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()


def _build_headers(payload_bytes: bytes, secret: Optional[str]) -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": "KJLE-Webhook/1.0"}
    if secret:
        sig = _sign_payload(payload_bytes, secret)
        headers["X-KJLE-Signature"] = f"sha256={sig}"
    return headers


async def _post_webhook(
    client: httpx.AsyncClient,
    url: str,
    payload_bytes: bytes,
    secret: Optional[str],
) -> tuple[bool, int, str]:
    """
    POSTs a webhook payload to a URL.
    Returns (success: bool, status_code: int, error_msg: str).
    """
    headers = _build_headers(payload_bytes, secret)
    try:
        resp = await client.post(url, content=payload_bytes, headers=headers)
        success = resp.status_code < 400
        return success, resp.status_code, "" if success else resp.text[:200]
    except httpx.TimeoutException:
        return False, 0, "timeout"
    except Exception as e:
        return False, 0, f"{type(e).__name__}: {str(e)[:200]}"


async def _update_webhook_meta(
    db,
    webhook_id: str,
    success: bool,
    status_code: int,
) -> None:
    """Updates last_fired_at, fire_count, and last_status on the webhook record."""
    try:
        # Increment fire_count via fetch-then-update (Supabase client has no atomic increment)
        current = (
            db.table(WEBHOOK_TABLE)
            .select("fire_count")
            .eq("id", webhook_id)
            .single()
            .execute()
            .data or {}
        )
        new_count = (current.get("fire_count") or 0) + 1
        db.table(WEBHOOK_TABLE).update({
            "last_fired_at": _now_iso(),
            "fire_count":    new_count,
            "last_status":   f"HTTP {status_code}" if status_code else ("timeout" if not success else "ok"),
        }).eq("id", webhook_id).execute()
    except Exception as e:
        logger.error(f"Failed to update webhook meta for {webhook_id}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC: fire_event — importable by other route modules
# ─────────────────────────────────────────────────────────────────────────────

async def fire_event(event_type: str, payload: dict) -> dict:
    """
    Fires all active webhooks registered for event_type.
    POSTs JSON payload to each URL. Signs with HMAC if secret is set.
    Updates last_fired_at, fire_count, last_status per webhook.

    Returns summary: {fired, failed, skipped_inactive, event_type}

    Usage from other modules:
        from .webhooks import fire_event
        await fire_event("lead.hot_classified", {"lead_id": "...", ...})
    """
    db = get_db()

    webhooks = (
        db.table(WEBHOOK_TABLE)
        .select("id, url, secret, is_active")
        .eq("event_type", event_type)
        .eq("is_active", True)
        .execute()
        .data or []
    )

    if not webhooks:
        return {"fired": 0, "failed": 0, "skipped_inactive": 0, "event_type": event_type}

    full_payload = {
        "event":      event_type,
        "fired_at":   _now_iso(),
        "source":     "kjle",
        "data":       payload,
    }
    payload_bytes = json.dumps(full_payload, default=str).encode("utf-8")

    fired = 0
    failed = 0

    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
        for wh in webhooks:
            wh_id  = wh["id"]
            url    = wh.get("url", "")
            secret = wh.get("secret") or None

            if not url:
                failed += 1
                continue

            success, status_code, err = await _post_webhook(client, url, payload_bytes, secret)

            if success:
                fired += 1
            else:
                failed += 1
                logger.warning(f"Webhook {wh_id} fire failed → {url}: {err or status_code}")

            await _update_webhook_meta(db, wh_id, success, status_code)

    return {
        "fired":            fired,
        "failed":           failed,
        "skipped_inactive": 0,
        "event_type":       event_type,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /webhooks/register
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/webhooks/register")
async def register_webhook(body: WebhookRegisterRequest):
    """
    Register a new webhook for a supported KJLE event type.
    """
    _validate_event_type(body.event_type)

    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=422,
            detail="url must start with http:// or https://",
        )

    db = get_db()

    now = _now_iso()
    payload = {
        "name":        body.name,
        "url":         body.url,
        "event_type":  body.event_type,
        "secret":      body.secret,
        "is_active":   True,
        "fire_count":  0,
        "created_at":  now,
    }

    result = db.table(WEBHOOK_TABLE).insert(payload).execute()
    created = result.data[0] if result.data else payload

    return {
        "status":  "success",
        "message": f"Webhook '{body.name}' registered for event '{body.event_type}'",
        "webhook": created,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /webhooks/list
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/webhooks/list")
async def list_webhooks(
    event_type: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
):
    """
    List all webhooks, sorted by created_at desc.
    Optionally filter by event_type or is_active status.
    """
    db = get_db()

    query = (
        db.table(WEBHOOK_TABLE)
        .select("id, name, url, event_type, is_active, fire_count, last_fired_at, last_status, created_at")
        .order("created_at", desc=True)
    )

    if event_type:
        query = query.eq("event_type", event_type)
    if is_active is not None:
        query = query.eq("is_active", is_active)

    webhooks = query.execute().data or []

    return {
        "status":  "success",
        "filters": {"event_type": event_type, "is_active": is_active},
        "count":   len(webhooks),
        "supported_events": sorted(SUPPORTED_EVENTS),
        "webhooks": webhooks,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /webhooks/{webhook_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/webhooks/{webhook_id}")
async def update_webhook(webhook_id: str, body: WebhookUpdateRequest):
    """
    Update webhook name, URL, active status, or signing secret.
    """
    db = get_db()

    existing = (
        db.table(WEBHOOK_TABLE)
        .select("*")
        .eq("id", webhook_id)
        .single()
        .execute()
        .data
    )
    if not existing:
        raise HTTPException(status_code=404, detail=f"Webhook '{webhook_id}' not found")

    if body.url and not body.url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=422,
            detail="url must start with http:// or https://",
        )

    update_payload: dict = {}
    if body.name      is not None: update_payload["name"]      = body.name
    if body.url       is not None: update_payload["url"]       = body.url
    if body.is_active is not None: update_payload["is_active"] = body.is_active
    if body.secret    is not None: update_payload["secret"]    = body.secret

    if not update_payload:
        return {"status": "success", "message": "No changes provided", "webhook": existing}

    db.table(WEBHOOK_TABLE).update(update_payload).eq("id", webhook_id).execute()

    updated = (
        db.table(WEBHOOK_TABLE)
        .select("*")
        .eq("id", webhook_id)
        .single()
        .execute()
        .data
    )

    return {
        "status":  "success",
        "webhook": updated,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /webhooks/{webhook_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: str):
    """
    Soft-deletes a webhook by setting is_active=false.
    The record is preserved for audit/history.
    """
    db = get_db()

    existing = (
        db.table(WEBHOOK_TABLE)
        .select("id, name, is_active")
        .eq("id", webhook_id)
        .single()
        .execute()
        .data
    )
    if not existing:
        raise HTTPException(status_code=404, detail=f"Webhook '{webhook_id}' not found")

    if not existing.get("is_active"):
        return {
            "status":     "success",
            "message":    f"Webhook '{existing.get('name')}' was already inactive",
            "webhook_id": webhook_id,
        }

    db.table(WEBHOOK_TABLE).update({"is_active": False}).eq("id", webhook_id).execute()

    return {
        "status":     "success",
        "message":    f"Webhook '{existing.get('name')}' has been deactivated (soft delete)",
        "webhook_id": webhook_id,
        "name":       existing.get("name"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /webhooks/fire/{event_type}
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/webhooks/fire/{event_type}")
async def fire_event_endpoint(event_type: str, payload: dict = {}):
    """
    Manually fire all active webhooks for a given event_type.
    Accepts any dict as the payload body. Useful for testing integrations.
    Returns summary: {fired, failed, skipped_inactive, event_type}.
    """
    _validate_event_type(event_type)

    db = get_db()

    # Count inactive webhooks for the summary
    inactive_count = (
        db.table(WEBHOOK_TABLE)
        .select("id", count="exact")
        .eq("event_type", event_type)
        .eq("is_active", False)
        .execute()
        .count or 0
    )

    result = await fire_event(event_type, payload)
    result["skipped_inactive"] = inactive_count

    return {
        "status": "success",
        **result,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /webhooks/test/{webhook_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/webhooks/test/{webhook_id}")
async def test_webhook(webhook_id: str):
    """
    Sends a test ping to a specific webhook URL with a sample payload.
    Returns: {success, status_code, response_ms}.
    Does NOT update fire_count or last_fired_at — this is a test only.
    """
    db = get_db()

    wh = (
        db.table(WEBHOOK_TABLE)
        .select("id, name, url, event_type, secret, is_active")
        .eq("id", webhook_id)
        .single()
        .execute()
        .data
    )
    if not wh:
        raise HTTPException(status_code=404, detail=f"Webhook '{webhook_id}' not found")

    url    = wh.get("url", "")
    secret = wh.get("secret") or None

    if not url:
        raise HTTPException(status_code=422, detail="Webhook has no URL configured")

    test_payload = {
        "event":    wh.get("event_type", "test"),
        "fired_at": _now_iso(),
        "source":   "kjle",
        "test":     True,
        "data": {
            "message":    "This is a test ping from KJLE Webhook System",
            "webhook_id": webhook_id,
            "name":       wh.get("name"),
        },
    }
    payload_bytes = json.dumps(test_payload, default=str).encode("utf-8")

    start_ms = time.monotonic()
    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
        success, status_code, error = await _post_webhook(client, url, payload_bytes, secret)
    elapsed_ms = round((time.monotonic() - start_ms) * 1000, 2)

    return {
        "status":      "success" if success else "failed",
        "webhook_id":  webhook_id,
        "name":        wh.get("name"),
        "url":         url,
        "success":     success,
        "status_code": status_code,
        "response_ms": elapsed_ms,
        "error":       error or None,
        "signed":      bool(secret),
    }
