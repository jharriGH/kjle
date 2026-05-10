"""
KJLE — Truelist webhook receiver
Route prefix handled in main.py: /kjle/v1

Receives batch-completion notifications from Truelist. Per investigation
2026-05-10, Truelist's webhook configuration is dashboard-side (no
discoverable per-batch webhook_url field — POST silently accepts but
ignores it). When the webhook is configured at the Truelist dashboard
side, this endpoint accepts the callback, persists the latest state to
truelist_batches, and (if state=completed) kicks off CSV ingestion.

Until the dashboard webhook is wired, the poller in scheduler.py
(job_email_clean_poll_batches) handles result ingestion every 30 min —
this receiver is a ready-to-accept shim, not a blocker.

Auth: ?secret=<TRUELIST_WEBHOOK_SECRET> query parameter, constant-time
compared against settings.TRUELIST_WEBHOOK_SECRET. Modeled on the
ReachInbox pattern in dnc_webhooks.py — URL secrecy IS the boundary.
Fail-closed when the env var is unset (no empty=empty acceptance).

Expected Truelist payload shape (assumed; verify when configured):
  {"id": "<uuid>", "batch_state": "completed", "email_count": N, ...}
We're defensive — pull batch_id from any of: id, batch_id, data.id.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..config import settings
from ..database import get_db
from .enrichment_email_clean import (
    _get_truelist_api_key,
    ingest_batch_result,
    poll_batch,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _verify_secret(provided: Optional[str], expected: str) -> bool:
    """Constant-time secret compare. Fail-closed on empty/missing."""
    if not expected or not provided:
        return False
    try:
        return hmac.compare_digest(expected, provided)
    except Exception:
        return False


def _extract_batch_id(body: dict) -> Optional[str]:
    """Defensive: try several common payload shapes."""
    if not isinstance(body, dict):
        return None
    return (
        body.get("id")
        or body.get("batch_id")
        or (body.get("data") or {}).get("id")
        or (body.get("batch") or {}).get("id")
    )


@router.post("/webhooks/truelist/batch-complete")
async def truelist_batch_complete(request: Request):
    """
    Truelist batch-completion webhook. Always returns 200 once auth passes —
    even on unknown payload shapes — so Truelist won't loop on retries.
    """
    body_bytes = await request.body()
    provided = request.query_params.get("secret")
    expected = getattr(settings, "TRUELIST_WEBHOOK_SECRET", "") or ""

    if not _verify_secret(provided, expected):
        logger.warning(
            f"webhooks_truelist: secret INVALID — provided_present={bool(provided)}, "
            f"secret_configured={bool(expected)}, body_len={len(body_bytes)}"
        )
        raise HTTPException(status_code=401, detail="invalid_secret")

    try:
        body = json.loads(body_bytes.decode("utf-8") or "{}") if body_bytes else {}
    except Exception as e:
        logger.error(f"webhooks_truelist: payload not JSON: {e}")
        return {"status": "received", "processed": False, "reason": "payload_not_json"}

    batch_id = _extract_batch_id(body)
    state    = body.get("batch_state") or (body.get("data") or {}).get("batch_state")

    if not batch_id:
        logger.warning(f"webhooks_truelist: no batch id in payload: {str(body)[:300]}")
        return {"status": "received", "processed": False, "reason": "no_batch_id"}

    db = get_db()

    # Look up batch row — if we don't recognize this id, just record and ack
    res = db.table("truelist_batches").select("id, status").eq("id", batch_id).execute()
    if not res.data:
        logger.warning(f"webhooks_truelist: unknown batch_id {batch_id} (not in truelist_batches)")
        return {"status": "received", "processed": False, "reason": "unknown_batch_id", "batch_id": batch_id}

    # Re-poll Truelist for the authoritative state + CSV URLs (Truelist
    # webhook payload may not include URLs — and we want fresh state anyway).
    try:
        api_key = _get_truelist_api_key(db)
        raw = await poll_batch(api_key, batch_id)
    except Exception as e:
        logger.error(f"webhooks_truelist: re-poll failed for {batch_id}: {e}")
        return {"status": "received", "processed": False, "reason": "repoll_failed", "error": str(e)}

    # Persist
    real_state = raw.get("batch_state") or state
    try:
        db.table("truelist_batches").update({
            "status":                    real_state,
            "annotated_csv_url":         raw.get("annotated_csv_url"),
            "safest_bet_csv_url":        raw.get("safest_bet_csv_url"),
            "highest_reach_csv_url":     raw.get("highest_reach_csv_url"),
            "only_invalid_csv_url":      raw.get("only_invalid_csv_url"),
            "ok_count":                  raw.get("ok_count"),
            "ok_for_all_count":          raw.get("ok_for_all_count"),
            "role_count":                raw.get("role_count"),
            "disposable_count":          raw.get("disposable_count"),
            "failed_syntax_check_count": raw.get("failed_syntax_check_count"),
            "failed_mx_check_count":     raw.get("failed_mx_check_count"),
            "failed_no_mailbox_count":   raw.get("failed_no_mailbox_count"),
        }).eq("id", batch_id).execute()
    except Exception as e:
        logger.warning(f"webhooks_truelist: persist failed (non-fatal) for {batch_id}: {e}")

    # If completed, ingest synchronously (idempotent — already-ingested fast-paths)
    if real_state == "completed":
        try:
            ingest = await ingest_batch_result(db, api_key, batch_id)
            return {"status": "received", "processed": True, "batch_id": batch_id, "ingest": ingest}
        except Exception as e:
            logger.error(f"webhooks_truelist: ingest failed for {batch_id}: {e}")
            return {"status": "received", "processed": False, "batch_id": batch_id, "error": str(e)}

    return {"status": "received", "processed": True, "batch_id": batch_id, "state": real_state}
