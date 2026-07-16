"""
KJLE — Mode 3: Re-Verify
File: api/routes/reverify.py

Re-scans a selected batch of leads and reports which WebSignalz signals changed
since the last audit. Use before campaign sends to confirm targets still have
the accessibility/quality problems we will pitch.

Auth: x-api-key header (same key as POST /kjle/v1/leads/mailable).
robots.txt: NOT applied here. Mode 3 is an operator-initiated targeted re-scan of
leads already held in the system — not unsolicited bulk crawling.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..database import get_db
from .leads import verify_api_key
from .website_audit import (
    _fetch_html_free as wa_fetch_html_free,
    _parse_signals_full as wa_parse_signals_full,
    _FULL_AUDIT_COLUMNS as WA_FULL_AUDIT_COLUMNS,
)

logger = logging.getLogger(__name__)
router = APIRouter()

REVERIFY_MAX         = 500
REVERIFY_CONCURRENCY = 10

# Signal columns to compare (excludes last_audited_at itself)
_SIGNAL_COLUMNS = sorted(WA_FULL_AUDIT_COLUMNS - {"last_audited_at"})


class ReverifyRequest(BaseModel):
    lead_ids: List[str]
    source:   Optional[str] = None


@router.post("/reverify")
async def reverify(body: ReverifyRequest, _auth=Depends(verify_api_key)):
    """
    Mode 3: Re-Verify — re-scan a batch of leads, write fresh signals, report what changed.

    Reuses wa_fetch_html_free + wa_parse_signals_full (single source of truth for parsing).
    Max 500 leads per call. Concurrency capped at 10. robots.txt does NOT gate this endpoint.

    Response:
      {"checked": int, "changed": int, "unreachable": int,
       "leads": [{"lead_id": uuid, "status": "ok"|"unreachable"|"error",
                  "changed_fields": [{"field": str, "old": any, "new": any}]}]}
    """
    if not body.lead_ids:
        raise HTTPException(status_code=400, detail="lead_ids must not be empty")
    if len(body.lead_ids) > REVERIFY_MAX:
        raise HTTPException(status_code=400, detail=f"lead_ids must be <= {REVERIFY_MAX}")

    db = get_db()

    select_cols = "id, website, last_audited_at, " + ", ".join(_SIGNAL_COLUMNS)
    rows = (
        db.table("leads")
        .select(select_cols)
        .in_("id", body.lead_ids)
        .execute()
    ).data or []

    by_id = {r["id"]: r for r in rows}

    checked     = 0
    changed_ct  = 0
    unreachable = 0
    results: list = []
    lock        = asyncio.Lock()
    sem         = asyncio.Semaphore(REVERIFY_CONCURRENCY)

    async def _reverify_one(lead_id: str) -> None:
        nonlocal checked, changed_ct, unreachable

        now_iso = datetime.now(timezone.utc).isoformat()
        row     = by_id.get(lead_id)

        if row is None:
            async with lock:
                checked += 1
            results.append({"lead_id": lead_id, "status": "error", "changed_fields": []})
            return

        website = (row.get("website") or "").strip()

        if not website:
            try:
                db.table("leads").update({"last_audited_at": now_iso}).eq("id", lead_id).execute()
            except Exception:
                pass
            async with lock:
                checked     += 1
                unreachable += 1
            results.append({"lead_id": lead_id, "status": "unreachable", "changed_fields": []})
            return

        try:
            html, final_url = await wa_fetch_html_free(website)

            if html is None:
                try:
                    db.table("leads").update({
                        "is_parked":       True,
                        "website_has_ssl": False,
                        "last_audited_at": now_iso,
                    }).eq("id", lead_id).execute()
                except Exception:
                    pass
                async with lock:
                    checked     += 1
                    unreachable += 1
                results.append({"lead_id": lead_id, "status": "unreachable", "changed_fields": []})
                return

            new_signals = wa_parse_signals_full(html, final_url)
            safe_new    = {k: v for k, v in new_signals.items() if k in WA_FULL_AUDIT_COLUMNS}

            changed_fields: list = []
            for field in _SIGNAL_COLUMNS:
                old_val = row.get(field)
                new_val = safe_new.get(field)
                if old_val != new_val:
                    changed_fields.append({"field": field, "old": old_val, "new": new_val})

            safe_new["last_audited_at"] = now_iso
            try:
                db.table("leads").update(safe_new).eq("id", lead_id).execute()
            except Exception as e:
                logger.error(f"[reverify] DB write failed for {lead_id}: {e}")

            async with lock:
                checked += 1
                if changed_fields:
                    changed_ct += 1
            results.append({"lead_id": lead_id, "status": "ok", "changed_fields": changed_fields})

        except Exception as e:
            logger.error(f"[reverify] Unexpected error for {lead_id}: {type(e).__name__}: {e}")
            try:
                db.table("leads").update({"last_audited_at": now_iso}).eq("id", lead_id).execute()
            except Exception:
                pass
            async with lock:
                checked += 1
            results.append({"lead_id": lead_id, "status": "error", "changed_fields": []})

    async def _gated(lead_id: str) -> None:
        async with sem:
            await _reverify_one(lead_id)

    await asyncio.gather(*[_gated(lid) for lid in body.lead_ids])

    return {
        "checked":     checked,
        "changed":     changed_ct,
        "unreachable": unreachable,
        "leads":       results,
    }
