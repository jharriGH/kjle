"""
KJLE — Prompt 28: Lead Management Routes
File: api/routes/lead_management.py
"""

import os
import logging
from datetime import datetime, timezone, timedelta
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


def _reattach_cutoff_iso(supabase) -> Optional[str]:
    """ISO cutoff for reattach-cooldown dedup, or None when disabled.

    Reads admin_settings 'campaign_reattach_cooldown_days' (default 30).
    Returns None when days <= 0, else (utcnow - days) as ISO-8601.
    """
    try:
        res = (
            supabase.table("admin_settings")
            .select("value")
            .eq("key", "campaign_reattach_cooldown_days")
            .limit(1)
            .execute()
        )
        rows = res.data or []
        days = int(rows[0]["value"]) if rows else 30
    except Exception:
        days = 30
    if days <= 0:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def score_to_segment(pain_score) -> str:
    """Inline threshold logic — mirrors segments_engine.py::_classify_lead.
    No import to avoid circular deps. Keep in sync.

    v2 thresholds (2026-05-09): HOT >= 30 | WARM 15-29 | COLD < 15.
    """
    if pain_score is None:
        return "unclassified"
    try:
        score = float(pain_score)
    except (ValueError, TypeError):
        return "unclassified"
    if score >= 30:
        return "hot"
    elif score >= 15:
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
        pending_res = supabase.table("leads").select("id", count="exact").eq("email_status", "pending_batch").execute()

        hot  = hot_res.count  or 0
        warm = warm_res.count or 0
        cold = cold_res.count or 0
        unclassified = total - hot - warm - cold

        valid_email   = valid_res.count   or 0
        invalid_email = invalid_res.count or 0
        unknown_email = unknown_res.count or 0
        error_email   = error_res.count   or 0
        pending_email = pending_res.count or 0

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
                "valid":         valid_email,
                "invalid":       invalid_email,
                "unknown":       unknown_email,
                "error":         error_email,
                "pending_batch": pending_email,
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
# GET /kjle/v1/leads/eligible-for-campaign
# Registered BEFORE /leads/{lead_id} to avoid route shadowing.
# Returns campaign-ready leads for the n8n route→create→attach flow:
# passes compliance (email valid, internal phone suppressions, dnc_status)
# and targeting filters (niche / pain / segment scoping).
# ─────────────────────────────────────────────────

# dnc_status values that mean "do not contact". Allowed states are NULL
# (column unpopulated = not yet evaluated) plus 'unchecked' / 'searchbug_clean'.
# Enum source: migrations/leads_dnc_status.sql.
_DNC_STATUS_BLOCKED = (
    "fed_dnc_flagged",
    "tcpa_litigator_flagged",
    "searchbug_dnc",
    "internal_suppression",
    "leadcrap_filtered",
)


