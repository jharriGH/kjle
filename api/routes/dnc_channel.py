"""
KJLE — Channel-aware DNC routes (Phase 4 Layer 1, Slice 1C)
File: api/routes/dnc_channel.py

Routes (PREFIX /kjle/v1 added by main.py):
  POST  /dnc/check-for-channel          — auth; batch channel-aware check
  GET   /dnc/fed-list/status            — auth; counts, last refresh
  POST  /dnc/fed-list/refresh           — auth; trigger refresh job (in-process)
  GET   /dnc/nanpa/status               — auth; totals + breakdown by line_type
  POST  /dnc/nanpa/refresh              — auth; trigger NANPA refresh job

Phase 4 Layer 1 design notes:
- /dnc/check-for-channel is the new entry point for callers that have a
  list of lead_ids and a target channel. Internally calls
  dedupe_and_check_phones (Slice 1B) for phone-based channels, which dedups
  by E.164, runs the free pre-filters (fed_dnc_list, NANPA line_type), and
  only pays Searchbug when free checks are inconclusive.
- The legacy /dnc/check/{phone} route (Phase 1) is NOT routed through here.
  Telehealth / AVA continue to call it directly until a later phase migrates
  them (Jim's 2026-05-24 decision).
- /fed-list and /nanpa /refresh endpoints invoke the scheduler job functions
  directly. Both are idempotent and safe to run on demand.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from ..database import get_db
from ..lib.dnc_batch import dedupe_and_check_phones
from ..lib.dnc_check import check_lead_for_channel, CHANNELS
from ..lib import carrier_lookup

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Auth ─────────────────────────────────────────────────────────────────────
API_SECRET_KEY = os.environ.get("API_SECRET_KEY")


def verify_api_key(x_api_key: str = Header(...)) -> None:
    if not API_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="server_misconfigured: API_SECRET_KEY env var unset",
        )
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# POST /dnc/check-for-channel
# ─────────────────────────────────────────────────────────────────────────────

class CheckForChannelRequest(BaseModel):
    lead_ids: list[str] = Field(..., min_length=1, max_length=500,
                                description="leads.id UUIDs to check")
    channel:  str       = Field(..., description="email | sms | voice | mail")
    source:   str       = Field("unknown",
                                description="audit tag identifying the caller")


@router.post("/dnc/check-for-channel")
async def dnc_check_for_channel_route(
    body: CheckForChannelRequest,
    x_api_key: str = Header(...),
):
    """
    Channel-aware DNC check for a batch of leads.

    For phone-based channels (sms, voice) the batch is deduped by
    normalized E.164 — one underlying check per unique phone, fanned out
    to all leads sharing that phone.

    For email/mail, each lead is checked individually (no dedup payoff).

    Max batch: 500 lead_ids. Response always 200 once auth passes — per-lead
    errors are surfaced inside blocked_leads / unknown_leads / no_phone_leads.

    Response shape:
        {
          "channel":               str,
          "source":                str,
          "total_leads_requested": int,
          "total_leads_covered":   int,
          "unique_phones_checked": int,
          "cost_usd":              float,
          "allowed_leads":         [lead_id, ...],
          "blocked_leads": [
            {"lead_id": str, "reason": str, "result_source": str}, ...
          ],
          "unknown_leads":  [lead_id, ...],   # not found in leads table
          "no_phone_leads": [lead_id, ...],   # only for phone channels
          "per_phone_results": [
            {"phone": str, "lead_ids": [...], "result": {...}}, ...
          ],
          "error": null | "..."
        }
    """
    verify_api_key(x_api_key)

    if body.channel not in CHANNELS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid channel: {body.channel}. Must be one of {CHANNELS}",
        )

    result = await dedupe_and_check_phones(
        body.lead_ids, body.channel, body.source
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Fed DNC list
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dnc/fed-list/status")
async def fed_dnc_status(x_api_key: str = Header(...)) -> dict:
    """
    Returns the size and freshness of the federal DNC mirror.

    Response:
        {
          "total_phones":     int,
          "last_imported_at": iso8601 | null,
          "newest_imported":  iso8601 | null,    # alias of last_imported_at
          "oldest_imported":  iso8601 | null
        }
    """
    verify_api_key(x_api_key)
    db = get_db()

    total = 0
    last_imported_at = None
    oldest_imported = None

    try:
        r = (db.table("fed_dnc_list").select("phone", count="exact")
             .limit(1).execute())
        total = r.count or 0
    except Exception as e:
        logger.warning("fed_dnc_status: count failed: %s", e)

    try:
        r = (db.table("fed_dnc_list").select("imported_at")
             .order("imported_at", desc=True).limit(1).execute())
        if r.data:
            last_imported_at = r.data[0].get("imported_at")
    except Exception as e:
        logger.warning("fed_dnc_status: last_imported_at query failed: %s", e)

    try:
        r = (db.table("fed_dnc_list").select("imported_at")
             .order("imported_at", desc=False).limit(1).execute())
        if r.data:
            oldest_imported = r.data[0].get("imported_at")
    except Exception as e:
        logger.warning("fed_dnc_status: oldest_imported query failed: %s", e)

    return {
        "total_phones":     total,
        "last_imported_at": last_imported_at,
        "newest_imported":  last_imported_at,
        "oldest_imported":  oldest_imported,
    }


@router.post("/dnc/fed-list/refresh")
async def fed_dnc_refresh(x_api_key: str = Header(...)) -> dict:
    """
    Trigger the federal DNC refresh job in-process.

    Calls the same job function the scheduler uses
    (api.routes.scheduler.job_fed_dnc_refresh_monthly). The job is
    idempotent and gracefully no-ops if the source file is not present
    on disk (e.g. FCC SAN approval still pending).

    Response: the job's own return dict.
    """
    verify_api_key(x_api_key)

    try:
        from .scheduler import job_fed_dnc_refresh_monthly  # type: ignore
    except Exception as e:
        logger.error("fed_dnc_refresh: scheduler import failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"scheduler_unavailable: {str(e)[:200]}",
        )

    try:
        return await job_fed_dnc_refresh_monthly()
    except Exception as e:
        logger.error("fed_dnc_refresh: job raised: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"job_failed: {type(e).__name__}: {str(e)[:200]}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# NANPA carrier prefix DB
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dnc/nanpa/status")
async def nanpa_status(x_api_key: str = Header(...)) -> dict:
    """
    Returns the size of the NANPA carrier prefix mirror plus an
    in-process lru_cache snapshot from carrier_lookup.

    Response:
        {
          "total_prefixes":     int,
          "by_line_type":       {"mobile": int, "landline": int, ...},
          "last_updated":       iso8601 | null,
          "in_process_cache":   {hits, misses, maxsize, currsize}
        }
    """
    verify_api_key(x_api_key)
    db = get_db()

    total = 0
    by_line_type: dict[str, int] = {}
    last_updated = None

    try:
        r = (db.table("nanpa_carrier_prefixes").select("npa_nxx_x", count="exact")
             .limit(1).execute())
        total = r.count or 0
    except Exception as e:
        logger.warning("nanpa_status: count failed: %s", e)

    try:
        # Supabase RPC for grouped counts is heavy; do client-side aggregation
        # over a select of line_type only. For million-row tables this works
        # because line_type is indexed and we only return that column.
        # NB: PostgREST caps result at 1000 rows by default — for NANPA's
        # ~200K prefixes we'd need to paginate. Use a smarter approach:
        # query each known line_type with a count.
        for lt in ("mobile", "landline", "voip", "unknown"):
            try:
                rr = (db.table("nanpa_carrier_prefixes")
                      .select("npa_nxx_x", count="exact")
                      .eq("line_type", lt).limit(1).execute())
                by_line_type[lt] = rr.count or 0
            except Exception as e:
                logger.warning(
                    "nanpa_status: line_type=%s count failed: %s", lt, e
                )
                by_line_type[lt] = 0
    except Exception as e:
        logger.warning("nanpa_status: by_line_type aggregation failed: %s", e)

    try:
        r = (db.table("nanpa_carrier_prefixes").select("last_updated")
             .order("last_updated", desc=True).limit(1).execute())
        if r.data:
            last_updated = r.data[0].get("last_updated")
    except Exception as e:
        logger.warning("nanpa_status: last_updated query failed: %s", e)

    return {
        "total_prefixes":   total,
        "by_line_type":     by_line_type,
        "last_updated":     last_updated,
        "in_process_cache": carrier_lookup.cache_info(),
    }


@router.post("/dnc/nanpa/refresh")
async def nanpa_refresh(x_api_key: str = Header(...)) -> dict:
    """
    Trigger the NANPA refresh job in-process.

    Calls api.routes.scheduler.job_nanpa_refresh_monthly. Idempotent;
    no-ops if the source file is not present.

    NB: clears the carrier_lookup in-process lru_cache before returning so
    subsequent lookups pick up fresh data.

    Response: the job's own return dict, plus 'in_process_cache_cleared': true.
    """
    verify_api_key(x_api_key)

    try:
        from .scheduler import job_nanpa_refresh_monthly  # type: ignore
    except Exception as e:
        logger.error("nanpa_refresh: scheduler import failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"scheduler_unavailable: {str(e)[:200]}",
        )

    try:
        result = await job_nanpa_refresh_monthly()
    except Exception as e:
        logger.error("nanpa_refresh: job raised: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"job_failed: {type(e).__name__}: {str(e)[:200]}",
        )

    try:
        carrier_lookup.clear_cache()
        result["in_process_cache_cleared"] = True
    except Exception as e:
        logger.warning("nanpa_refresh: cache clear failed: %s", e)
        result["in_process_cache_cleared"] = False

    return result
