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

    overlap=true  → lead was recently attached to a campaign; do not add to another.
    overlap=false → safe to target.

    Checks campaign_lead_attachments within the cooldown window. Any attachment
    within the window counts as overlap.
    """
    verify_api_key(x_api_key)
    supabase = get_supabase()

    cooldown_days = days if days is not None else _attach_cooldown_days(supabase)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()

    try:
        # ── Pull attachment rows for this email within the cooldown window ─────
        cla_query = (
            supabase.table("campaign_lead_attachments")
            .select("campaign_name, attached_at")
            .eq("email", email)
            .gte("attached_at", cutoff)
        )
        if kje_product:
            cla_query = cla_query.eq("kje_product", kje_product)

        cla_res = cla_query.execute()
        attachment_rows = cla_res.data or []

        if not attachment_rows:
            return {"overlap": False, "matching_campaigns": []}

        matching_campaigns = [
            {
                "name": r.get("campaign_name"),
                "attached_at": r.get("attached_at"),
            }
            for r in attachment_rows
        ]

        return {"overlap": True, "matching_campaigns": matching_campaigns}

    except Exception as e:
        logger.error(f"lead_overlap error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
