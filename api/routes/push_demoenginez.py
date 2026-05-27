"""
DemoEnginez Push Integration
KJLE - King James Lead Empire
Routes: /kjle/v1/push/demoenginez/*

Pushes qualifying KJLE leads to DemoEnginez via Lovable edge function.
Endpoint: https://wvqifxycceixsiwsudve.supabase.co/functions/v1/receive-kjle-leads

Phase 4 Layer 1 (2026-05-24): both /batch and /single now gate via DNC
before pushing. Batch uses dedupe_and_check_phones (dedup payoff at n>1);
single calls check_lead_for_channel directly (n=1 doesn't benefit from
dedup, just adds overhead).

Slice 1C (2026-05-27): imports fixed to match Slice 1B's split between
dnc_check.py and dnc_batch.py. ChannelCheckResult.to_dict() used to unwrap
the dataclass returned by check_lead_for_channel.
"""

import logging
import httpx
from datetime import datetime, date, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db
from ..lib.dnc_check import check_lead_for_channel
from ..lib.dnc_batch import dedupe_and_check_phones

logger = logging.getLogger(__name__)
router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

DE_EDGE_FUNCTION_URL = "https://wvqifxycceixsiwsudve.supabase.co/functions/v1/receive-kjle-leads"
DE_API_KEY           = "demoenginez-kjle-push-2026"
DE_USER_ID           = "a18da7d6-1d54-4330-87a5-d49efb5c8df0"
PUSH_SOURCE          = "kjle"


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class BatchPushRequest(BaseModel):
    limit:         int            = 50
    niche_slug:    Optional[str]  = None
    state:         Optional[str]  = None
    min_pain:      int            = 0
    segment_label: Optional[str]  = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _today_iso() -> str:
    return date.today().isoformat()

def _map_lead_to_de(lead: dict) -> dict:
    pain = lead.get("pain_score") or 0
    return {
        "business_name": lead.get("business_name") or "",
        "phone":         lead.get("phone") or "",
        "email":         lead.get("email") or "",
        "website":       lead.get("website") or "",
        "address":       lead.get("address") or "",
        "city":          lead.get("city") or "",
        "region":        lead.get("state") or "",
        "zip":           lead.get("zip") or "",
        "niche":         lead.get("niche_slug") or "",
        "source":        PUSH_SOURCE,
        "hot_lead":      pain >= 30,   # v2 threshold (was 70 under v1, realigned 2026-05-09)
        "viewed":        False,
        "converted":     False,
        "outreach_sent": False,
        "user_id":       DE_USER_ID,
    }

async def _log_push_event(db, lead_count: int, status: str, filters_used: dict):
    try:
        db.table("export_log").insert({
            "export_type":  "demoenginez_push",
            "destination":  "demoenginez",
            "filters_used": filters_used,
            "lead_count":   lead_count,
            "status":       status,
            "created_at":   _now_iso(),
        }).execute()
    except Exception as e:
        logger.error(f"Failed to write push log: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# POST /push/demoenginez/batch
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/push/demoenginez/batch")
async def push_batch(body: BatchPushRequest):
    db = get_db()
    limit = min(body.limit, 500)

    query = (
        db.table("leads")
        .select(
            "id, business_name, phone, email, website, address, city, state, "
            "zip, niche_slug, pain_score, fit_demoenginez, is_active, segment_label"
        )
        .eq("is_active", True)
        .gte("pain_score", body.min_pain)
    )

    if body.segment_label:
        query = query.eq("segment_label", body.segment_label)
    if body.niche_slug:
        query = query.eq("niche_slug", body.niche_slug)
    if body.state:
        query = query.eq("state", body.state.upper())

    query = query.order("pain_score", desc=True).limit(limit)
    leads = query.execute().data or []

    if not leads:
        return {
            "status": "success",
            "message": "No eligible leads found",
            "summary": {"pushed": 0, "duplicates": 0, "failed": 0},
        }

    # ── DNC gating (Phase 4 Layer 1, Slice 1C) ─────────────────────────────
    # DemoEnginez is a voice-channel destination; gate via the voice channel
    # check. dedupe_and_check_phones takes list[str] of lead_ids; we extract
    # them from the leads we already fetched. The function runs internal
    # suppressions + fed_dnc_list + TCPA litigator + Searchbug (full tier)
    # and fans the verdict per-unique-phone to all leads sharing that phone.
    lead_id_list = [lead["id"] for lead in leads if lead.get("id")]

    dnc_summary = await dedupe_and_check_phones(
        lead_id_list,
        channel="voice",
        source="demoenginez_push_batch",
    )
    # Slice 1B returns blocked_leads as list[dict{lead_id, reason, result_source}].
    blocked_ids = {b["lead_id"] for b in dnc_summary.get("blocked_leads", [])}
    allowed_leads = [lead for lead in leads if lead.get("id") not in blocked_ids]

    if not allowed_leads:
        return {
            "status": "success",
            "message": "All eligible leads blocked by DNC",
            "summary": {
                "pushed":                0,
                "duplicates":            0,
                "failed":                0,
                "blocked_by_dnc":        len(blocked_ids),
                "unique_phones_checked": dnc_summary.get("unique_phones_checked", 0),
                "dnc_cost_usd":          dnc_summary.get("cost_usd", 0.0),
            },
        }

    # Map leads to DemoEnginez format
    de_leads = [_map_lead_to_de(lead) for lead in allowed_leads]

    # Call DemoEnginez edge function
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                DE_EDGE_FUNCTION_URL,
                json={
                    "api_key": DE_API_KEY,
                    "leads":   de_leads,
                },
            )
            result = r.json()

        if r.status_code != 200:
            raise Exception(f"Edge function error {r.status_code}: {result}")

        inserted   = result.get("inserted", 0)
        duplicates = result.get("duplicates", 0)
        failed     = result.get("failed", 0)

        filters_used = body.model_dump()
        await _log_push_event(db, inserted, "success", filters_used)

        return {
            "status": "success",
            "summary": {
                "pushed":                inserted,
                "duplicates":            duplicates,
                "failed":                failed,
                "total_sent":            len(de_leads),
                "blocked_by_dnc":        len(blocked_ids),
                "unique_phones_checked": dnc_summary.get("unique_phones_checked", 0),
                "dnc_cost_usd":          dnc_summary.get("cost_usd", 0.0),
            },
            "filters": filters_used,
        }

    except Exception as e:
        logger.error(f"DemoEnginez edge function push failed: {e}")
        raise HTTPException(status_code=500, detail=f"Push failed: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# POST /push/demoenginez/single/{lead_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/push/demoenginez/single/{lead_id}")
