"""
Stage 4 Enrichment Engine — Firecrawl Deep Page Extraction
KJLE - King James Lead Empire
Routes: /kjle/v1/enrichment/stage4/*

Stage 4 runs ONLY on leads with enrichment_stage = 3 AND pain_score >= 75.
Cost: $0.005 per record via Firecrawl API.
This is the highest-cost, highest-signal enrichment stage.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import get_db
from ..config import settings
from ..lib import cost_guard

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

FIRECRAWL_URL = "https://api.firecrawl.dev/v1/scrape"
FIRECRAWL_COST_PER_RECORD = 0.005
FIRECRAWL_MARKDOWN_MAX_CHARS = 5000
HTTP_TIMEOUT = 60.0           # Firecrawl deep extraction can take time
ABSOLUTE_MIN_PAIN = 75        # Hard floor — cannot be overridden below this
DEFAULT_MIN_PAIN = 75
DEFAULT_BATCH_LIMIT = 10      # Conservative default — Firecrawl is the priciest stage

EXTRACT_PROMPT = (
    "Extract: business owner name, services offered, service areas, "
    "years in business, any pricing mentioned, unique selling points"
)


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class BatchStage4Request(BaseModel):
    limit: int = DEFAULT_BATCH_LIMIT
    niche_slug: Optional[str] = None
    state: Optional[str] = None
    min_pain: int = DEFAULT_MIN_PAIN

    def effective_min_pain(self) -> int:
        """Enforces the absolute pain floor — cannot go below 75."""
        return max(self.min_pain, ABSOLUTE_MIN_PAIN)


# ─────────────────────────────────────────────────────────────────────────────
# Firecrawl Fetcher
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_firecrawl(website: str) -> dict:
    """
    Calls Firecrawl API to deep-extract page content and structured business data.
    Returns all extracted fields, or None values on failure.
    Cost: $0.005 per call.
    """
    empty = {
        "firecrawl_markdown": None,
        "firecrawl_owner_name": None,
        "firecrawl_services": None,
        "firecrawl_service_areas": None,
        "firecrawl_years_in_business": None,
        "firecrawl_usp": None,
    }

    api_key = settings.FIRECRAWL_API_KEY
    if not api_key:
        logger.warning("FIRECRAWL_API_KEY not configured — skipping Stage 4")
        return empty

    if not website:
        logger.info("No website URL — skipping Firecrawl call")
        return empty

    # ── Hard budget guard ────────────────────────────────────────────────────
    # Firecrawl is a flat per-scrape charge — $0.005 reference rate is
    # conservative-low; tunable via a future admin_setting if billing data
    # suggests adjusting.
    if not await cost_guard.check_budget(
        service="firecrawl",
        estimated_cost_usd=cost_guard.FIRECRAWL_COST_PER_SCRAPE,
        job_name="enrich_stage4_manual",
        leads_affected=1,
    ):
        logger.warning("Firecrawl call blocked by budget guard")
        return empty

    url = website if website.startswith(("http://", "https://")) else f"https://{website}"

    payload = {
        "url": url,
        "formats": ["markdown", "extract"],
        "extract": {"prompt": EXTRACT_PROMPT},
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(
                FIRECRAWL_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        # Firecrawl v1 response shape:
        # {"success": true, "data": {"markdown": "...", "extract": {...}, ...}}
        if not data.get("success"):
            logger.warning(f"Firecrawl returned success=false for {url}: {data.get('error')}")
            return empty

        fc_data = data.get("data", {})

        # ── Markdown (truncated) ──────────────────────────────────────────────
        raw_markdown = fc_data.get("markdown") or ""
        markdown_truncated = raw_markdown[:FIRECRAWL_MARKDOWN_MAX_CHARS] if raw_markdown else None

        # ── Extracted structured fields ───────────────────────────────────────
        extracted = fc_data.get("extract") or {}

        owner_name = _extract_str(extracted, [
            "business_owner_name", "owner_name", "owner", "contact_name"
        ])
        services = _extract_list_as_csv(extracted, [
            "services_offered", "services", "service_list"
        ])
        service_areas = _extract_list_as_csv(extracted, [
            "service_areas", "areas_served", "coverage_areas", "service_area"
        ])
        years_in_business = _extract_str(extracted, [
            "years_in_business", "years_operating", "founded", "established"
        ])
        usp = _extract_str(extracted, [
            "unique_selling_points", "usp", "unique_selling_proposition",
            "differentiators", "value_proposition"
        ])

        return {
            "firecrawl_markdown":          markdown_truncated,
            "firecrawl_owner_name":        owner_name,
            "firecrawl_services":          services,
            "firecrawl_service_areas":     service_areas,
            "firecrawl_years_in_business": years_in_business,
            "firecrawl_usp":               usp,
        }

    except httpx.TimeoutException:
        logger.warning(f"Firecrawl timeout for {url}")
    except httpx.HTTPStatusError as e:
        logger.warning(f"Firecrawl HTTP {e.response.status_code} for {url}")
        if e.response.status_code == 402:
            logger.error("Firecrawl: Payment required — check account credits/plan")
        elif e.response.status_code == 401:
            logger.error("Firecrawl: Invalid API key")
        elif e.response.status_code == 429:
            logger.error("Firecrawl: Rate limit hit — consider reducing batch size")
    except Exception as e:
        logger.warning(f"Firecrawl error for {url}: {type(e).__name__}: {e}")

    return empty


# ─────────────────────────────────────────────────────────────────────────────
# Extraction Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_str(data: dict, keys: list) -> Optional[str]:
    """Try multiple key aliases and return the first non-empty string found."""
    for key in keys:
        val = data.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip()
        if val and isinstance(val, list) and val:
            # If a list was returned for a string field, join it
            return ", ".join(str(v).strip() for v in val if v)
    return None


def _extract_list_as_csv(data: dict, keys: list) -> Optional[str]:
    """Try multiple key aliases and return a comma-separated string."""
    for key in keys:
        val = data.get(key)
        if not val:
            continue
        if isinstance(val, list):
            items = [str(v).strip() for v in val if v and str(v).strip()]
            return ", ".join(items) if items else None
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Cost Logger
# ─────────────────────────────────────────────────────────────────────────────

async def _log_cost(lead_id: str) -> None:
    """Logs one Firecrawl API call ($0.005) to api_cost_log."""
    db = get_db()
    try:
        db.table("api_cost_log").insert({
            "lead_id": lead_id,
            "service": "firecrawl",
            "cost_per_unit": FIRECRAWL_COST_PER_RECORD,
            "records_processed": 1,
            "source_system": "kjle",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"Cost log failed for lead {lead_id}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Core Stage 4 Runner
# ─────────────────────────────────────────────────────────────────────────────

async def _run_stage4(lead: dict) -> dict:
    """
    Runs Firecrawl deep enrichment on a single lead dict.
    Always returns a complete update payload — never raises.
    """
    lead_id = lead["id"]
    website = (lead.get("website") or lead.get("outscraper_website") or "").strip()

    firecrawl_data = await _fetch_firecrawl(website)

    # Log cost — charged per call regardless of result quality.
    # Legacy api_cost_log insert (fixed $0.005) kept for existing readers.
    await _log_cost(lead_id)

    # New hard-cap ledger: only log if we actually attempted a scrape
    # (skip when firecrawl was blocked or no website was provided).
    if firecrawl_data.get("firecrawl_markdown") is not None \
       or firecrawl_data.get("firecrawl_owner_name") is not None:
        await cost_guard.log_cost(
            stage="stage4",
            service="firecrawl",
            cost_usd=cost_guard.FIRECRAWL_COST_PER_SCRAPE,
            lead_id=lead_id,
            items_fetched=1,
            metadata={"entry_point": "enrich_stage4_manual"},
        )

    return {
        **firecrawl_data,
        "enrichment_stage": 4,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /stage4/single/{lead_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/stage4/single/{lead_id}")
async def enrich_stage4_single(lead_id: str, min_pain: int = DEFAULT_MIN_PAIN):
    """
    Enrich one Stage 3 lead with Firecrawl deep page extraction.
    Pain gate enforced at 75 — cannot be lowered.
    Cost: $0.005 per call.
    """
    db = get_db()
    effective_min = max(min_pain, ABSOLUTE_MIN_PAIN)

    result = (
        db.table("leads")
        .select(
            "id, business_name, website, outscraper_website, city, state, "
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

    # Stage gate
    if stage < 3:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Lead {lead_id} is at enrichment_stage {stage}. "
                "Stage 4 requires enrichment_stage >= 3. "
                "Run Stages 1, 2, and 3 first."
            ),
        )

    # Pain gate — hard block
    if pain < effective_min:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Lead {lead_id} has pain_score={pain}, "
                f"which is below the Stage 4 minimum of {effective_min}. "
                "Stage 4 is pain-gated at 75 to control Firecrawl costs."
            ),
        )

    update_payload = await _run_stage4(lead)
    db.table("leads").update(update_payload).eq("id", lead_id).execute()

    return {
        "status": "success",
        "lead_id": lead_id,
        "business_name": lead.get("business_name"),
        "pain_score": pain,
        "cost_usd": FIRECRAWL_COST_PER_RECORD,
        "enrichment_result": {
            "firecrawl_owner_name":        update_payload["firecrawl_owner_name"],
            "firecrawl_services":          update_payload["firecrawl_services"],
            "firecrawl_service_areas":     update_payload["firecrawl_service_areas"],
            "firecrawl_years_in_business": update_payload["firecrawl_years_in_business"],
            "firecrawl_usp":               update_payload["firecrawl_usp"],
            "firecrawl_markdown_chars":    len(update_payload["firecrawl_markdown"] or ""),
            "enrichment_stage": 4,
            "enriched_at": update_payload["enriched_at"],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /stage4/batch
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/stage4/batch")
async def enrich_stage4_batch(body: BatchStage4Request):
    """
    Enrich a batch of Stage 3 leads (enrichment_stage = 3, pain_score >= min_pain)
    with Firecrawl deep extraction. Default limit is 10 — Firecrawl is the
    highest-cost stage at $0.005/lead.
    Returns summary with processed, succeeded, failed, skipped, and total cost.
    """
    db = get_db()
    effective_min = body.effective_min_pain()

    query = (
        db.table("leads")
        .select(
            "id, business_name, website, outscraper_website, city, state, "
            "enrichment_stage, pain_score, niche_slug"
        )
        .eq("enrichment_stage", 3)
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
            "message": f"No Stage 3 leads found with pain_score >= {effective_min}",
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

        # Double-check pain gate at record level (defensive)
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
            update_payload = await _run_stage4(lead)
            db.table("leads").update(update_payload).eq("id", lead_id).execute()
            succeeded += 1
            results_detail.append({
                "lead_id": lead_id,
                "business_name": lead.get("business_name"),
                "pain_score": pain,
                "status": "enriched",
                "firecrawl_owner_name":    update_payload["firecrawl_owner_name"],
                "firecrawl_services":      update_payload["firecrawl_services"],
                "firecrawl_service_areas": update_payload["firecrawl_service_areas"],
                "firecrawl_usp":           update_payload["firecrawl_usp"],
                "markdown_chars":          len(update_payload["firecrawl_markdown"] or ""),
            })

        except Exception as e:
            failed += 1
            logger.error(f"Stage 4 batch failed for lead {lead_id}: {e}")

            # Advance stage to prevent infinite retry on broken leads
            try:
                db.table("leads").update({
                    "firecrawl_markdown": None,
                    "firecrawl_owner_name": None,
                    "firecrawl_services": None,
                    "firecrawl_service_areas": None,
                    "firecrawl_years_in_business": None,
                    "firecrawl_usp": None,
                    "enrichment_stage": 4,
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

    total_cost = round(succeeded * FIRECRAWL_COST_PER_RECORD, 4)

    return {
        "status": "success",
        "summary": {
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "total_cost_usd": total_cost,
            "cost_per_record_usd": FIRECRAWL_COST_PER_RECORD,
            "firecrawl_enabled": True,
        },
        "filters": {**body.model_dump(), "effective_min_pain": effective_min},
        "results": results_detail,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /stage4/cost-estimate
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/stage4/cost-estimate")
async def get_stage4_cost_estimate(
    niche_slug: Optional[str] = None,
    state: Optional[str] = None,
    min_pain: int = DEFAULT_MIN_PAIN,
):
    """
    Returns count of Stage 3 leads eligible for Stage 4 and estimated cost
    at $0.005/lead. Pain gate floor is 75.
    """
    db = get_db()
    effective_min = max(min_pain, ABSOLUTE_MIN_PAIN)

    # Eligible leads (stage 3, pain gated)
    query = (
        db.table("leads")
        .select("id", count="exact")
        .eq("enrichment_stage", 3)
        .eq("is_active", True)
        .gte("pain_score", effective_min)
    )
    if niche_slug:
        query = query.eq("niche_slug", niche_slug)
    if state:
        query = query.eq("state", state.upper())

    result = query.execute()
    eligible_count = result.count if result.count is not None else 0
    estimated_cost = round(eligible_count * FIRECRAWL_COST_PER_RECORD, 4)

    # Total Stage 3 leads (before pain gate) for context
    total_query = (
        db.table("leads")
        .select("id", count="exact")
        .eq("enrichment_stage", 3)
        .eq("is_active", True)
    )
    if niche_slug:
        total_query = total_query.eq("niche_slug", niche_slug)
    if state:
        total_query = total_query.eq("state", state.upper())

    total_result = total_query.execute()
    total_stage3 = total_result.count if total_result.count is not None else 0
    gated_out = total_stage3 - eligible_count

    return {
        "status": "success",
        "filters": {
            "niche_slug": niche_slug,
            "state": state,
            "min_pain": min_pain,
            "effective_min_pain": effective_min,
        },
        "estimate": {
            "total_stage3_leads": total_stage3,
            "eligible_for_stage4": eligible_count,
            "gated_out_by_pain": gated_out,
            "cost_per_record_usd": FIRECRAWL_COST_PER_RECORD,
            "estimated_cost_usd": estimated_cost,
            "firecrawl_enabled": True,
        },
        "note": (
            f"Stage 4 pain gate is set to {effective_min}. "
            f"{gated_out} leads excluded due to pain score < {effective_min}."
        ),
    }
