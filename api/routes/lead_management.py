"""
KJLE — Prompt 28: Lead Management Routes
File: api/routes/lead_management.py
"""

import os
import logging
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Header, Query
from pydantic import BaseModel
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


def score_to_segment(pain_score) -> str:
    """Inline threshold logic — mirrors segments_engine.py. No import to avoid circular deps."""
    if pain_score is None:
        return "unclassified"
    try:
        score = float(pain_score)
    except (ValueError, TypeError):
        return "unclassified"
    if score >= 70:
        return "hot"
    elif score >= 40:
        return "warm"
    else:
        return "cold"


# ─────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────

class BulkActionRequest(BaseModel):
    lead_ids: List[str]
    action: str  # delete | reclassify | dnc | push_demoenginez | push_voicedrop


# ─────────────────────────────────────────────────
# GET /kjle/v1/leads/stats
# Registered BEFORE /leads/{lead_id} to avoid route shadowing
# ─────────────────────────────────────────────────

@router.get("/leads/stats")
async def lead_stats(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        total_res = supabase.table("leads").select("id", count="exact").execute()
        total = total_res.count or 0

        hot_res   = supabase.table("leads").select("id", count="exact").eq("segment_label", "hot").execute()
        warm_res  = supabase.table("leads").select("id", count="exact").eq("segment_label", "warm").execute()
        cold_res  = supabase.table("leads").select("id", count="exact").eq("segment_label", "cold").execute()

        valid_res   = supabase.table("leads").select("id", count="exact").eq("email_status", "valid").execute()
        invalid_res = supabase.table("leads").select("id", count="exact").eq("email_status", "invalid").execute()
        unknown_res = supabase.table("leads").select("id", count="exact").eq("email_status", "unknown").execute()
        error_res   = supabase.table("leads").select("id", count="exact").eq("email_status", "error").execute()

        hot  = hot_res.count  or 0
        warm = warm_res.count or 0
        cold = cold_res.count or 0
        unclassified = total - hot - warm - cold

        valid_email   = valid_res.count   or 0
        invalid_email = invalid_res.count or 0
        unknown_email = unknown_res.count or 0
        error_email   = error_res.count   or 0

        # DNC count — graceful fallback if column doesn't exist yet
        dnc_count = 0
        try:
            dnc_res   = supabase.table("leads").select("id", count="exact").eq("do_not_contact", True).execute()
            dnc_count = dnc_res.count or 0
        except Exception:
            dnc_count = 0

        phone_res = supabase.table("leads").select("id", count="exact").neq("phone", None).execute()
        email_res = supabase.table("leads").select("id", count="exact").neq("email", None).execute()

        return {
            "total": total,
            "by_segment": {
                "hot": hot,
                "warm": warm,
                "cold": cold,
                "unclassified": unclassified,
            },
            "by_email_status": {
                "valid": valid_email,
                "invalid": invalid_email,
                "unknown": unknown_email,
                "error": error_email,
            },
            "dnc_count": dnc_count,
            "has_phone": phone_res.count or 0,
            "has_email": email_res.count or 0,
        }

    except Exception as e:
        logger.error(f"lead_stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/leads/bulk
# Registered BEFORE /{lead_id} to avoid route shadowing
# ─────────────────────────────────────────────────

@router.post("/leads/bulk")
async def bulk_action(payload: BulkActionRequest, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)

    if len(payload.lead_ids) > 500:
        raise HTTPException(status_code=400, detail="Max 500 lead_ids per bulk request")

    valid_actions = {"delete", "reclassify", "dnc", "push_demoenginez", "push_voicedrop"}
    if payload.action not in valid_actions:
        raise HTTPException(status_code=400, detail=f"Invalid action. Must be one of: {valid_actions}")

    supabase = get_supabase()
    processed = 0
    errors = []

    try:
        if payload.action == "delete":
            supabase.table("leads").delete().in_("id", payload.lead_ids).execute()
            processed = len(payload.lead_ids)

        elif payload.action == "dnc":
            try:
                supabase.table("leads").update({"do_not_contact": True}).in_("id", payload.lead_ids).execute()
                processed = len(payload.lead_ids)
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"DNC update failed — do_not_contact column may not exist. Run migration first. Error: {e}"
                )

        elif payload.action == "reclassify":
            from datetime import datetime, timezone
            leads_res = supabase.table("leads").select("id, pain_score").in_("id", payload.lead_ids).execute()
            now = datetime.now(timezone.utc).isoformat()
            for lead in (leads_res.data or []):
                try:
                    new_label = score_to_segment(lead.get("pain_score"))
                    supabase.table("leads").update({
                        "segment_label": new_label,
                        "segmented_at": now,
                    }).eq("id", lead["id"]).execute()
                    processed += 1
                except Exception as e:
                    errors.append({"lead_id": lead["id"], "error": str(e)})

        elif payload.action in ("push_demoenginez", "push_voicedrop"):
            # Uses shared Supabase — target schema not exposed via REST, log as not supported via bulk
            raise HTTPException(
                status_code=400,
                detail="push_demoenginez and push_voicedrop bulk actions must be triggered via the dedicated push routes."
            )

        return {
            "action": payload.action,
            "processed": processed,
            "errors": errors,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"bulk_action error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# GET /kjle/v1/leads/{lead_id}
# ─────────────────────────────────────────────────

@router.get("/leads/{lead_id}")
async def get_lead(lead_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        result = supabase.table("leads").select("*").eq("id", lead_id).single().execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Lead not found")
        return result.data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"get_lead error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/leads/{lead_id}/reclassify
# ─────────────────────────────────────────────────

@router.post("/leads/{lead_id}/reclassify")
async def reclassify_lead(lead_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        lead_res = supabase.table("leads").select("id, pain_score").eq("id", lead_id).single().execute()
        if not lead_res.data:
            raise HTTPException(status_code=404, detail="Lead not found")

        from datetime import datetime, timezone
        new_label = score_to_segment(lead_res.data.get("pain_score"))
        now = datetime.now(timezone.utc).isoformat()

        supabase.table("leads").update({
            "segment_label": new_label,
            "segmented_at": now,
        }).eq("id", lead_id).execute()

        return {"lead_id": lead_id, "segment_label": new_label, "segmented_at": now}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"reclassify_lead error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/leads/{lead_id}/dnc
# ─────────────────────────────────────────────────

@router.post("/leads/{lead_id}/dnc")
async def mark_dnc(lead_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        lead_res = supabase.table("leads").select("id").eq("id", lead_id).single().execute()
        if not lead_res.data:
            raise HTTPException(status_code=404, detail="Lead not found")

        supabase.table("leads").update({"do_not_contact": True}).eq("id", lead_id).execute()
        return {"success": True, "lead_id": lead_id}

    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        if "do_not_contact" in error_msg or "column" in error_msg.lower():
            raise HTTPException(
                status_code=500,
                detail="do_not_contact column does not exist. Run this in Supabase SQL editor (project dhzpwobfihrprlcxqjbq): ALTER TABLE leads ADD COLUMN IF NOT EXISTS do_not_contact BOOLEAN DEFAULT FALSE;"
            )
        logger.error(f"mark_dnc error: {e}")
        raise HTTPException(status_code=500, detail=error_msg)


# ─────────────────────────────────────────────────
# DELETE /kjle/v1/leads/{lead_id}
# ─────────────────────────────────────────────────

@router.delete("/leads/{lead_id}")
async def delete_lead(lead_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        lead_res = supabase.table("leads").select("id").eq("id", lead_id).single().execute()
        if not lead_res.data:
            raise HTTPException(status_code=404, detail="Lead not found")

        supabase.table("leads").delete().eq("id", lead_id).execute()
        return {"deleted": True, "lead_id": lead_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete_lead error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
