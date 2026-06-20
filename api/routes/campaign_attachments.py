"""
KJLE — Campaign Lead Overlap / Attachment Query
File: api/routes/campaign_attachments.py

GET /kjle/v1/campaigns/lead-overlap
  Checks whether an email is currently attached to an active or paused campaign
  within the cooldown window. Used by downstream systems before adding a lead
  to a new campaign.
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Header, Query
from supabase import create_client, Client

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")

router = APIRouter()


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


def _attach_cooldown_days(supabase: Client) -> int:
    """Read campaign_attach_cooldown_days from admin_settings (default 30)."""
    try:
        res = (
            supabase.table("admin_settings")
            .select("value")
            .eq("key", "campaign_attach_cooldown_days")
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return int(rows[0]["value"]) if rows else 30
    except Exception:
        return 30


@router.get("/campaigns/lead-overlap")
async def lead_overlap(
    email: str = Query(..., description="Email address to check"),
    kje_product: Optional[str] = Query(None, description="Narrow to a specific KJE product slug"),
    days: Optional[int] = Query(None, description="Lookback window in days; defaults to admin_settings campaign_attach_cooldown_days"),
    x_api_key: str = Header(...),
):
    """
    Check whether an email is attached to any active or paused campaign within
    the cooldown window.

    overlap=true  → lead is in-flight; do not add to another campaign.
    overlap=false → safe to target.

    Status is resolved live by joining reachinbox_campaign_id →
    campaign_performance.status; only 'active' and 'paused' count as overlap.
    """
    verify_api_key(x_api_key)
    supabase = get_supabase()

    cooldown_days = days if days is not None else _attach_cooldown_days(supabase)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()

    try:
        # ── Resolve currently active/paused campaign IDs ──────────────────────
        cp_res = (
            supabase.table("campaign_performance")
            .select("reachinbox_campaign_id, campaign_name, status")
            .in_("status", ["active", "paused"])
            .execute()
        )
        active_campaigns = {
            r["reachinbox_campaign_id"]: r
            for r in (cp_res.data or [])
            if r.get("reachinbox_campaign_id")
        }

        if not active_campaigns:
            return {"overlap": False, "matching_campaigns": []}

        # ── Pull attachment rows for this email within the cooldown window ─────
        cla_query = (
            supabase.table("campaign_lead_attachments")
            .select("reachinbox_campaign_id")
            .eq("email", email)
            .gte("attached_at", cutoff)
        )
        if kje_product:
            cla_query = cla_query.eq("kje_product", kje_product)

        cla_res = cla_query.execute()
        attachment_rows = cla_res.data or []

        # ── Filter to campaigns that are still active or paused ───────────────
        matched_ids = {
            r["reachinbox_campaign_id"]
            for r in attachment_rows
            if r.get("reachinbox_campaign_id") in active_campaigns
        }

        if not matched_ids:
            return {"overlap": False, "matching_campaigns": []}

        matching_campaigns = [
            {
                "id": rid,
                "name": active_campaigns[rid].get("campaign_name"),
                "status": active_campaigns[rid].get("status"),
            }
            for rid in matched_ids
        ]

        return {"overlap": True, "matching_campaigns": matching_campaigns}

    except Exception as e:
        logger.error(f"lead_overlap error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
