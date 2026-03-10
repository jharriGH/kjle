"""
VoiceDrop OS Push Integration
KJLE - King James Lead Empire
Routes: /kjle/v1/push/voicedrop/*

Pushes qualifying KJLE leads (phone not null) into VoiceDrop OS's
leads table for RVM + AI calling campaigns.

VoiceDrop OS lives in the voicedrop schema of the shared Supabase project.
Falls back to shared KJLE client if dedicated credentials are not configured.
"""

import logging
from datetime import datetime, date, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

from ..database import get_db
from ..config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

VOICEDROP_TABLE = "voicedrop.leads"
PUSH_SOURCE     = "kjle"
PUSH_STATUS     = "queued"
BATCH_HARD_CAP  = 500


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class SinglePushBody(BaseModel):
    campaign_id: Optional[str] = None


class BatchPushRequest(BaseModel):
    limit: int = 100
    niche_slug: Optional[str] = None
    state: Optional[str] = None
    min_pain: int = 50
    segment_label: Optional[str] = None
    campaign_id: Optional[str] = None
    require_phone: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# VoiceDrop Client Factory
# ─────────────────────────────────────────────────────────────────────────────

def _get_vd_client() -> Client:
    """
    Returns a Supabase client for VoiceDrop OS.
    Uses dedicated credentials if configured; falls back to shared KJLE client
    since both live in the same Supabase project (voicedrop schema).
    """
    if settings.VOICEDROP_SUPABASE_URL and settings.VOICEDROP_SUPABASE_KEY:
        return create_client(
            settings.VOICEDROP_SUPABASE_URL,
            settings.VOICEDROP_SUPABASE_KEY,
        )
    return get_db()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return date.today().isoformat()


def _normalize_phone(phone: Optional[str]) -> Optional[str]:
    """Strips non-numeric characters for consistent dedup comparison."""
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    return digits if digits else None


def _map_lead_to_vd(lead: dict, campaign_id: Optional[str]) -> dict:
    """Maps a KJLE lead dict to VoiceDrop OS leads table format."""
    return {
        "business_name": lead.get("business_name") or "",
        "phone":         lead.get("phone") or "",
        "city":          lead.get("city") or "",
        "state":         lead.get("state") or "",
        "niche":         lead.get("niche_slug") or "",
        "pain_score":    lead.get("pain_score") or 0,
        "segment_label": lead.get("segment_label") or "",
        "source":        PUSH_SOURCE,
        "status":        PUSH_STATUS,
        "campaign_id":   campaign_id,
        "created_at":    _now_iso(),
    }


async def _is_duplicate(vd_client: Client, phone_digits: Optional[str]) -> bool:
    """Returns True if a lead with this phone already exists in VoiceDrop leads."""
    if not phone_digits:
        return False
    try:
        result = (
            vd_client.table(VOICEDROP_TABLE)
            .select("id", count="exact")
            .eq("phone", phone_digits)
            .execute()
        )
        return (result.count or 0) > 0
    except Exception as e:
        logger.warning(f"VoiceDrop duplicate check failed for phone {phone_digits}: {e}")
        return False   # fail open — attempt insert


async def _log_push(db, lead_count: int, status: str, filters_used: dict) -> None:
    """Logs push event to export_log. Silently absorbs failures."""
    try:
        db.table("export_log").insert({
            "export_type":  "voicedrop_push",
            "destination":  "voicedrop",
            "filters_used": filters_used,
            "lead_count":   lead_count,
            "status":       status,
            "created_at":   _now_iso(),
        }).execute()
    except Exception as e:
        logger.error(f"Failed to write VoiceDrop push log: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# POST /push/voicedrop/single/{lead_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/push/voicedrop/single/{lead_id}")
