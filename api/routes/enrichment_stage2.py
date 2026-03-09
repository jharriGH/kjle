"""
Stage 2 Enrichment Engine — PageSpeed + Yelp
KJLE - King James Lead Empire
Routes: /kjle/v1/enrichment/stage2/*

Stage 2 runs on leads with enrichment_stage = 1.
Calls: Google PageSpeed Insights (free) + Yelp Fusion API (free tier).
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

PAGESPEED_URL = (
    "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    "?url={url}&strategy=mobile"
)
YELP_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"
HTTP_TIMEOUT = 15.0
STAGE2_COST = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class BatchStage2Request(BaseModel):
    limit: int = 25
    niche_slug: Optional[str] = None
    state: Optional[str] = None
    min_pain: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# PageSpeed Fetcher
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_pagespeed(website: str) -> dict:
    """
    Calls Google PageSpeed Insights API (mobile strategy, no key required).
    Returns performance_score (0–100) and page_load_time_ms, or None for both on failure.
    """
    result = {
        "performance_score": None,
        "page_load_time_ms": None,
    }

    if not website:
        return result

    url = website if website.startswith(("http://", "https://")) else f"https://{website}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(PAGESPEED_URL.format(url=url))
            resp.raise_for_status()
            data = resp.json()

        lighthouse = data.get("lighthouseResult", {})
        categories = lighthouse.get("categories", {})
        audits = lighthouse.get("audits", {})

        # Performance score: API returns 0.0–1.0, we store as 0–100
        perf = categories.get("performance", {}).get("score")
        if perf is not None:
            result["performance_score"] = round(float(perf) * 100)

        # Time to Interactive (best proxy for page_load_time_ms)
        interactive = audits.get("interactive", {}).get("numericValue")
        if interactive is not None:
            result["page_load_time_ms"] = round(float(interactive))

    except httpx.TimeoutException:
        logger.warning(f"PageSpeed timeout for {url}")
    except httpx.HTTPStatusError as e:
        logger.warning(f"PageSpeed HTTP {e.response.status_code} for {url}")
    except Exception as e:
        logger.warning(f"PageSpeed error for {url}: {type(e).__name__}: {e}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Yelp Fetcher
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_yelp(business_name: str, city: str, state: str) -> dict:
    """
    Calls Yelp Fusion business search for the lead's business name + location.
    Returns yelp_rating, yelp_review_count, yelp_url, or None for all on failure.
    """
    result = {
        "yelp_rating": None,
        "yelp_review_count": None,
        "yelp_url": None,
    }

    api_key = settings.YELP_API_KEY
    if not api_key:
        logger.info("YELP_API_KEY not configured — skipping Yelp enrichment")
        return result

    if not business_name or not city or not state:
        logger.info("Missing business_name/city/state — skipping Yelp enrichment")
        return result

    location = f"{city}, {state}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(
                YELP_SEARCH_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                params={"term": business_name, "location": location, "limit": 1},
            )
            resp.raise_for_status()
            data = resp.json()

        businesses = data.get("businesses", [])
        if not businesses:
            logger.info(f"No Yelp results for '{business_name}' in {location}")
            return result

        biz = businesses[0]
        result["yelp_rating"] = biz.get("rating")          # float e.g. 4.5
        result["yelp_review_count"] = biz.get("review_count")  # int
        result["yelp_url"] = biz.get("url")                # string

    except httpx.TimeoutException:
        logger.warning(f"Yelp timeout for '{business_name}' in {location}")
    except httpx.HTTPStatusError as e:
        logger.warning(f"Yelp HTTP {e.response.status_code} for '{business_name}'")
    except Exception as e:
        logger.warning(f"Yelp error for '{business_name}': {type(e).__name__}: {e}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Cost Logger
# ─────────────────────────────────────────────────────────────────────────────

async def _log_cost(lead_id: str, service: str) -> None:
    """Logs a Stage 2 API call to api_cost_log (cost always $0)."""
    db = get_db()
    try:
        db.table("api_cost_log").insert({
            "lead_id": lead_id,
            "service": service,
            "cost_per_unit": STAGE2_COST,
            "records_processed": 1,
            "source_system": "kjle",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"Cost log failed for lead {lead_id} / {service}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Core Stage 2 Runner
# ─────────────────────────────────────────────────────────────────────────────

async def _run_stage2(lead: dict) -> dict:
    """
    Runs both PageSpeed and Yelp enrichment on a single lead dict.
    Always returns a complete update payload — never raises.
    """
    lead_id = lead["id"]
    website = (lead.get("website") or "").strip()
    business_name = (lead.get("business_name") or "").strip()
    city = (lead.get("city") or "").strip()
    state = (lead.get("state") or "").strip()

    # Run both concurrently
    import asyncio
    pagespeed_data, yelp_data = await asyncio.gather(
        _fetch_pagespeed(website),
        _fetch_yelp(business_name, city, state),
        return_exceptions=False,
    )

    # Log both API calls
    await _log_cost(lead_id, "pagespeed")
    await _log_cost(lead_id, "yelp_search")

    now_iso = datetime.now(timezone.utc).isoformat()

    return {
        **pagespeed_data,
        **yelp_data,
        "enrichment_stage": 2,
        "enriched_at": now_iso,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /stage2/single/{lead_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/stage2/single/{lead_id}")
async def enrich_stage2_single(lead_id: str):
    """
    Enrich one Stage 1 lead (enrichment_stage = 1) with PageSpeed + Yelp data.
    Returns updated enrichment fields.
    """
    db = get_db()

    result = (
        db.table("leads")
        .select(
            "id, business_name, website, city, state, "
            "enrichment_stage, niche_slug, pain_score"
        )
        .eq("id", lead_id)
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail=f"Lead {lead_id} not found")

    lead = result.data

    if lead.get("enrichment_stage", 0) < 1:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Lead {lead_id} is at enrichment_stage "
                f"{lead.get('enrichment_stage', 0)}. "
                "Stage 2 requires enrichment_stage >= 1. Run Stage 1 first."
            ),
        )

    update_payload = await _run_stage2(lead)

    db.table("leads").update(update_payload).eq("id", lead_id).execute()

    return {
        "status": "success",
        "lead_id": lead_id,
        "business_name": lead.get("business_name"),
        "enrichment_result": {
            "performance_score": update_payload["performance_score"],
            "page_load_time_ms": update_payload["page_load_time_ms"],
            "yelp_rating": update_payload["yelp_rating"],
            "yelp_review_count": update_payload["yelp_review_count"],
            "yelp_url": update_payload["yelp_url"],
            "enrichment_stage": 2,
            "enriched_at": update_payload["enriched_at"],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /stage2/batch
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/stage2/batch")
async def enrich_stage2_batch(body: BatchStage2Request):
    """
    Enrich a batch of Stage 1 leads (enrichment_stage = 1) with PageSpeed + Yelp.
    Returns summary: processed, succeeded, failed.
    """
    db = get_db()

    query = (
        db.table("leads")
        .select(
            "id, business_name, website, city, state, "
            "enrichment_stage, niche_slug, pain_score"
        )
        .eq("enrichment_stage", 1)
        .eq("is_active", True)
    )

    if body.niche_slug:
        query = query.eq("niche_slug", body.niche_slug)

    if body.state:
        query = query.eq("state", body.state.upper())

    if body.min_pain and body.min_pain > 0:
        query = query.gte("pain_score", body.min_pain)

    query = query.order("pain_score", desc=True).limit(body.limit)
    result = query.execute()
    leads = result.data or []

    if not leads:
        return {
            "status": "success",
            "message": "No Stage 1 leads found matching filters",
            "summary": {"processed": 0, "succeeded": 0, "failed": 0},
            "filters": body.model_dump(),
        }

    processed = 0
    succeeded = 0
    failed = 0
    results_detail = []

    for lead in leads:
        lead_id = lead["id"]
        processed += 1

        try:
            update_payload = await _run_stage2(lead)
            db.table("leads").update(update_payload).eq("id", lead_id).execute()
            succeeded += 1
            results_detail.append({
                "lead_id": lead_id,
                "business_name": lead.get("business_name"),
                "status": "enriched",
                "performance_score": update_payload["performance_score"],
                "yelp_rating": update_payload["yelp_rating"],
                "yelp_review_count": update_payload["yelp_review_count"],
            })

        except Exception as e:
            failed += 1
            logger.error(f"Stage 2 batch failed for lead {lead_id}: {e}")

            # Advance stage to avoid infinite retry on bad leads
            try:
                db.table("leads").update({
                    "performance_score": None,
                    "page_load_time_ms": None,
                    "yelp_rating": None,
                    "yelp_review_count": None,
                    "yelp_url": None,
                    "enrichment_stage": 2,
                    "enriched_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", lead_id).execute()
            except Exception as inner_e:
                logger.error(f"Fallback stage advance failed for {lead_id}: {inner_e}")

            results_detail.append({
                "lead_id": lead_id,
                "business_name": lead.get("business_name"),
                "status": "failed",
                "error": str(e),
            })

    return {
        "status": "success",
        "summary": {
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "total_cost_usd": STAGE2_COST,
            "yelp_enabled": bool(settings.YELP_API_KEY),
        },
        "filters": body.model_dump(),
        "results": results_detail,
    }