async def push_single_lead(lead_id: str):
    db = get_db()

    result = (
        db.table("leads")
        .select(
            "id, business_name, phone, email, website, address, city, state, "
            "zip, niche_slug, pain_score, is_active"
        )
        .eq("id", lead_id)
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead = result.data

    # ── DNC gating (Phase 4 Layer 1, Slice 1C) ─────────────────────────────
    # Single-lead path uses check_lead_for_channel directly — n=1 doesn't
    # benefit from dedup, so routing through dedupe_and_check_phones would
    # just add overhead. Response shape mirrors the batch endpoint so
    # callers get a consistent contract.
    #
    # Slice 1B returns a ChannelCheckResult dataclass; unwrap with to_dict()
    # so the response-shape contract matches the batch path.
    dnc_result = await check_lead_for_channel(
        lead_id,
        channel="voice",
        source="demoenginez_push_single",
    )
    dnc = dnc_result.to_dict()

    if dnc.get("is_blocked"):
        return {
            "status":                "blocked",
            "pushed":                0,
            "duplicate":             False,
            "business_name":         lead.get("business_name"),
            "blocked_by_dnc":        True,
            "unique_phones_checked": 1,
            "dnc_cost_usd":          dnc.get("cost_usd", 0.0),
            "dnc_reason":            dnc.get("reason"),
            "dnc_result_source":     dnc.get("result_source"),
        }

    de_lead = _map_lead_to_de(lead)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                DE_EDGE_FUNCTION_URL,
                json={"api_key": DE_API_KEY, "leads": [de_lead]},
            )
            result = r.json()

        return {
            "status":                "success",
            "pushed":                result.get("inserted", 0),
            "duplicate":             result.get("duplicates", 0) > 0,
            "business_name":         lead.get("business_name"),
            "blocked_by_dnc":        False,
            "unique_phones_checked": 1,
            "dnc_cost_usd":          dnc.get("cost_usd", 0.0),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Push failed: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# GET /push/demoenginez/status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/push/demoenginez/status")
async def get_push_status():
    db = get_db()

    total = db.table("leads").select("id", count="exact").eq("is_active", True).execute().count or 0
    today = _today_iso()
    today_logs = db.table("export_log").select("lead_count").eq("export_type", "demoenginez_push").gte("created_at", today).execute().data or []
    pushed_today = sum(r.get("lead_count") or 0 for r in today_logs)

    return {
        "status":         "success",
        "eligible_leads": total,
        "pushed_today":   pushed_today,
        "infrastructure": {
            "mode":     "edge_function",
            "endpoint": DE_EDGE_FUNCTION_URL,
        },
    }
