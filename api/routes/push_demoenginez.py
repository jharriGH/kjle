"""
DemoEnginez Push Integration
KJLE - King James Lead Empire
Routes: /kjle/v1/push/demoenginez/*

Pushes qualifying KJLE leads to DemoEnginez via Lovable edge function.
Endpoint: https://wvqifxycceixsiwsudve.supabase.co/functions/v1/receive-kjle-leads
"""

import logging
import httpx
from datetime import datetime, date, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db

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
        "hot_lead":      pain >= 70,
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

    # Map leads to DemoEnginez format
    de_leads = [_map_lead_to_de(lead) for lead in leads]

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
                "pushed":     inserted,
                "duplicates": duplicates,
                "failed":     failed,
                "total_sent": len(de_leads),
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
    de_lead = _map_lead_to_de(lead)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                DE_EDGE_FUNCTION_URL,
                json={"api_key": DE_API_KEY, "leads": [de_lead]},
            )
            result = r.json()

        return {
            "status":        "success",
            "pushed":        result.get("inserted", 0),
            "duplicate":     result.get("duplicates", 0) > 0,
            "business_name": lead.get("business_name"),
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
