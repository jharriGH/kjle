"""
DemoEnginez Push Integration
KJLE - King James Lead Empire
Routes: /kjle/v1/push/demoenginez/*

Pushes qualifying KJLE leads (fit_demoenginez=true) directly into
the DemoEnginez leads table. Both apps share one Supabase project —
KJLE lives in kjle schema, DemoEnginez in public schema.

If DEMOENGINEZ_SUPABASE_URL/KEY are set, uses a dedicated client for
DemoEnginez. Otherwise falls back to the shared Supabase client since
both schemas live in the same project.
"""

import logging
from datetime import datetime, date, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from supabase import create_client, Client

from ..database import get_db
from ..config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

DEMOENGINEZ_TABLE = "leads"          # public schema — DemoEnginez target table
KJLE_TABLE        = "kjle.leads"     # kjle schema — KJLE source table (schema-prefixed)
PUSH_SOURCE       = "kjle"
PUSH_STATUS       = "new"


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class BatchPushRequest(BaseModel):
    limit: int = 50
    niche_slug: Optional[str] = None
    state: Optional[str] = None
    min_pain: int = 60
    segment_label: str = "hot"


# ─────────────────────────────────────────────────────────────────────────────
# DemoEnginez Client Factory
# ─────────────────────────────────────────────────────────────────────────────

def _get_de_client() -> Client:
    """
    Returns a Supabase client pointed at the DemoEnginez database.

    Strategy:
    - If DEMOENGINEZ_SUPABASE_URL + DEMOENGINEZ_SUPABASE_KEY are set,
      creates a dedicated client for DemoEnginez.
    - Otherwise falls back to the shared KJLE client — both apps share
      one Supabase project, so public.leads is accessible from either client.
    """
    if settings.DEMOENGINEZ_SUPABASE_URL and settings.DEMOENGINEZ_SUPABASE_KEY:
        return create_client(
            settings.DEMOENGINEZ_SUPABASE_URL,
            settings.DEMOENGINEZ_SUPABASE_KEY,
        )
    # Shared project — fall back to KJLE client; public.leads is accessible
    return get_db()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return date.today().isoformat()


def _map_lead_to_de(lead: dict) -> dict:
    """
    Maps a KJLE lead dict to DemoEnginez leads table format.
    Normalizes niche_slug → niche (DemoEnginez uses 'niche' column).
    """
    return {
        "business_name": lead.get("business_name") or "",
        "phone":         lead.get("phone") or "",
        "email":         lead.get("email") or "",
        "website":       lead.get("website") or "",
        "city":          lead.get("city") or "",
        "state":         lead.get("state") or "",
        "niche":         lead.get("niche_slug") or "",   # KJLE uses niche_slug
        "pain_score":    lead.get("pain_score") or 0,
        "source":        PUSH_SOURCE,
        "status":        PUSH_STATUS,
        "created_at":    _now_iso(),
    }


def _phone_key(lead: dict) -> Optional[str]:
    """Returns a normalized phone string for dedup, or None if no phone."""
    phone = (lead.get("phone") or "").strip()
    if not phone:
        return None
    # Strip everything non-numeric for comparison
    digits = "".join(c for c in phone if c.isdigit())
    return digits if digits else None


async def _is_duplicate_in_de(de_client: Client, phone: Optional[str]) -> bool:
    """
    Returns True if a lead with this phone already exists in DemoEnginez leads.
    Skips check entirely if phone is None/empty.
    """
    if not phone:
        return False
    try:
        result = (
            de_client.table(DEMOENGINEZ_TABLE)
            .select("id", count="exact")
            .eq("phone", phone)
            .execute()
        )
        return (result.count or 0) > 0
    except Exception as e:
        logger.warning(f"Duplicate check failed for phone {phone}: {e}")
        return False  # Fail open — attempt insert, let DB constraint catch it