@router.get("/leads/eligible-for-campaign")
async def eligible_for_campaign(
    vertical: Optional[str] = Query(None, description="Vertical (alias for niche)"),
    niche: Optional[str] = Query(None, description="niche_slug filter; takes precedence over `vertical`"),
    segment_id: Optional[str] = Query(None, description="Saved segment id; applies its stored filters and passes through"),
    pain_min: Optional[int] = Query(None, description="Minimum pain_score (inclusive)"),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    require_email_valid: bool = Query(True, description="When true, restrict to email_status='valid' + email_valid=true"),
    x_api_key: str = Header(...),
):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    skipped_filters: List[str] = []

    # ── segment_id passthrough — apply stored filters when available ───────
    seg_niche: Optional[str] = None
    seg_pain_min: Optional[int] = None
    seg_label: Optional[str] = None
    if segment_id:
        try:
            seg_res = (
                supabase.table("segments")
                .select("id, segment_label, niche_slug, filters")
                .eq("id", segment_id)
                .limit(1)
                .execute()
            )
            seg_rows = seg_res.data or []
            if not seg_rows:
                raise HTTPException(status_code=404, detail=f"Segment '{segment_id}' not found")
            seg = seg_rows[0]
            seg_filters = seg.get("filters") or {}
            if not isinstance(seg_filters, dict):
                seg_filters = {}
            seg_niche = seg.get("niche_slug") or seg_filters.get("niche_slug")
            seg_label = seg.get("segment_label") or seg_filters.get("segment_label")
            seg_pain_min = seg_filters.get("min_pain")
        except HTTPException:
            raise
        except Exception as e:
            skipped_filters.append(f"segment_lookup_failed:{e}")

    effective_niche = niche or vertical or seg_niche
    effective_pain_min = pain_min if pain_min is not None else seg_pain_min

    # ── targeting + base query ─────────────────────────────────────────────
    select_cols = "id, business_name, email, phone, niche_slug, pain_score, dnc_status"
    query = supabase.table("leads").select(select_cols).eq("is_active", True)
    count_query = supabase.table("leads").select("id", count="exact").eq("is_active", True)

    if effective_niche:
        query = query.eq("niche_slug", effective_niche)
        count_query = count_query.eq("niche_slug", effective_niche)

    if seg_label and seg_label != "custom":
        query = query.eq("segment_label", seg_label)
        count_query = count_query.eq("segment_label", seg_label)

    if effective_pain_min is not None:
        query = query.gte("pain_score", int(effective_pain_min))
        count_query = count_query.gte("pain_score", int(effective_pain_min))

    # ── email presence + Truelist validity ─────────────────────────────────
    query = query.neq("email", None)
    count_query = count_query.neq("email", None)
    if require_email_valid:
        query = query.eq("email_status", "valid").eq("email_valid", True)
        count_query = count_query.eq("email_status", "valid").eq("email_valid", True)

    # ── dnc_status: allow NULL (not-yet-evaluated) or non-blocked values ───
    blocked_csv = ",".join(_DNC_STATUS_BLOCKED)
    dnc_or = f"dnc_status.is.null,dnc_status.not.in.({blocked_csv})"
    query = query.or_(dnc_or)
    count_query = count_query.or_(dnc_or)

    # ── reattach-cooldown exclusion ────────────────────────────────────────
    # Exclude leads attached to a campaign within the cooldown window
    # (admin_settings 'campaign_reattach_cooldown_days', default 30). NULL =
    # never attached, so it always passes. Same predicate on BOTH queries.
    cutoff = _reattach_cutoff_iso(supabase)
    if cutoff:
        attached_or = f"last_campaign_attached_at.is.null,last_campaign_attached_at.lt.{cutoff}"
        query = query.or_(attached_or)
        count_query = count_query.or_(attached_or)   # SAME predicate on BOTH

    # ── pre-suppression total ──────────────────────────────────────────────
    try:
        count_result = count_query.execute()
        total = count_result.count if count_result.count is not None else 0
    except Exception as e:
        logger.error(f"eligible_for_campaign count failed: {e}")
        raise HTTPException(status_code=500, detail=f"count_query_failed: {e}")

    # ── fetch the page, ordered by pain_score desc ─────────────────────────
    try:
        page = (
            query.order("pain_score", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        rows = page.data or []
    except Exception as e:
        logger.error(f"eligible_for_campaign fetch failed: {e}")
        raise HTTPException(status_code=500, detail=f"lead_query_failed: {e}")

    # ── dnc_suppressions (phone-based internal suppression list) ───────────
    # Read-only batch lookup mirrors api/lib/dnc_check.py::_check_internal_suppression
    # but vectorized to one round-trip per page.
    if rows:
        from ..lib import phone_utils  # local import; module is pure-function
        phone_norm_by_row: dict = {}
        unique_norms: set = set()
        for r in rows:
            np = phone_utils.normalize_phone(r.get("phone"))
            if np:
                phone_norm_by_row[r["id"]] = np
                unique_norms.add(np)

        suppressed: set = set()
        if unique_norms:
            try:
                sup_res = (
                    supabase.table("dnc_suppressions")
                    .select("phone")
                    .in_("phone", list(unique_norms))
                    .execute()
                )
                suppressed = {row["phone"] for row in (sup_res.data or []) if row.get("phone")}
            except Exception as e:
                logger.warning(f"eligible_for_campaign dnc_suppressions lookup failed: {e}")
                skipped_filters.append(f"dnc_suppressions_lookup_failed:{e}")

        if suppressed:
            rows = [r for r in rows if phone_norm_by_row.get(r["id"]) not in suppressed]

    leads_out = [
        {
            "id": r.get("id"),
            "business_name": r.get("business_name"),
            "email": r.get("email"),
            "phone": r.get("phone"),
            "niche": r.get("niche_slug"),
            "pain_score": r.get("pain_score"),
            "dnc_status": r.get("dnc_status"),
        }
        for r in rows
    ]

    return {
        "total": total,
        "count": len(leads_out),
        "leads": leads_out,
        "skipped_filters": skipped_filters,
        "segment_id": segment_id,
    }


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
