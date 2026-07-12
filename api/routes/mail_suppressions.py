"""
KJLE — Mail (physical address) suppression endpoints
File: api/routes/mail_suppressions.py

Routes (PREFIX /kjle/v1 added by main.py):
  POST /mail-suppressions/add          — auth, add a physical-address suppression
  GET  /mail-suppressions/check        — auth, check by address+zip (or lead_id); FAIL CLOSED
  POST /mail-suppressions/check-batch  — auth, batch check (≤500)
  GET  /mail-suppressions/list         — auth, paginated browse

Compliance note:
  GET /mail-suppressions/check FAILS CLOSED on DB read error — it returns
  suppressed=true with metadata.lookup_error rather than green-lighting a mailing.
  A do-not-mail check that cannot verify must not permit delivery.

Audit note:
  The DNC audit mechanism writes to dnc_audit_log (a DNC-specific table) and is
  not reused here. Caller identity is carried by the required `source` field on
  every request row, which is persisted in mail_suppressions.source. No new
  audit infra was built — the source column IS the audit trail.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Auth ─────────────────────────────────────────────────────────────────────
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Constants ─────────────────────────────────────────────────────────────────
CHECK_BATCH_MAX   = 500
LIST_LIMIT_MAX    = 1000


# ── Pydantic models ───────────────────────────────────────────────────────────

class AddMailSuppressionRequest(BaseModel):
    address:  str
    zip:      str
    lead_id:  Optional[str] = None
    reason:   Optional[str] = None
    source:   str
    notes:    Optional[str] = None


class BatchItem(BaseModel):
    address: str
    zip:     str
    lead_id: Optional[str] = None


class CheckBatchRequest(BaseModel):
    source: str
    items:  list[BatchItem]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_address(address: str, zip_code: str) -> str:
    """Normalize to a stable suppression key: lowercased street + 5-digit zip, alnum only."""
    street = re.sub(r"[^a-z0-9]", "", (address or "").lower())
    z = re.sub(r"\D", "", (zip_code or ""))[:5]
    return f"{street}:{z}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── POST /mail-suppressions/add ───────────────────────────────────────────────

@router.post("/mail-suppressions/add")
async def mail_suppression_add(
    body: AddMailSuppressionRequest,
    x_api_key: str = Header(...),
):
    verify_api_key(x_api_key)

    addr_norm = _normalize_address(body.address, body.zip)
    if not addr_norm or addr_norm == ":":
        raise HTTPException(status_code=400, detail="address and zip produced an empty normalization key")

    db = get_db()

    # Idempotency check — don't double-insert the same normalized address.
    try:
        existing = (
            db.table("mail_suppressions")
            .select("id")
            .eq("address_normalized", addr_norm)
            .limit(1)
            .execute()
        )
        already_existed = bool(existing.data)
    except Exception as e:
        logger.error(f"mail_suppressions: existence check failed for {addr_norm}: {e}")
        raise HTTPException(status_code=500, detail=f"suppression_check_failed: {e}")

    if not already_existed:
        payload = {
            "address_normalized": addr_norm,
            "lead_id":            body.lead_id,
            "reason":             body.reason,
            "source":             body.source,
            "suppressed_at":      _now_iso(),
            "notes":              body.notes,
        }
        try:
            db.table("mail_suppressions").insert(payload).execute()
        except Exception as e:
            logger.error(f"mail_suppressions: insert failed for {addr_norm}: {e}")
            raise HTTPException(status_code=500, detail=f"suppression_write_failed: {e}")

    return {
        "suppressed":       True,
        "address_normalized": addr_norm,
        "already_existed":  already_existed,
        "source":           body.source,
    }


# ── GET /mail-suppressions/check ─────────────────────────────────────────────

@router.get("/mail-suppressions/check")
async def mail_suppression_check(
    x_api_key: str           = Header(...),
    address:   Optional[str] = Query(None, description="Street address"),
    zip:       Optional[str] = Query(None, alias="zip", description="ZIP code"),
    lead_id:   Optional[str] = Query(None, description="Optional: check by lead UUID"),
):
    verify_api_key(x_api_key)

    if not address and not lead_id:
        raise HTTPException(
            status_code=400,
            detail="provide address+zip or lead_id",
        )

    addr_norm: Optional[str] = None
    if address is not None:
        addr_norm = _normalize_address(address, zip or "")

    db = get_db()

    try:
        q = db.table("mail_suppressions").select("address_normalized, reason, source, lead_id")

        if addr_norm:
            q = q.eq("address_normalized", addr_norm)
        elif lead_id:
            q = q.eq("lead_id", lead_id)

        res = q.limit(1).execute()
        row = res.data[0] if res.data else None

    except Exception as e:
        # FAIL CLOSED — cannot verify suppression status → must not permit mailing.
        logger.error(f"mail_suppressions: check read failed for {addr_norm or lead_id}: {e}")
        return {
            "suppressed":         True,
            "address_normalized": addr_norm,
            "reason":             None,
            "source":             None,
            "metadata":           {"lookup_error": str(e)[:200]},
        }

    if row:
        return {
            "suppressed":         True,
            "address_normalized": row.get("address_normalized") or addr_norm,
            "reason":             row.get("reason"),
            "source":             row.get("source"),
        }

    return {
        "suppressed":         False,
        "address_normalized": addr_norm,
        "reason":             None,
        "source":             None,
    }


# ── POST /mail-suppressions/check-batch ──────────────────────────────────────

@router.post("/mail-suppressions/check-batch")
async def mail_suppression_check_batch(
    body: CheckBatchRequest,
    x_api_key: str = Header(...),
):
    verify_api_key(x_api_key)

    if not body.items:
        raise HTTPException(status_code=400, detail="items list is empty")
    if len(body.items) > CHECK_BATCH_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"batch too large: {len(body.items)} > {CHECK_BATCH_MAX}",
        )

    db = get_db()

    # Build a set of normalized keys for a single bulk query (one DB round-trip).
    item_keys = [
        (_normalize_address(item.address, item.zip), item)
        for item in body.items
    ]
    all_keys = [k for k, _ in item_keys]

    try:
        res = (
            db.table("mail_suppressions")
            .select("address_normalized, reason, source, lead_id")
            .in_("address_normalized", all_keys)
            .execute()
        )
        suppressed_keys: set[str] = {row["address_normalized"] for row in (res.data or [])}
        suppressed_rows_map: dict[str, dict] = {
            row["address_normalized"]: row for row in (res.data or [])
        }
    except Exception as e:
        # FAIL CLOSED — on bulk DB error, treat all items as suppressed.
        logger.error(f"mail_suppressions: batch check read failed: {e}")
        suppressed_items = [
            {
                "address":            item.address,
                "zip":                item.zip,
                "lead_id":            item.lead_id,
                "address_normalized": key,
                "lookup_error":       str(e)[:200],
            }
            for key, item in item_keys
        ]
        return {
            "checked":     len(body.items),
            "suppressed":  suppressed_items,
            "clean_count": 0,
        }

    suppressed_items = []
    for key, item in item_keys:
        if key in suppressed_keys:
            matched = suppressed_rows_map.get(key, {})
            suppressed_items.append({
                "address":            item.address,
                "zip":                item.zip,
                "lead_id":            item.lead_id,
                "address_normalized": key,
                "reason":             matched.get("reason"),
                "source":             matched.get("source"),
            })

    return {
        "checked":     len(body.items),
        "suppressed":  suppressed_items,
        "clean_count": len(body.items) - len(suppressed_items),
    }


# ── GET /mail-suppressions/list ───────────────────────────────────────────────

@router.get("/mail-suppressions/list")
async def mail_suppression_list(
    x_api_key: str = Header(...),
    limit:     int = Query(100, ge=1),
    offset:    int = Query(0,   ge=0),
):
    verify_api_key(x_api_key)

    if limit > LIST_LIMIT_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"limit_too_large: {limit} > {LIST_LIMIT_MAX}",
        )

    db = get_db()

    try:
        rows_res = (
            db.table("mail_suppressions")
            .select("address_normalized, reason, source, suppressed_at")
            .order("suppressed_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        rows = rows_res.data or []
    except Exception as e:
        logger.error(f"mail_suppression_list: rows fetch failed: {e}")
        raise HTTPException(status_code=500, detail=f"list_read_failed: {e}")

    try:
        total = (
            db.table("mail_suppressions")
            .select("id", count="exact")
            .limit(1)
            .execute()
            .count or 0
        )
    except Exception:
        total = 0

    return {
        "total":  total,
        "limit":  limit,
        "offset": offset,
        "rows":   rows,
    }