async def _log_push_event(
    db,
    lead_count: int,
    status: str,
    filters_used: dict,
) -> None:
    """Logs push event to export_log. Silently absorbs failures."""
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
# POST /push/demoenginez/single/{lead_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/push/demoenginez/single/{lead_id}")
async def push_single_lead(lead_id: str):
    """
    Push one KJLE lead to DemoEnginez.
    Validates fit_demoenginez=true and is_active=true.
    Deduplicates by phone before inserting.
    """
    db = get_db()

    # Fetch KJLE lead
    result = (
        db.table("leads")
        .select(
            "id, business_name, phone, email, website, city, state, "
            "niche_slug, pain_score, fit_demoenginez, is_active, segment_label"
        )
        .eq("id", lead_id)
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail=f"Lead '{lead_id}' not found")

    lead = result.data

    # Fit gate
    if not lead.get("is_active"):
        raise HTTPException(
            status_code=422,
            detail=f"Lead '{lead_id}' is not active",
        )

    if not lead.get("fit_demoenginez"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Lead '{lead_id}' has fit_demoenginez=false. "
                "Only DemoEnginez-fit leads can be pushed."
            ),
        )

    de_client = _get_de_client()
    phone_key = _phone_key(lead)

    # Duplicate check
    is_dup = await _is_duplicate_in_de(de_client, phone_key)
    if is_dup:
        return {
            "status": "success",
            "pushed":         False,
            "duplicate":      True,
            "lead_id":        lead_id,
            "business_name":  lead.get("business_name"),
            "message":        f"Lead already exists in DemoEnginez (phone match: {phone_key})",
        }

    # Map and insert
    de_payload = _map_lead_to_de(lead)
    try:
        de_client.table(DEMOENGINEZ_TABLE).insert(de_payload).execute()
    except Exception as e:
        logger.error(f"DemoEnginez insert failed for lead {lead_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Insert to DemoEnginez failed: {str(e)}",
        )

    # Log single push as export event
    await _log_push_event(db, 1, "success", {"lead_id": lead_id, "single_push": True})

    return {
        "status":        "success",
        "pushed":        True,
        "duplicate":     False,
        "lead_id":       lead_id,
        "business_name": lead.get("business_name"),
        "pain_score":    lead.get("pain_score"),
        "niche_slug":    lead.get("niche_slug"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /push/demoenginez/batch
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/push/demoenginez/batch")
async def push_batch(body: BatchPushRequest):
    """
    Pushes a batch of KJLE leads (fit_demoenginez=true) to DemoEnginez.
    Deduplicates by phone. Returns push summary.
    """
    db = get_db()
    de_client = _get_de_client()

    limit = min(body.limit, 500)   # hard cap — protect DE from runaway batches

    query = (
        db.table("leads")
        .select(
            "id, business_name, phone, email, website, city, state, "
            "niche_slug, pain_score, fit_demoenginez, is_active, segment_label"
        )
        .eq("is_active", True)
        .eq("fit_demoenginez", True)
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
            "message": "No eligible leads found matching filters",
            "summary": {
                "pushed":              0,
                "skipped_duplicate":   0,
                "skipped_not_fit":     0,
                "failed":              0,
                "total_fetched":       0,
            },
            "filters": body.model_dump(),
        }

    pushed = 0
    skipped_duplicate = 0
    skipped_not_fit = 0
    failed = 0
    results_detail = []

    for lead in leads:
        lead_id = lead["id"]

        # Secondary fit check (defensive — query already filters, but belt+suspenders)
        if not lead.get("fit_demoenginez"):
            skipped_not_fit += 1
            continue

        phone_key = _phone_key(lead)

        # Duplicate check
        is_dup = await _is_duplicate_in_de(de_client, phone_key)
        if is_dup:
            skipped_duplicate += 1
            results_detail.append({
                "lead_id":       lead_id,
                "business_name": lead.get("business_name"),
                "status":        "duplicate",
                "phone":         phone_key,
            })
            continue

        # Map and insert
        de_payload = _map_lead_to_de(lead)
        try:
            de_client.table(DEMOENGINEZ_TABLE).insert(de_payload).execute()
            pushed += 1
            results_detail.append({
                "lead_id":       lead_id,
                "business_name": lead.get("business_name"),
                "pain_score":    lead.get("pain_score"),
                "niche_slug":    lead.get("niche_slug"),
                "status":        "pushed",
            })
        except Exception as e:
            failed += 1
            logger.error(f"DE batch insert failed for lead {lead_id}: {e}")
            results_detail.append({
                "lead_id":       lead_id,
                "business_name": lead.get("business_name"),
                "status":        "failed",
                "error":         str(e),
            })

    # Determine overall status
    if pushed > 0 and failed == 0:
        push_status = "success"
    elif pushed > 0 and failed > 0:
        push_status = "partial"
    elif pushed == 0 and failed > 0:
        push_status = "failed"
    else:
        push_status = "success"   # all skipped — not an error

    filters_used = {
        **body.model_dump(),
        "effective_limit": limit,
    }
    await _log_push_event(db, pushed, push_status, filters_used)

    return {
        "status": push_status,
        "summary": {
            "pushed":            pushed,
            "skipped_duplicate": skipped_duplicate,
            "skipped_not_fit":   skipped_not_fit,
            "failed":            failed,
            "total_fetched":     len(leads),
        },
        "filters": filters_used,
        "results": results_detail,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /push/demoenginez/status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/push/demoenginez/status")
async def get_push_status():
    """
    Returns:
    - Count of KJLE leads eligible for DemoEnginez push (fit_demoenginez=true)
      broken down by segment_label.
    - Total leads pushed today (from export_log).
    - Shared vs dedicated Supabase client mode.
    """
    db = get_db()

    # Eligible leads by segment
    segments = ["hot", "warm", "cold", None]  # None = all / unclassified
    segment_counts = {}

    for label in segments:
        q = (
            db.table("leads")
            .select("id", count="exact")
            .eq("is_active", True)
            .eq("fit_demoenginez", True)
        )
        if label is not None:
            q = q.eq("segment_label", label)
        result = q.execute()
        key = label if label is not None else "unclassified"
        segment_counts[key] = result.count if result.count is not None else 0

    total_eligible = (
        db.table("leads")
        .select("id", count="exact")
        .eq("is_active", True)
        .eq("fit_demoenginez", True)
        .execute()
        .count or 0
    )

    # Pushed today from export_log
    today = _today_iso()
    today_log_rows = (
        db.table("export_log")
        .select("lead_count")
        .eq("export_type", "demoenginez_push")
        .gte("created_at", today)
        .execute()
        .data or []
    )
    pushed_today = sum(r.get("lead_count") or 0 for r in today_log_rows)

    using_shared_client = not (
        settings.DEMOENGINEZ_SUPABASE_URL and settings.DEMOENGINEZ_SUPABASE_KEY
    )

    return {
        "status": "success",
        "eligible_for_push": {
            "total":         total_eligible,
            "hot":           segment_counts.get("hot", 0),
            "warm":          segment_counts.get("warm", 0),
            "cold":          segment_counts.get("cold", 0),
            "unclassified":  segment_counts.get("unclassified", 0),
        },
        "pushed_today":       pushed_today,
        "infrastructure": {
            "mode":           "shared_supabase" if using_shared_client else "dedicated_supabase",
            "target_table":   f"public.{DEMOENGINEZ_TABLE}",
            "source_schema":  "kjle",
        },
    }