async def push_single_lead(lead_id: str, body: SinglePushBody = SinglePushBody()):
    """
    Push one KJLE lead to VoiceDrop OS.
    Validates: is_active=true, phone not null.
    Deduplicates by phone before inserting.
    """
    db = get_db()

    result = (
        db.table("leads")
        .select(
            "id, business_name, phone, city, state, "
            "niche_slug, pain_score, segment_label, is_active"
        )
        .eq("id", lead_id)
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail=f"Lead '{lead_id}' not found")

    lead = result.data

    if not lead.get("is_active"):
        raise HTTPException(
            status_code=422,
            detail=f"Lead '{lead_id}' is not active",
        )

    phone = (lead.get("phone") or "").strip()
    if not phone:
        raise HTTPException(
            status_code=422,
            detail=f"Lead '{lead_id}' has no phone number. VoiceDrop requires a phone.",
        )

    phone_digits = _normalize_phone(phone)
    vd_client = _get_vd_client()

    is_dup = await _is_duplicate(vd_client, phone_digits)
    if is_dup:
        return {
            "status":        "success",
            "pushed":        False,
            "duplicate":     True,
            "lead_id":       lead_id,
            "business_name": lead.get("business_name"),
            "message":       f"Lead already exists in VoiceDrop (phone: {phone_digits})",
        }

    vd_payload = _map_lead_to_vd(lead, body.campaign_id)
    try:
        vd_client.table(VOICEDROP_TABLE).insert(vd_payload).execute()
    except Exception as e:
        logger.error(f"VoiceDrop insert failed for lead {lead_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Insert to VoiceDrop failed: {str(e)}",
        )

    await _log_push(db, 1, "success", {"lead_id": lead_id, "campaign_id": body.campaign_id})

    return {
        "status":        "success",
        "pushed":        True,
        "duplicate":     False,
        "lead_id":       lead_id,
        "business_name": lead.get("business_name"),
        "phone":         phone,
        "pain_score":    lead.get("pain_score"),
        "campaign_id":   body.campaign_id,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /push/voicedrop/batch
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/push/voicedrop/batch")
async def push_batch(body: BatchPushRequest):
    """
    Push a batch of KJLE leads to VoiceDrop OS.
    Filters by is_active, phone presence, pain_score, segment_label.
    Deduplicates by phone. Returns push summary.
    """
    db = get_db()
    vd_client = _get_vd_client()

    limit = min(body.limit, BATCH_HARD_CAP)

    query = (
        db.table("leads")
        .select(
            "id, business_name, phone, city, state, "
            "niche_slug, pain_score, segment_label, is_active"
        )
        .eq("is_active", True)
        .gte("pain_score", body.min_pain)
    )

    if body.require_phone:
        query = query.not_.is_("phone", "null").neq("phone", "")
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
            "message": "No eligible leads found matching filters",
            "summary": {
                "pushed":            0,
                "skipped_duplicate": 0,
                "skipped_no_phone":  0,
                "failed":            0,
                "total_fetched":     0,
            },
            "filters": {**body.model_dump(), "effective_limit": limit},
        }

    pushed = 0
    skipped_duplicate = 0
    skipped_no_phone = 0
    failed = 0
    results_detail = []

    for lead in leads:
        lead_id = lead["id"]
        phone = (lead.get("phone") or "").strip()

        if not phone:
            skipped_no_phone += 1
            results_detail.append({
                "lead_id":       lead_id,
                "business_name": lead.get("business_name"),
                "status":        "skipped_no_phone",
            })
            continue

        phone_digits = _normalize_phone(phone)
        is_dup = await _is_duplicate(vd_client, phone_digits)

        if is_dup:
            skipped_duplicate += 1
            results_detail.append({
                "lead_id":       lead_id,
                "business_name": lead.get("business_name"),
                "status":        "duplicate",
                "phone":         phone_digits,
            })
            continue

        vd_payload = _map_lead_to_vd(lead, body.campaign_id)
        try:
            vd_client.table(VOICEDROP_TABLE).insert(vd_payload).execute()
            pushed += 1
            results_detail.append({
                "lead_id":       lead_id,
                "business_name": lead.get("business_name"),
                "pain_score":    lead.get("pain_score"),
                "phone":         phone,
                "status":        "pushed",
            })
        except Exception as e:
            failed += 1
            logger.error(f"VoiceDrop batch insert failed for lead {lead_id}: {e}")
            results_detail.append({
                "lead_id":       lead_id,
                "business_name": lead.get("business_name"),
                "status":        "failed",
                "error":         str(e),
            })

    if pushed > 0 and failed == 0:
        push_status = "success"
    elif pushed > 0 and failed > 0:
        push_status = "partial"
    elif pushed == 0 and failed > 0:
        push_status = "failed"
    else:
        push_status = "success"

    filters_used = {**body.model_dump(), "effective_limit": limit}
    await _log_push(db, pushed, push_status, filters_used)

    return {
        "status": push_status,
        "summary": {
            "pushed":            pushed,
            "skipped_duplicate": skipped_duplicate,
            "skipped_no_phone":  skipped_no_phone,
            "failed":            failed,
            "total_fetched":     len(leads),
        },
        "filters": filters_used,
        "results": results_detail,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /push/voicedrop/status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/push/voicedrop/status")
async def get_push_status():
    """
    Returns eligible lead counts by segment_label, total pushed today,
    and infrastructure mode (shared vs dedicated Supabase client).
    """
    db = get_db()

    # Eligible = is_active=true, phone not null
    def _eligible_count(segment: Optional[str]) -> int:
        q = (
            db.table("leads")
            .select("id", count="exact")
            .eq("is_active", True)
            .not_.is_("phone", "null")
            .neq("phone", "")
        )
        if segment:
            q = q.eq("segment_label", segment)
        r = q.execute()
        return r.count if r.count is not None else 0

    total_eligible = _eligible_count(None)
    hot_count   = _eligible_count("hot")
    warm_count  = _eligible_count("warm")
    cold_count  = _eligible_count("cold")

    # Pushed today from export_log
    today = _today_iso()
    today_rows = (
        db.table("export_log")
        .select("lead_count")
        .eq("export_type", "voicedrop_push")
        .gte("created_at", today)
        .execute()
        .data or []
    )
    pushed_today = sum(r.get("lead_count") or 0 for r in today_rows)

    using_shared = not (
        settings.VOICEDROP_SUPABASE_URL and settings.VOICEDROP_SUPABASE_KEY
    )

    return {
        "status": "success",
        "eligible_for_push": {
            "total": total_eligible,
            "hot":   hot_count,
            "warm":  warm_count,
            "cold":  cold_count,
        },
        "pushed_today": pushed_today,
        "infrastructure": {
            "mode":          "shared_supabase" if using_shared else "dedicated_supabase",
            "target_table":  VOICEDROP_TABLE,
            "source_schema": "kjle",
        },
    }
