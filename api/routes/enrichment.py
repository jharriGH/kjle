"""
Stage 1 Free Enrichment Engine
KJLE - King James Lead Empire
Routes: /kjle/v1/enrichment/*

Stage 1 is $0 cost — runs on any lead regardless of pain score.
Extracts: website reachability, JSON-LD schema, phone/address signals.
"""

import re
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

STAGE1_COST = 0.0
HTTP_TIMEOUT = 10.0
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; KJLE-Enrichment/1.0; "
        "+https://kjle-api.onrender.com)"
    )
}

PHONE_REGEX = re.compile(
    r"(\+?1[-.\s]?)?(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})"
)
ADDRESS_REGEX = re.compile(
    r"\d{1,5}\s[\w\s]{1,40}(?:street|st|avenue|ave|road|rd|"
    r"boulevard|blvd|drive|dr|lane|ln|way|court|ct|place|pl|"
    r"highway|hwy|suite|ste)\b",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class BatchEnrichRequest(BaseModel):
    limit: int = 50
    niche_slug: Optional[str] = None
    state: Optional[str] = None
    min_pain: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Core Stage 1 Enrichment Logic
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_and_parse(url: str) -> dict:
    """
    Fetches a website URL and extracts all Stage 1 signals.
    Returns a dict of enrichment fields — never raises.
    """
    result = {
        "website_reachable": False,
        "has_schema_markup": False,
        "schema_types": None,
        "has_phone_on_page": False,
        "has_address_on_page": False,
    }

    # Normalize URL
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers=HTTP_HEADERS,
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        result["website_reachable"] = True
        html = response.text

        # ── JSON-LD Schema Detection ──────────────────────────────────────
        soup = BeautifulSoup(html, "html.parser")
        schema_tags = soup.find_all("script", type="application/ld+json")
        detected_types = []

        for tag in schema_tags:
            try:
                data = json.loads(tag.string or "")
                # Handle both single object and @graph array
                if isinstance(data, list):
                    for item in data:
                        t = item.get("@type")
                        if t:
                            if isinstance(t, list):
                                detected_types.extend(t)
                            else:
                                detected_types.append(t)
                elif isinstance(data, dict):
                    # Check @graph
                    if "@graph" in data:
                        for item in data["@graph"]:
                            t = item.get("@type")
                            if t:
                                if isinstance(t, list):
                                    detected_types.extend(t)
                                else:
                                    detected_types.append(t)
                    else:
                        t = data.get("@type")
                        if t:
                            if isinstance(t, list):
                                detected_types.extend(t)
                            else:
                                detected_types.append(t)
            except (json.JSONDecodeError, AttributeError):
                continue

        if detected_types:
            # Deduplicate while preserving order
            seen = set()
            unique_types = []
            for t in detected_types:
                if t not in seen:
                    seen.add(t)
                    unique_types.append(t)
            result["has_schema_markup"] = True
            result["schema_types"] = ",".join(unique_types)

        # ── Phone Detection ───────────────────────────────────────────────
        # Use plain text to avoid HTML tag noise
        page_text = soup.get_text(separator=" ", strip=True)
        result["has_phone_on_page"] = bool(PHONE_REGEX.search(page_text))

        # ── Address Detection ─────────────────────────────────────────────
        result["has_address_on_page"] = bool(ADDRESS_REGEX.search(page_text))

    except httpx.TimeoutException:
        logger.warning(f"Timeout fetching {url}")
    except httpx.HTTPStatusError as e:
        logger.warning(f"HTTP {e.response.status_code} for {url}")
    except Exception as e:
        logger.warning(f"Error fetching {url}: {type(e).__name__}: {e}")

    return result


async def _enrich_lead(lead: dict) -> tuple[bool, dict]:
    """
    Runs Stage 1 enrichment on a single lead dict.
    Returns (success: bool, updated_fields: dict).
    """
    website = (lead.get("website") or "").strip()

    if not website:
        # No website — still advance stage, mark unreachable
        signals = {
            "website_reachable": False,
            "has_schema_markup": False,
            "schema_types": None,
            "has_phone_on_page": False,
            "has_address_on_page": False,
        }
    else:
        signals = await _fetch_and_parse(website)

    now_iso = datetime.now(timezone.utc).isoformat()
    update_payload = {
        **signals,
        "enrichment_stage": 1,
        "enriched_at": now_iso,
    }

    return True, update_payload


async def _log_cost(lead_id: str) -> None:
    """Logs Stage 1 enrichment cost (always $0) to api_cost_log."""
    db = get_db()
    try:
        db.table("api_cost_log").insert({
            "lead_id": lead_id,
            "service": "stage1_scrape",
            "cost_per_unit": STAGE1_COST,
            "records_processed": 1,
            "source_system": "kjle",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"Cost log failed for lead {lead_id}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# POST /stage1/single/{lead_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/stage1/single/{lead_id}")
async def enrich_single_lead(lead_id: str):
    """
    Enrich one lead by ID using Stage 1 (free) enrichment.
    Returns the updated lead fields after enrichment.
    """
    db = get_db()

    # Fetch lead
    result = (
        db.table("leads")
        .select("id, business_name, website, enrichment_stage, niche_slug, state")
        .eq("id", lead_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail=f"Lead {lead_id} not found")

    lead = result.data[0]

    # Run enrichment
    success, update_payload = await _enrich_lead(lead)

    # Persist to Supabase
    update_result = (
        db.table("leads")
        .update(update_payload)
        .eq("id", lead_id)
        .execute()
    )

    # Log cost
    await _log_cost(lead_id)

    updated_lead = update_result.data[0] if update_result.data else {**lead, **update_payload}

    return {
        "status": "success",
        "lead_id": lead_id,
        "business_name": lead.get("business_name"),
        "enrichment_result": {
            "website_reachable": update_payload["website_reachable"],
            "has_schema_markup": update_payload["has_schema_markup"],
            "schema_types": update_payload["schema_types"],
            "has_phone_on_page": update_payload["has_phone_on_page"],
            "has_address_on_page": update_payload["has_address_on_page"],
            "enrichment_stage": 1,
            "enriched_at": update_payload["enriched_at"],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /stage1/batch
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/stage1/batch")
async def enrich_batch(body: BatchEnrichRequest):
    """
    Enrich a batch of unenriched leads (enrichment_stage = 0, is_active = true,
    website not null) using Stage 1 free enrichment.
    Returns summary: processed, succeeded, failed, skipped.
    """
    db = get_db()

    # Build query for unenriched leads
    query = (
        db.table("leads")
        .select("id, business_name, website, enrichment_stage, niche_slug, state")
        .eq("enrichment_stage", 0)
        .eq("is_active", True)
        .not_.is_("website", "null")
        .neq("website", "")
    )

    if body.niche_slug:
        query = query.eq("niche_slug", body.niche_slug)

    if body.state:
        query = query.eq("state", body.state.upper())

    if body.min_pain and body.min_pain > 0:
        query = query.gte("pain_score", body.min_pain)

    query = query.limit(body.limit)
    result = query.execute()

    leads = result.data or []

    if not leads:
        return {
            "status": "success",
            "message": "No unenriched leads found matching filters",
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "filters": {
                "limit": body.limit,
                "niche_slug": body.niche_slug,
                "state": body.state,
                "min_pain": body.min_pain,
            },
        }

    processed = 0
    succeeded = 0
    failed = 0
    skipped = 0
    results_detail = []

    for lead in leads:
        lead_id = lead["id"]
        processed += 1

        try:
            success, update_payload = await _enrich_lead(lead)

            # Persist update
            db.table("leads").update(update_payload).eq("id", lead_id).execute()

            # Log cost
            await _log_cost(lead_id)

            succeeded += 1
            results_detail.append({
                "lead_id": lead_id,
                "business_name": lead.get("business_name"),
                "status": "enriched",
                "website_reachable": update_payload["website_reachable"],
                "has_schema_markup": update_payload["has_schema_markup"],
                "schema_types": update_payload["schema_types"],
            })

        except Exception as e:
            failed += 1
            logger.error(f"Batch enrichment failed for lead {lead_id}: {e}")

            # Still mark as stage 1 so we don't loop forever on bad leads
            try:
                db.table("leads").update({
                    "website_reachable": False,
                    "has_schema_markup": False,
                    "schema_types": None,
                    "has_phone_on_page": False,
                    "has_address_on_page": False,
                    "enrichment_stage": 1,
                    "enriched_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", lead_id).execute()
            except Exception as inner_e:
                logger.error(f"Fallback update failed for {lead_id}: {inner_e}")

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
            "skipped": skipped,
            "total_cost_usd": STAGE1_COST,
        },
        "filters": {
            "limit": body.limit,
            "niche_slug": body.niche_slug,
            "state": body.state,
            "min_pain": body.min_pain,
        },
        "results": results_detail,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_enrichment_status():
    """
    Returns enrichment pipeline status: lead counts per stage (0–4),
    total enriched, total unenriched, and Stage 1 cost estimate ($0).
    """
    db = get_db()

    # Fetch stage counts in one query using Supabase RPC would be ideal,
    # but we'll use targeted selects to stay within the client pattern.
    stage_counts = {}

    for stage in range(5):  # stages 0–4
        result = (
            db.table("leads")
            .select("id", count="exact")
            .eq("enrichment_stage", stage)
            .eq("is_active", True)
            .execute()
        )
        stage_counts[stage] = result.count if result.count is not None else 0

    total_leads = sum(stage_counts.values())
    total_unenriched = stage_counts.get(0, 0)
    total_enriched = total_leads - total_unenriched

    # Count leads with website (eligible for Stage 1)
    eligible_result = (
        db.table("leads")
        .select("id", count="exact")
        .eq("enrichment_stage", 0)
        .eq("is_active", True)
        .not_.is_("website", "null")
        .neq("website", "")
        .execute()
    )
    eligible_for_stage1 = eligible_result.count if eligible_result.count is not None else 0

    return {
        "status": "success",
        "pipeline": {
            "stage_0_unenriched": stage_counts[0],
            "stage_1_free_enriched": stage_counts[1],
            "stage_2_enriched": stage_counts[2],
            "stage_3_enriched": stage_counts[3],
            "stage_4_fully_enriched": stage_counts[4],
        },
        "totals": {
            "total_active_leads": total_leads,
            "total_enriched": total_enriched,
            "total_unenriched": total_unenriched,
            "eligible_for_stage1": eligible_for_stage1,
        },
        "cost_estimate": {
            "stage1_cost_per_lead_usd": STAGE1_COST,
            "stage1_estimated_total_usd": STAGE1_COST,
            "note": "Stage 1 is always free — no API calls, local HTML parsing only",
        },
    }
