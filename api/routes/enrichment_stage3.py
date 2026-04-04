"""
Stage 3 Enrichment Engine — Outscraper Google Maps (Pain-Gated)
KJLE - King James Lead Empire
Routes: /kjle/v1/enrichment/stage3/*

Stage 3 runs ONLY on leads with enrichment_stage = 2 AND pain_score >= 60.
Cost: $0.002 per record via Outscraper API.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import get_db
from ..config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

OUTSCRAPER_URL = "https://api.app.outscraper.com/maps/search-v3"
OUTSCRAPER_COST_PER_RECORD = 0.002
HTTP_TIMEOUT = 30.0          # Outscraper can be slow — generous timeout
DEFAULT_MIN_PAIN = 60        # Hard floor for Stage 3 pain gate
ABSOLUTE_MIN_PAIN = 60       # Never process below this regardless of request


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class BatchStage3Request(BaseModel):
    limit: int = 25
    niche_slug: Optional[str] = None
    state: Optional[str] = None
    min_pain: int = DEFAULT_MIN_PAIN

    def effective_min_pain(self) -> int:
        """Enforces the absolute pain floor — can never go below 60."""
        return max(self.min_pain, ABSOLUTE_MIN_PAIN)


# ─────────────────────────────────────────────────────────────────────────────
# Outscraper Fetcher
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_outscraper(business_name: str, city: str, state: str) -> dict:
    """
    Calls Outscraper Maps Search v3 for the lead's business name + location.
    Returns all extracted fields, or None values on failure.
    Cost: $0.002 per call.
    """
    empty = {
        "outscraper_place_id": None,
        "outscraper_rating": None,
        "outscraper_reviews_count": None,
        "outscraper_phone": None,
        "outscraper_full_address": None,
        "outscraper_business_status": None,
        "outscraper_website": None,
        "outscraper_latitude": None,
        "outscraper_longitude": None,
    }

    # Read key from admin_settings table (set via Supabase, not env var)
    try:
        db = get_db()
        key_result = db.table("admin_settings").select("value").eq("key", "outscraper_api_key").execute()
        api_key = key_result.data[0]["value"] if key_result.data else ""
    except Exception:
        api_key = ""
    if not api_key:
        logger.warning("outscraper_api_key not configured in admin_settings — skipping Stage 3")
        return empty

    if not business_name or not city or not state:
        logger.info("Missing business_name/city/state — skipping Outscraper call")
        return empty

    query = f"{business_name} {city} {state}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(
                OUTSCRAPER_URL,
                headers={"X-API-KEY": api_key},
                params={"query": query, "limit": 1, "language": "en"},
            )
            resp.raise_for_status()
            data = resp.json()

        # Outscraper returns: {"status": "OK", "data": [[{...}]]}
        # data is a list of query results; each query result is a list of places
        results_outer = data.get("data", [])
        if not results_outer or not results_outer[0]:
            logger.info(f"No Outscraper results for '{query}'")
            return empty

        place = results_outer[0][0]  # first result of first query

        return {
            "outscraper_place_id":        place.get("place_id"),
            "outscraper_rating":          _safe_float(place.get("rating")),
            "outscraper_reviews_count":   _safe_int(place.get("reviews")),
            "outscraper_phone":           place.get("phone") or place.get("phone_international"),
            "outscraper_full_address":    place.get("full_address"),
            "outscraper_business_status": place.get("business_status"),
            "outscraper_website":         place.get("site") or place.get("website"),
            "outscraper_latitude":        _safe_float(place.get("latitude")),
            "outscraper_longitude":       _safe_float(place.get("longitude")),
        }

    except httpx.TimeoutException:
        logger.warning(f"Outscraper timeout for '{query}'")
    except httpx.HTTPStatusError as e:
        logger.warning(f"Outscraper HTTP {e.response.status_code} for '{query}'")
        if e.response.status_code == 402:
            logger.error("Outscraper: Payment required — check account credits")
        elif e.response.status_code == 401:
            logger.error("Outscraper: Invalid API key")
    except Exception as e:
        logger.warning(f"Outscraper error for '{query}': {type(e).__name__}: {e}")

    return empty


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _safe_int(val) -> Optional[int]:
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


async def _log_cost(lead_id: str) -> None:
    """Logs one Outscraper API call ($0.002) to api_cost_log."""
    db = get_db()
    try:
        db.table("api_cost_log").insert({
            "lead_id": lead_id,
            "service": "outscraper",
            "cost_per_unit": OUTSCRAPER_COST_PER_RECORD,
            "records_processed": 1,
            "source_system": "kjle",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"Cost log failed for lead {lead_id}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Core Stage 3 Runner
# ─────────────────────────────────────────────────────────────────────────────

async def _run_stage3(lead: dict) -> dict:
    """
    Runs Outscraper enrichment on a single lead dict.
    Always returns a complete update payload — never raises.
    """
    lead_id = lead["id"]
    business_name = (lead.get("business_name") or "").strip()
    city = (lead.get("city") or "").strip()
    state = (lead.get("state") or "").strip()

    outscraper_data = await _fetch_outscraper(business_name, city, state)

    # Always log cost — we paid for the API call regardless of result
    await _log_cost(lead_id)

    return {
        **outscraper_data,
        "enrichment_stage": 3,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /stage3/single/{lead_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/stage3/single/{lead_id}")
async def enrich_stage3_single(lead_id: str, min_pain: int = DEFAULT_MIN_PAIN):
    """
    Enrich one Stage 2 lead with Outscraper Google Maps data.
    Pain gate enforced: will not process leads with pain_score < max(min_pain, 60).
    """
    db = get_db()
    effective_min = max(min_pain, ABSOLUTE_MIN_PAIN)

    result = (
        db.table("leads")
        .select(
            "id, business_name, city, state, website, "
            "enrichment_stage, pain_score, niche_slug"
        )
        .eq("id", lead_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail=f"Lead {lead_id} not found")

    lead = result.data[0]
    pain = lead.get("pain_score") or 0
    stage = lead.get("enrichment_stage") or 0

    # Stage gate (Stage 2 / Yelp removed — gate is now Stage 1)
    if stage < 1:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Lead {lead_id} is at enrichment_stage {stage}. "
                "Stage 3 requires enrichment_stage >= 1. "
                "Run Stage 1 first."
            ),
        )

    # Pain gate — hard block
    if pain < effective_min:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Lead {lead_id} has pain_score={pain}, "
                f"which is below the minimum threshold of {effective_min}. "
                "Stage 3 is pain-gated to control Outscraper costs."
            ),
        )

    update_payload = await _run_stage3(lead)
    db.table("leads").update(update_payload).eq("id", lead_id).execute()

    return {
        "status": "success",
        "lead_id": lead_id,
        "business_name": lead.get("business_name"),
        "pain_score": pain,
        "cost_usd": OUTSCRAPER_COST_PER_RECORD,
        "enrichment_result": {
            "outscraper_place_id":        update_payload["outscraper_place_id"],
            "outscraper_rating":          update_payload["outscraper_rating"],
            "outscraper_reviews_count":   update_payload["outscraper_reviews_count"],
            "outscraper_phone":           update_payload["outscraper_phone"],
            "outscraper_full_address":    update_payload["outscraper_full_address"],
            "outscraper_business_status": update_payload["outscraper_business_status"],
            "outscraper_website":         update_payload["outscraper_website"],
            "outscraper_latitude":        update_payload["outscraper_latitude"],
            "outscraper_longitude":       update_payload["outscraper_longitude"],
            "enrichment_stage": 3,
            "enriched_at": update_payload["enriched_at"],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /stage3/batch
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/stage3/batch")
async def enrich_stage3_batch(body: BatchStage3Request):
    """
    Enrich a batch of Stage 2 leads (enrichment_stage = 2, pain_score >= min_pain)
    with Outscraper Google Maps data.
    Returns summary with processed, succeeded, failed, skipped, and total cost.
    """
    db = get_db()
    effective_min = body.effective_min_pain()

    query = (
        db.table("leads")
        .select(
            "id, business_name, city, state, website, "
            "enrichment_stage, pain_score, niche_slug"
        )
        .eq("enrichment_stage", 1)  # Stage 2/Yelp removed
        .eq("is_active", True)
        .gte("pain_score", effective_min)
    )

    if body.niche_slug:
        query = query.eq("niche_slug", body.niche_slug)

    if body.state:
        query = query.eq("state", body.state.upper())

    query = query.order("pain_score", desc=True).limit(body.limit)
    result = query.execute()
    leads = result.data or []

    if not leads:
        return {
            "status": "success",
            "message": f"No Stage 1 leads found with pain_score >= {effective_min}",
            "summary": {
                "processed": 0,
                "succeeded": 0,
                "failed": 0,
                "skipped": 0,
                "total_cost_usd": 0.0,
            },
            "filters": {**body.model_dump(), "effective_min_pain": effective_min},
        }

    processed = 0
    succeeded = 0
    failed = 0
    skipped = 0
    results_detail = []

    for lead in leads:
        lead_id = lead["id"]
        pain = lead.get("pain_score") or 0

        # Double-check pain gate at the record level (defensive)
        if pain < ABSOLUTE_MIN_PAIN:
            skipped += 1
            results_detail.append({
                "lead_id": lead_id,
                "business_name": lead.get("business_name"),
                "status": "skipped",
                "reason": f"pain_score={pain} below floor of {ABSOLUTE_MIN_PAIN}",
            })
            continue

        processed += 1

        try:
            update_payload = await _run_stage3(lead)
            db.table("leads").update(update_payload).eq("id", lead_id).execute()
            succeeded += 1
            results_detail.append({
                "lead_id": lead_id,
                "business_name": lead.get("business_name"),
                "pain_score": pain,
                "status": "enriched",
                "outscraper_rating":        update_payload["outscraper_rating"],
                "outscraper_reviews_count": update_payload["outscraper_reviews_count"],
                "outscraper_place_id":      update_payload["outscraper_place_id"],
            })

        except Exception as e:
            failed += 1
            logger.error(f"Stage 3 batch failed for lead {lead_id}: {e}")

            # Advance stage to prevent infinite retry
            try:
                db.table("leads").update({
                    "outscraper_place_id": None,
                    "outscraper_rating": None,
                    "outscraper_reviews_count": None,
                    "outscraper_phone": None,
                    "outscraper_full_address": None,
                    "outscraper_business_status": None,
                    "outscraper_website": None,
                    "outscraper_latitude": None,
                    "outscraper_longitude": None,
                    "enrichment_stage": 3,
                    "enriched_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", lead_id).execute()
            except Exception as inner_e:
                logger.error(f"Fallback stage advance failed for {lead_id}: {inner_e}")

            results_detail.append({
                "lead_id": lead_id,
                "business_name": lead.get("business_name"),
                "pain_score": pain,
                "status": "failed",
                "error": str(e),
            })

    total_cost = round(succeeded * OUTSCRAPER_COST_PER_RECORD, 4)

    return {
        "status": "success",
        "summary": {
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "total_cost_usd": total_cost,
            "cost_per_record_usd": OUTSCRAPER_COST_PER_RECORD,
            "outscraper_enabled": True,
        },
        "filters": {**body.model_dump(), "effective_min_pain": effective_min},
        "results": results_detail,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /stage3/cost-estimate
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/stage3/cost-estimate")
async def get_stage3_cost_estimate(
    niche_slug: Optional[str] = None,
    state: Optional[str] = None,
    min_pain: int = DEFAULT_MIN_PAIN,
):
    """
    Returns count of Stage 2 leads eligible for Stage 3 and estimated cost
    at $0.002/lead. Applies pain gate (floor: 60).
    """
    db = get_db()
    effective_min = max(min_pain, ABSOLUTE_MIN_PAIN)

    query = (
        db.table("leads")
        .select("id", count="exact")
        .eq("enrichment_stage", 1)  # Stage 2/Yelp removed
        .eq("is_active", True)
        .gte("pain_score", effective_min)
    )

    if niche_slug:
        query = query.eq("niche_slug", niche_slug)

    if state:
        query = query.eq("state", state.upper())

    result = query.execute()
    eligible_count = result.count if result.count is not None else 0
    estimated_cost = round(eligible_count * OUTSCRAPER_COST_PER_RECORD, 4)

    # Also get total Stage 2 leads (before pain gate) for context
    total_query = (
        db.table("leads")
        .select("id", count="exact")
        .eq("enrichment_stage", 1)  # Stage 2/Yelp removed
        .eq("is_active", True)
    )
    if niche_slug:
        total_query = total_query.eq("niche_slug", niche_slug)
    if state:
        total_query = total_query.eq("state", state.upper())

    total_result = total_query.execute()
    total_stage2 = total_result.count if total_result.count is not None else 0
    gated_out = total_stage2 - eligible_count

    return {
        "status": "success",
        "filters": {
            "niche_slug": niche_slug,
            "state": state,
            "min_pain": min_pain,
            "effective_min_pain": effective_min,
        },
        "estimate": {
            "total_stage1_leads": total_stage2,
            "eligible_for_stage3": eligible_count,
            "gated_out_by_pain": gated_out,
            "cost_per_record_usd": OUTSCRAPER_COST_PER_RECORD,
            "estimated_cost_usd": estimated_cost,
            "outscraper_enabled": True,
        },
        "note": (
            f"Stage 3 pain gate is set to {effective_min}. "
            f"{gated_out} leads excluded due to low pain score."
        ),
    }
