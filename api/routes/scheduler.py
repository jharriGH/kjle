"""
Scheduled Automation — APScheduler Cron Jobs
KJLE - King James Lead Empire
Routes: /kjle/v1/scheduler/*

5 background jobs registered at startup via setup_scheduler():
  1. classify_segments    — every 6 hours
  2. enrich_stage1        — every 12 hours
  3. cost_digest          — daily at 08:00 UTC
  4. stale_cleanup        — daily at 02:00 UTC
  5. email_clean_nightly  — daily at 00:00 UTC (midnight), runs up to 7 hours
                            ~16,800 emails/night at 1.5s/email

Call setup_scheduler() inside FastAPI lifespan (main.py).
"""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta, date
from typing import Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import APIRouter, HTTPException, Query

from ..database import get_db
from .segments_engine import _classify_lead
from .enrichment import _fetch_and_parse
from .webhooks import fire_event

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SCHEDULER_LOG_TABLE  = "scheduler_log"
CLASSIFY_LIMIT       = 10_000
ENRICH_STAGE1_LIMIT  = 200      # bumped from 50 — configurable via admin_settings key enrich_stage1_limit
ENRICH_MIN_PAIN      = 0        # Stage 1 is free — run on all leads with websites
STALE_DAYS           = 180
STALE_MAX_PAIN       = 30

# Stage 3 / Stage 4 nightly defaults — all overridable via admin_settings
ENRICH_STAGE3_LIMIT        = 25     # leads per nightly run
ENRICH_STAGE3_MIN_PAIN     = 60     # pain gate
ENRICH_STAGE3_DAILY_BUDGET = 0.10   # USD — hard stop if exceeded
ENRICH_STAGE4_LIMIT        = 10     # leads per nightly run
ENRICH_STAGE4_MIN_PAIN     = 75     # pain gate
ENRICH_STAGE4_DAILY_BUDGET = 0.05   # USD — hard stop if exceeded
OUTSCRAPER_COST_PER_LEAD   = 0.002
FIRECRAWL_COST_PER_LEAD    = 0.005

# Email clean nightly — Render Starter safe limit
# ~36min per 1,000 leads — 2,500 fits within 90min Starter window
EMAIL_CLEAN_NIGHTLY_LIMIT  = 2_500  # safe for Render Starter
EMAIL_CLEAN_PAGE_SIZE      = 1_000  # Supabase max rows per request
EMAIL_CLEAN_NIGHTLY_SLEEP  = 1.5    # seconds between each Truelist request
EMAIL_CLEAN_TRUELIST_URL   = "https://api.truelist.io/api/v1/verify_inline"
EMAIL_CLEAN_MAX_RETRIES    = 3
EMAIL_CLEAN_RETRY_BACKOFF  = 3.0    # seconds to wait on 429

JOB_DEFINITIONS = {
    "classify_segments": {
        "description": "Auto-classify all active leads into hot/warm/cold segments",
        "schedule":    "Every 6 hours",
        "trigger":     "interval",
    },
    "enrich_stage1": {
        "description": "Auto-enrich up to 50 unenriched leads (stage 0, pain >= 50) with Stage 1 free enrichment",
        "schedule":    "Every 12 hours",
        "trigger":     "interval",
    },
    "cost_digest": {
        "description": "Daily cost digest: today's spend by service, guardrail check, fires webhook",
        "schedule":    "Daily at 08:00 UTC",
        "trigger":     "cron",
    },
    "stale_cleanup": {
        "description": "Soft-delete unenriched low-pain leads older than 180 days",
        "schedule":    "Daily at 02:00 UTC",
        "trigger":     "cron",
    },
    "email_clean_nightly": {
        "description": "Nightly Truelist email validation — up to 16,800 leads/night at 1.5s/email (midnight–7am UTC)",
        "schedule":    "Daily at 00:00 UTC",
        "trigger":     "cron",
    },
    "enrich_stage3_nightly": {
        "description": "Nightly Outscraper Google Maps enrichment — Stage 1 leads, pain >= 60, budget-guarded",
        "schedule":    "Daily at 01:00 UTC",
        "trigger":     "cron",
    },
    "enrich_stage4_nightly": {
        "description": "Nightly Firecrawl deep enrichment — Stage 3 leads, pain >= 75, budget-guarded",
        "schedule":    "Daily at 03:00 UTC",
        "trigger":     "cron",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return date.today().isoformat()


async def _log_job(
    job_name: str,
    leads_processed: int = 0,
    hot: int = 0,
    warm: int = 0,
    cold: int = 0,
    duration_seconds: float = 0.0,
    status: str = "success",
    notes: Optional[str] = None,
) -> None:
    """Inserts a scheduler_log record. Silently absorbs failures."""
    db = get_db()
    try:
        db.table(SCHEDULER_LOG_TABLE).insert({
            "job_name":         job_name,
            "leads_processed":  leads_processed,
            "hot":              hot,
            "warm":             warm,
            "cold":             cold,
            "duration_seconds": round(duration_seconds, 3),
            "status":           status,
            "notes":            notes,
            "ran_at":           _now_iso(),
        }).execute()
    except Exception as e:
        logger.error(f"scheduler_log write failed for job '{job_name}': {e}")


def _safe_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _safe_int(val, default=0) -> int:
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Job 1 — Auto-Classify Segments (every 6 hours)
# ─────────────────────────────────────────────────────────────────────────────

async def job_classify_segments() -> dict:
    """
    Fetches all active leads (up to 10,000), runs _classify_lead() on each,
    writes segment_label + segmented_at back to leads table.
    """
    job_name = "classify_segments"
    logger.info(f"[{job_name}] Starting...")
    t_start = time.monotonic()

    db = get_db()
    counts = {"hot": 0, "warm": 0, "cold": 0}
    failed = 0

    leads = (
        db.table("leads")
        .select("id, pain_score, fit_demoenginez, enrichment_stage, website_reachable")
        .eq("is_active", True)
        .limit(CLASSIFY_LIMIT)
        .execute()
        .data or []
    )

    for lead in leads:
        label = _classify_lead(lead)
        counts[label] += 1
        try:
            db.table("leads").update({
                "segment_label": label,
                "segmented_at":  _now_iso(),
            }).eq("id", lead["id"]).execute()
        except Exception as e:
            failed += 1
            logger.error(f"[{job_name}] Update failed for lead {lead['id']}: {e}")

    duration = time.monotonic() - t_start
    notes = f"failed_writes={failed}" if failed else None
    status = "partial" if failed > 0 else "success"

    await _log_job(
        job_name,
        leads_processed=len(leads),
        hot=counts["hot"],
        warm=counts["warm"],
        cold=counts["cold"],
        duration_seconds=duration,
        status=status,
        notes=notes,
    )

    result = {
        "job": job_name,
        "leads_processed": len(leads),
        "hot":   counts["hot"],
        "warm":  counts["warm"],
        "cold":  counts["cold"],
        "failed_writes": failed,
        "duration_seconds": round(duration, 3),
        "status": status,
    }
    logger.info(f"[{job_name}] Done. {result}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Job 2 — Auto-Enrich Stage 1 (every 12 hours)
# ─────────────────────────────────────────────────────────────────────────────

async def job_enrich_stage1() -> dict:
    """
    Fetches up to 50 unenriched leads (stage=0, pain>=50, website not null),
    runs Stage 1 free enrichment on each (website scrape, JSON-LD, phone/address).
    """
    job_name = "enrich_stage1"
    logger.info(f"[{job_name}] Starting...")
    t_start = time.monotonic()

    db = get_db()

    leads = (
        db.table("leads")
        .select("id, website, business_name, pain_score")
        .eq("is_active", True)
        .eq("enrichment_stage", 0)
        .gte("pain_score", ENRICH_MIN_PAIN)
        .not_.is_("website", "null")
        .neq("website", "")
        .order("pain_score", desc=True)
        .limit(ENRICH_STAGE1_LIMIT)
        .execute()
        .data or []
    )

    succeeded = 0
    failed = 0

    for lead in leads:
        lead_id = lead["id"]
        website = (lead.get("website") or "").strip()

        try:
            signals = await _fetch_and_parse(website)
            update_payload = {
                **signals,
                "enrichment_stage": 1,
                "enriched_at":      _now_iso(),
            }
            db.table("leads").update(update_payload).eq("id", lead_id).execute()

            # Log cost ($0)
            db.table("api_cost_log").insert({
                "lead_id":           lead_id,
                "service":           "stage1_scrape",
                "cost_per_unit":     0.0,
                "records_processed": 1,
                "source_system":     "kjle",
                "created_at":        _now_iso(),
            }).execute()

            succeeded += 1

        except Exception as e:
            failed += 1
            logger.error(f"[{job_name}] Enrichment failed for lead {lead_id}: {e}")
            # Advance stage to avoid infinite retry on persistently broken websites
            try:
                db.table("leads").update({
                    "enrichment_stage": 1,
                    "enriched_at":      _now_iso(),
                }).eq("id", lead_id).execute()
            except Exception as inner:
                logger.error(f"[{job_name}] Could not advance stage for lead {lead_id}: {inner}")

    duration = time.monotonic() - t_start
    notes = f"succeeded={succeeded}, failed={failed}"
    status = "partial" if failed > 0 and succeeded > 0 else ("failed" if failed > 0 else "success")

    await _log_job(
        job_name,
        leads_processed=succeeded,
        duration_seconds=duration,
        status=status,
        notes=notes,
    )

    result = {
        "job":              job_name,
        "leads_processed":  len(leads),
        "succeeded":        succeeded,
        "failed":           failed,
        "duration_seconds": round(duration, 3),
        "status":           status,
    }
    logger.info(f"[{job_name}] Done. {result}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Job 3 — Cost Digest (daily 08:00 UTC)
# ─────────────────────────────────────────────────────────────────────────────

async def job_cost_digest() -> dict:
    """
    Daily cost digest: calculates today's spend by service,
    checks against guardrails, fires webhook if any breached.
    """
    job_name = "cost_digest"
    logger.info(f"[{job_name}] Starting...")
    t_start = time.monotonic()

    db = get_db()
    today = _today_iso()

    # Fetch today's cost log
    rows = (
        db.table("api_cost_log")
        .select("service, cost_per_unit, records_processed")
        .gte("created_at", today)
        .execute()
        .data or []
    )

    # Aggregate by service
    service_map: dict = {}
    for row in rows:
        svc  = row.get("service") or "unknown"
        cost = _safe_float(row.get("cost_per_unit", 0)) * _safe_int(row.get("records_processed", 1))
        service_map[svc] = service_map.get(svc, 0.0) + cost

    total_today = round(sum(service_map.values()), 6)

    # Check guardrails
    guardrails = (
        db.table("budget_guardrails")
        .select("*")
        .eq("active", True)
        .execute()
        .data or []
    )

    breached_guardrails = []
    for g in guardrails:
        period = g.get("period")
        if period != "daily":
            continue
        svc   = g.get("service")
        limit = _safe_float(g.get("limit_amount", 0))
        spent = service_map.get(svc, 0.0) if svc else total_today
        if spent >= limit:
            breached_guardrails.append({
                "service": svc or "global",
                "spent":   round(spent, 6),
                "limit":   limit,
            })

    # Fire webhook if any guardrails breached
    if breached_guardrails:
        try:
            await fire_event("cost.guardrail_breached", {
                "date":     today,
                "breached": breached_guardrails,
                "total_today_usd": total_today,
            })
        except Exception as e:
            logger.error(f"[{job_name}] Webhook fire failed: {e}")

    duration = time.monotonic() - t_start
    notes = (
        f"total_today=${total_today:.4f}, "
        f"services={len(service_map)}, "
        f"guardrails_breached={len(breached_guardrails)}"
    )

    await _log_job(
        job_name,
        duration_seconds=duration,
        status="success",
        notes=notes,
    )

    result = {
        "job":               job_name,
        "date":              today,
        "total_today_usd":   total_today,
        "by_service":        service_map,
        "guardrails_breached": len(breached_guardrails),
        "duration_seconds":  round(duration, 3),
        "status":            "success",
    }
    logger.info(f"[{job_name}] Done. {result}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Job 4 — Stale Lead Cleanup (02:00 UTC)
# ─────────────────────────────────────────────────────────────────────────────

async def job_stale_cleanup() -> dict:
    """
    Soft-deletes leads that are:
    - is_active = true
    - created_at < now() - 180 days
    - enrichment_stage = 0 (never enriched)
    - pain_score < 30 (low value)
    Sets is_active=false on matching leads.
    """
    job_name = "stale_cleanup"
    logger.info(f"[{job_name}] Starting...")
    t_start = time.monotonic()

    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)).isoformat()

    stale_leads = (
        db.table("leads")
        .select("id")
        .eq("is_active", True)
        .eq("enrichment_stage", 0)
        .lt("pain_score", STALE_MAX_PAIN)
        .lt("created_at", cutoff)
        .execute()
        .data or []
    )

    deactivated = 0
    failed = 0
    now = _now_iso()

    for lead in stale_leads:
        try:
            db.table("leads").update({
                "is_active":  False,
                "updated_at": now,
            }).eq("id", lead["id"]).execute()
            deactivated += 1
        except Exception as e:
            failed += 1
            logger.error(f"[{job_name}] Deactivation failed for lead {lead['id']}: {e}")

    duration = time.monotonic() - t_start
    notes = (
        f"stale_found={len(stale_leads)}, deactivated={deactivated}, "
        f"failed={failed}, cutoff={cutoff[:10]}, max_pain={STALE_MAX_PAIN}"
    )
    status = "partial" if failed > 0 and deactivated > 0 else ("failed" if failed > 0 else "success")

    await _log_job(
        job_name,
        leads_processed=deactivated,
        duration_seconds=duration,
        status=status,
        notes=notes,
    )

    result = {
        "job":              job_name,
        "stale_found":      len(stale_leads),
        "deactivated":      deactivated,
        "failed":           failed,
        "cutoff_date":      cutoff[:10],
        "duration_seconds": round(duration, 3),
        "status":           status,
    }
    logger.info(f"[{job_name}] Done. {result}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Job 5 — Nightly Email Clean (00:00 UTC — runs through 7am window)
# ~16,800 emails/night at 1.5s/email
# ─────────────────────────────────────────────────────────────────────────────

async def job_email_clean_nightly() -> dict:
    """
    Nightly Truelist.io email validation job.
    - Fires at midnight UTC
    - Processes uncleaned leads up to 7-hour window (16,800 max)
    - Rate: 1.5s per email (safely under 1 req/sec Truelist limit)
    - Skips already-cleaned leads (only_uncleaned=True)
    - Retries on 429 with exponential backoff
    - Prioritizes HOT leads first, then WARM, then COLD
    """
    job_name = "email_clean_nightly"
    logger.info(f"[{job_name}] Starting nightly email clean — up to {EMAIL_CLEAN_NIGHTLY_LIMIT:,} leads")
    t_start = time.monotonic()

    db = get_db()

    # Load Truelist API key from admin_settings
    api_key = None
    try:
        res = db.table("admin_settings").select("value").eq("key", "truelist_api_key").execute()
        if res.data and res.data[0].get("value"):
            api_key = res.data[0]["value"].strip() or None
    except Exception as e:
        logger.warning(f"[{job_name}] Could not load truelist_api_key: {e}")

    if not api_key:
        notes = "SKIPPED — truelist_api_key not set in admin_settings"
        logger.warning(f"[{job_name}] {notes}")
        await _log_job(job_name, status="skipped", notes=notes)
        return {"job": job_name, "status": "skipped", "reason": notes}

    # Check email_clean_enabled setting
    try:
        enabled_res = db.table("admin_settings").select("value").eq("key", "email_clean_enabled").execute()
        enabled = (enabled_res.data or [{}])[0].get("value", "false").lower() in ("true", "1", "yes")
    except Exception:
        enabled = False

    if not enabled:
        notes = "SKIPPED — email_clean_enabled is false in admin_settings"
        logger.warning(f"[{job_name}] {notes}")
        await _log_job(job_name, status="skipped", notes=notes)
        return {"job": job_name, "status": "skipped", "reason": notes}

    # Fetch uncleaned leads in pages of 1,000 (Supabase default row limit)
    # Paginate until we hit EMAIL_CLEAN_NIGHTLY_LIMIT or run out of leads
    # Prioritize HOT leads first (highest pain_score DESC)
    all_leads = []
    offset = 0
    while len(all_leads) < EMAIL_CLEAN_NIGHTLY_LIMIT:
        remaining = EMAIL_CLEAN_NIGHTLY_LIMIT - len(all_leads)
        batch_size = min(EMAIL_CLEAN_PAGE_SIZE, remaining)
        batch = (
            db.table("leads")
            .select("id, email, segment_label")
            .eq("is_active", True)
            .not_.is_("email", "null")
            .neq("email", "")
            .is_("email_cleaned_at", "null")
            .order("pain_score", desc=True)
            .range(offset, offset + batch_size - 1)
            .execute()
            .data or []
        )
        if not batch:
            break
        all_leads.extend(batch)
        if len(batch) < batch_size:
            break
        offset += batch_size

    leads = all_leads
    total         = len(leads)
    valid_count   = 0
    invalid_count = 0
    unknown_count = 0
    failed_count  = 0

    logger.info(f"[{job_name}] Found {total:,} uncleaned leads to process (limit={EMAIL_CLEAN_NIGHTLY_LIMIT:,})")

    async with httpx.AsyncClient() as client:
        for i, lead in enumerate(leads):
            lead_id = lead["id"]
            email   = lead["email"]

            try:
                # POST with retry on 429
                data = None
                for attempt in range(1, EMAIL_CLEAN_MAX_RETRIES + 1):
                    r = await client.post(
                        EMAIL_CLEAN_TRUELIST_URL,
                        headers={"Authorization": f"Bearer {api_key}"},
                        params={"email": email},
                        timeout=15.0,
                    )
                    if r.status_code == 429:
                        wait = EMAIL_CLEAN_RETRY_BACKOFF * attempt
                        logger.warning(f"[{job_name}] 429 rate limit — attempt {attempt}/{EMAIL_CLEAN_MAX_RETRIES}, waiting {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    if r.status_code == 200:
                        data = r.json()
                        break
                    else:
                        logger.error(f"[{job_name}] HTTP {r.status_code} for {email}")
                        break

                if data is None:
                    raise Exception(f"No valid response after {EMAIL_CLEAN_MAX_RETRIES} attempts")

                # Parse response — flat emails[] array
                email_obj = data["emails"][0]
                state = email_obj.get("email_state", "unknown")

                if state == "ok":
                    email_valid, email_status = True, "valid"
                    valid_count += 1
                elif state == "bad":
                    email_valid, email_status = False, "invalid"
                    invalid_count += 1
                else:
                    email_valid, email_status = None, "unknown"
                    unknown_count += 1

                db.table("leads").update({
                    "email_valid":      email_valid,
                    "email_status":     email_status,
                    "email_cleaned_at": _now_iso(),
                }).eq("id", lead_id).execute()

                if (i + 1) % 500 == 0:
                    logger.info(
                        f"[{job_name}] Progress: {i+1:,}/{total:,} — "
                        f"valid={valid_count}, invalid={invalid_count}, "
                        f"unknown={unknown_count}, failed={failed_count}"
                    )

            except Exception as e:
                failed_count += 1
                logger.error(f"[{job_name}] FAILED lead={lead_id} email={email} error={e}")
                try:
                    db.table("leads").update({
                        "email_valid":      None,
                        "email_status":     "error",
                        "email_cleaned_at": _now_iso(),
                    }).eq("id", lead_id).execute()
                except Exception:
                    pass

            await asyncio.sleep(EMAIL_CLEAN_NIGHTLY_SLEEP)

    duration = time.monotonic() - t_start
    notes = (
        f"processed={total}, valid={valid_count}, invalid={invalid_count}, "
        f"unknown={unknown_count}, failed={failed_count}, "
        f"elapsed={round(duration/3600, 2)}hrs"
    )
    status = "partial" if failed_count > 0 and (valid_count + invalid_count + unknown_count) > 0 else "success"

    await _log_job(
        job_name,
        leads_processed=total,
        duration_seconds=duration,
        status=status,
        notes=notes,
    )

    result = {
        "job":              job_name,
        "total_processed":  total,
        "valid":            valid_count,
        "invalid":          invalid_count,
        "unknown":          unknown_count,
        "failed":           failed_count,
        "duration_hours":   round(duration / 3600, 2),
        "status":           status,
    }
    logger.info(f"[{job_name}] Done. {result}")
    return result




# ─────────────────────────────────────────────────────────────────────────────
# Budget Guard — checks spend before running paid enrichment jobs
# ─────────────────────────────────────────────────────────────────────────────

async def _get_admin_setting(key: str, default):
    """Reads a single value from admin_settings table. Returns default on any error."""
    try:
        db = get_db()
        res = db.table("admin_settings").select("value").eq("key", key).execute()
        if res.data and res.data[0].get("value") is not None:
            val = res.data[0]["value"]
            # Try numeric conversion if default is numeric
            if isinstance(default, float):
                return float(val)
            if isinstance(default, int):
                return int(val)
            return val
    except Exception:
        pass
    return default


async def _get_today_spend(service: str) -> float:
    """Returns total spend for a service today from api_cost_log."""
    try:
        db = get_db()
        today = _today_iso()
        rows = (
            db.table("api_cost_log")
            .select("cost_per_unit, records_processed")
            .eq("service", service)
            .gte("created_at", today)
            .execute()
            .data or []
        )
        return round(sum(
            _safe_float(r.get("cost_per_unit", 0)) * _safe_int(r.get("records_processed", 1))
            for r in rows
        ), 6)
    except Exception:
        return 0.0


async def _budget_guard(service: str, cost_per_lead: float, batch_limit: int, daily_cap: float) -> tuple[bool, int, str]:
    """
    Checks budget before running a paid enrichment job.
    Returns (allowed: bool, safe_limit: int, reason: str)
    - allowed: False = skip entire job
    - safe_limit: how many leads we can afford today within the cap
    - reason: human-readable explanation for scheduler log
    """
    today_spend = await _get_today_spend(service)
    remaining_budget = daily_cap - today_spend

    if remaining_budget <= 0:
        return False, 0, f"daily_cap_reached — spent ${today_spend:.4f} of ${daily_cap:.2f} cap for {service}"

    affordable_leads = int(remaining_budget / cost_per_lead)
    safe_limit = min(batch_limit, affordable_leads)

    if safe_limit <= 0:
        return False, 0, f"budget_too_low — ${remaining_budget:.4f} remaining, need ${cost_per_lead:.3f}/lead for {service}"

    reason = (
        f"budget_ok — ${today_spend:.4f} spent today, ${remaining_budget:.4f} remaining, "
        f"cap=${daily_cap:.2f}, running {safe_limit} leads"
    )
    return True, safe_limit, reason


# ─────────────────────────────────────────────────────────────────────────────
# Job 6 — Nightly Stage 3 Enrichment (01:00 UTC)
# Outscraper Google Maps — pain-gated, budget-guarded
# ─────────────────────────────────────────────────────────────────────────────

async def job_enrich_stage3_nightly() -> dict:
    """
    Nightly Outscraper enrichment job.
    - Fires at 01:00 UTC
    - Processes Stage 1 leads with pain_score >= enrich_stage3_min_pain (default 60)
    - Batch size from admin_settings key enrich_stage3_nightly_limit (default 25)
    - Daily budget cap from admin_settings key enrich_stage3_daily_budget_usd (default $0.10)
    - Skips entirely if daily budget cap already reached
    - Orders by pain_score DESC — hottest leads first
    - Cost: $0.002/lead via Outscraper
    """
    job_name = "enrich_stage3_nightly"
    logger.info(f"[{job_name}] Starting...")
    t_start = time.monotonic()

    from ..config import settings

    # Load configurable settings
    batch_limit = int(await _get_admin_setting("enrich_stage3_nightly_limit", ENRICH_STAGE3_LIMIT))
    min_pain    = int(await _get_admin_setting("enrich_stage3_min_pain", ENRICH_STAGE3_MIN_PAIN))
    daily_cap   = float(await _get_admin_setting("enrich_stage3_daily_budget_usd", ENRICH_STAGE3_DAILY_BUDGET))

    # Budget Guard
    allowed, safe_limit, budget_reason = await _budget_guard(
        "outscraper", OUTSCRAPER_COST_PER_LEAD, batch_limit, daily_cap
    )
    if not allowed:
        logger.warning(f"[{job_name}] Skipped — {budget_reason}")
        await _log_job(job_name, status="skipped", notes=budget_reason)
        return {"job": job_name, "status": "skipped", "reason": budget_reason}

    logger.info(f"[{job_name}] {budget_reason}")

    # Verify Outscraper API key
    api_key = settings.OUTSCRAPER_API_KEY
    if not api_key:
        notes = "SKIPPED — OUTSCRAPER_API_KEY not set in environment"
        logger.warning(f"[{job_name}] {notes}")
        await _log_job(job_name, status="skipped", notes=notes)
        return {"job": job_name, "status": "skipped", "reason": notes}

    db = get_db()

    # Fetch eligible leads: Stage 1, pain >= min_pain, ordered by pain DESC
    leads = (
        db.table("leads")
        .select("id, business_name, city, state, website, pain_score, niche_slug")
        .eq("enrichment_stage", 1)
        .eq("is_active", True)
        .gte("pain_score", min_pain)
        .order("pain_score", desc=True)
        .limit(safe_limit)
        .execute()
        .data or []
    )

    if not leads:
        notes = f"no_eligible_leads — stage=1, pain>={min_pain}"
        await _log_job(job_name, status="success", notes=notes)
        return {"job": job_name, "status": "success", "leads_found": 0, "reason": notes}

    succeeded = 0
    failed = 0
    total_cost = 0.0

    for lead in leads:
        lead_id = lead["id"]
        business_name = (lead.get("business_name") or "").strip()
        city  = (lead.get("city") or "").strip()
        state = (lead.get("state") or "").strip()

        try:
            query = f"{business_name} {city} {state}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    "https://api.app.outscraper.com/maps/search-v3",
                    headers={"X-API-KEY": api_key},
                    params={"query": query, "limit": 1, "language": "en", "async": "false"},
                )
                resp.raise_for_status()
                data = resp.json()

            results_outer = data.get("data", [])
            place = (results_outer[0][0] if results_outer and results_outer[0] else {})

            update_payload = {
                "outscraper_place_id":        place.get("place_id"),
                "outscraper_rating":          float(place["rating"]) if place.get("rating") is not None else None,
                "outscraper_reviews_count":   int(place["reviews"]) if place.get("reviews") is not None else None,
                "outscraper_phone":           place.get("phone") or place.get("phone_international"),
                "outscraper_full_address":    place.get("full_address"),
                "outscraper_business_status": place.get("business_status"),
                "outscraper_website":         place.get("site") or place.get("website"),
                "outscraper_latitude":        float(place["latitude"]) if place.get("latitude") is not None else None,
                "outscraper_longitude":       float(place["longitude"]) if place.get("longitude") is not None else None,
                "enrichment_stage": 3,
                "enriched_at": _now_iso(),
            }

            db.table("leads").update(update_payload).eq("id", lead_id).execute()

            # Log cost
            db.table("api_cost_log").insert({
                "lead_id": lead_id,
                "service": "outscraper",
                "cost_per_unit": OUTSCRAPER_COST_PER_LEAD,
                "records_processed": 1,
                "source_system": "kjle",
                "created_at": _now_iso(),
            }).execute()

            total_cost += OUTSCRAPER_COST_PER_LEAD
            succeeded += 1

        except Exception as e:
            failed += 1
            logger.error(f"[{job_name}] Failed for lead {lead_id}: {e}")
            # Advance stage to prevent infinite retry
            try:
                db.table("leads").update({
                    "enrichment_stage": 3,
                    "enriched_at": _now_iso(),
                }).eq("id", lead_id).execute()
            except Exception:
                pass

    duration = time.monotonic() - t_start
    notes = (
        f"succeeded={succeeded}, failed={failed}, "
        f"total_cost=${round(total_cost, 4):.4f}, "
        f"pain_gate={min_pain}, budget_cap=${daily_cap:.2f}"
    )
    status = "partial" if failed > 0 and succeeded > 0 else ("failed" if succeeded == 0 and failed > 0 else "success")

    await _log_job(job_name, leads_processed=succeeded, duration_seconds=duration, status=status, notes=notes)

    result = {
        "job": job_name,
        "leads_processed": len(leads),
        "succeeded": succeeded,
        "failed": failed,
        "total_cost_usd": round(total_cost, 4),
        "duration_seconds": round(duration, 3),
        "status": status,
    }
    logger.info(f"[{job_name}] Done. {result}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Job 7 — Nightly Stage 4 Enrichment (03:00 UTC)
# Firecrawl deep extraction — pain-gated, budget-guarded
# ─────────────────────────────────────────────────────────────────────────────

async def job_enrich_stage4_nightly() -> dict:
    """
    Nightly Firecrawl deep enrichment job.
    - Fires at 03:00 UTC (after Stage 3 completes)
    - Processes Stage 3 leads with pain_score >= enrich_stage4_min_pain (default 75)
    - Batch size from admin_settings key enrich_stage4_nightly_limit (default 10)
    - Daily budget cap from admin_settings key enrich_stage4_daily_budget_usd (default $0.05)
    - Skips entirely if daily budget cap already reached
    - Orders by pain_score DESC — hottest leads first
    - Cost: $0.005/lead via Firecrawl
    """
    job_name = "enrich_stage4_nightly"
    logger.info(f"[{job_name}] Starting...")
    t_start = time.monotonic()

    from ..config import settings

    # Load configurable settings
    batch_limit = int(await _get_admin_setting("enrich_stage4_nightly_limit", ENRICH_STAGE4_LIMIT))
    min_pain    = int(await _get_admin_setting("enrich_stage4_min_pain", ENRICH_STAGE4_MIN_PAIN))
    daily_cap   = float(await _get_admin_setting("enrich_stage4_daily_budget_usd", ENRICH_STAGE4_DAILY_BUDGET))

    # Budget Guard
    allowed, safe_limit, budget_reason = await _budget_guard(
        "firecrawl", FIRECRAWL_COST_PER_LEAD, batch_limit, daily_cap
    )
    if not allowed:
        logger.warning(f"[{job_name}] Skipped — {budget_reason}")
        await _log_job(job_name, status="skipped", notes=budget_reason)
        return {"job": job_name, "status": "skipped", "reason": budget_reason}

    logger.info(f"[{job_name}] {budget_reason}")

    # Verify Firecrawl API key
    api_key = settings.FIRECRAWL_API_KEY
    if not api_key:
        notes = "SKIPPED — FIRECRAWL_API_KEY not set in environment"
        logger.warning(f"[{job_name}] {notes}")
        await _log_job(job_name, status="skipped", notes=notes)
        return {"job": job_name, "status": "skipped", "reason": notes}

    db = get_db()

    # Fetch eligible leads: Stage 3, pain >= min_pain, ordered by pain DESC
    leads = (
        db.table("leads")
        .select("id, business_name, website, outscraper_website, pain_score, niche_slug")
        .eq("enrichment_stage", 3)
        .eq("is_active", True)
        .gte("pain_score", min_pain)
        .order("pain_score", desc=True)
        .limit(safe_limit)
        .execute()
        .data or []
    )

    if not leads:
        notes = f"no_eligible_leads — stage=3, pain>={min_pain}"
        await _log_job(job_name, status="success", notes=notes)
        return {"job": job_name, "status": "success", "leads_found": 0, "reason": notes}

    succeeded = 0
    failed = 0
    total_cost = 0.0

    EXTRACT_PROMPT = (
        "Extract: business owner name, services offered, service areas, "
        "years in business, any pricing mentioned, unique selling points"
    )

    for lead in leads:
        lead_id = lead["id"]
        website = (lead.get("website") or lead.get("outscraper_website") or "").strip()

        if not website:
            # No website — advance stage, no cost
            try:
                db.table("leads").update({
                    "enrichment_stage": 4,
                    "enriched_at": _now_iso(),
                }).eq("id", lead_id).execute()
            except Exception:
                pass
            continue

        url = website if website.startswith(("http://", "https://")) else f"https://{website}"

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    "https://api.firecrawl.dev/v1/scrape",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "url": url,
                        "formats": ["markdown", "extract"],
                        "extract": {"prompt": EXTRACT_PROMPT},
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            fc_data = data.get("data", {}) if data.get("success") else {}
            raw_md = fc_data.get("markdown") or ""
            extracted = fc_data.get("extract") or {}

            def _str(keys):
                for k in keys:
                    v = extracted.get(k)
                    if v and isinstance(v, str):
                        return v.strip()
                    if v and isinstance(v, list):
                        return ", ".join(str(i).strip() for i in v if i)
                return None

            def _csv(keys):
                for k in keys:
                    v = extracted.get(k)
                    if not v:
                        continue
                    if isinstance(v, list):
                        items = [str(i).strip() for i in v if i]
                        return ", ".join(items) if items else None
                    if isinstance(v, str):
                        return v.strip()
                return None

            update_payload = {
                "firecrawl_markdown":          raw_md[:5000] if raw_md else None,
                "firecrawl_owner_name":        _str(["business_owner_name", "owner_name", "owner"]),
                "firecrawl_services":          _csv(["services_offered", "services", "service_list"]),
                "firecrawl_service_areas":     _csv(["service_areas", "areas_served", "service_area"]),
                "firecrawl_years_in_business": _str(["years_in_business", "years_operating", "founded"]),
                "firecrawl_usp":               _str(["unique_selling_points", "usp", "value_proposition"]),
                "enrichment_stage": 4,
                "enriched_at": _now_iso(),
            }

            db.table("leads").update(update_payload).eq("id", lead_id).execute()

            # Log cost
            db.table("api_cost_log").insert({
                "lead_id": lead_id,
                "service": "firecrawl",
                "cost_per_unit": FIRECRAWL_COST_PER_LEAD,
                "records_processed": 1,
                "source_system": "kjle",
                "created_at": _now_iso(),
            }).execute()

            total_cost += FIRECRAWL_COST_PER_LEAD
            succeeded += 1

        except Exception as e:
            failed += 1
            logger.error(f"[{job_name}] Failed for lead {lead_id}: {e}")
            try:
                db.table("leads").update({
                    "enrichment_stage": 4,
                    "enriched_at": _now_iso(),
                }).eq("id", lead_id).execute()
            except Exception:
                pass

    duration = time.monotonic() - t_start
    notes = (
        f"succeeded={succeeded}, failed={failed}, "
        f"total_cost=${round(total_cost, 4):.4f}, "
        f"pain_gate={min_pain}, budget_cap=${daily_cap:.2f}"
    )
    status = "partial" if failed > 0 and succeeded > 0 else ("failed" if succeeded == 0 and failed > 0 else "success")

    await _log_job(job_name, leads_processed=succeeded, duration_seconds=duration, status=status, notes=notes)

    result = {
        "job": job_name,
        "leads_processed": len(leads),
        "succeeded": succeeded,
        "failed": failed,
        "total_cost_usd": round(total_cost, 4),
        "duration_seconds": round(duration, 3),
        "status": status,
    }
    logger.info(f"[{job_name}] Done. {result}")
    return result

# ─────────────────────────────────────────────────────────────────────────────
# JOB DISPATCH MAP — maps name → async callable
# ─────────────────────────────────────────────────────────────────────────────

JOB_FUNCTIONS = {
    "classify_segments":    job_classify_segments,
    "enrich_stage1":        job_enrich_stage1,
    "cost_digest":          job_cost_digest,
    "stale_cleanup":        job_stale_cleanup,
    "email_clean_nightly":  job_email_clean_nightly,
    "enrich_stage3_nightly": job_enrich_stage3_nightly,
    "enrich_stage4_nightly": job_enrich_stage4_nightly,
}


# ─────────────────────────────────────────────────────────────────────────────
# setup_scheduler — called from main.py lifespan
# ─────────────────────────────────────────────────────────────────────────────

def setup_scheduler() -> AsyncIOScheduler:
    """
    Creates and configures the APScheduler AsyncIOScheduler.
    Registers all 5 jobs. Returns the scheduler (caller must call .start() and .shutdown()).
    """
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Job 1: classify_segments — every 6 hours
    scheduler.add_job(
        job_classify_segments,
        trigger=IntervalTrigger(hours=6),
        id="classify_segments",
        name="Auto-Classify Segments",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Job 2: enrich_stage1 — every 12 hours
    scheduler.add_job(
        job_enrich_stage1,
        trigger=IntervalTrigger(hours=12),
        id="enrich_stage1",
        name="Auto-Enrich Stage 1",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Job 3: cost_digest — daily at 08:00 UTC
    scheduler.add_job(
        job_cost_digest,
        trigger=CronTrigger(hour=8, minute=0),
        id="cost_digest",
        name="Daily Cost Digest",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # Job 4: stale_cleanup — daily at 02:00 UTC
    scheduler.add_job(
        job_stale_cleanup,
        trigger=CronTrigger(hour=2, minute=0),
        id="stale_cleanup",
        name="Stale Lead Cleanup",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # Job 5: email_clean_nightly — daily at 00:00 UTC (midnight)
    # Processes up to 16,800 uncleaned leads at 1.5s/email over 7-hour window
    # Prioritizes HOT leads first, skips already-cleaned leads
    scheduler.add_job(
        job_email_clean_nightly,
        trigger=CronTrigger(hour=0, minute=0),
        id="email_clean_nightly",
        name="Nightly Email Clean (Truelist)",
        replace_existing=True,
        misfire_grace_time=3600,  # 1-hour grace window
    )

    # Job 6: enrich_stage3_nightly — daily at 01:00 UTC
    # Outscraper Google Maps enrichment — pain-gated, budget-guarded
    scheduler.add_job(
        job_enrich_stage3_nightly,
        trigger=CronTrigger(hour=1, minute=0),
        id="enrich_stage3_nightly",
        name="Nightly Stage 3 Enrichment (Outscraper)",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # Job 7: enrich_stage4_nightly — daily at 03:00 UTC
    # Firecrawl deep extraction — pain-gated, budget-guarded
    # Runs 2hrs after Stage 3 to allow Stage 3 leads to be classified first
    scheduler.add_job(
        job_enrich_stage4_nightly,
        trigger=CronTrigger(hour=3, minute=0),
        id="enrich_stage4_nightly",
        name="Nightly Stage 4 Enrichment (Firecrawl)",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    logger.info("⏰ APScheduler configured: 7 jobs registered")
    return scheduler


# ─────────────────────────────────────────────────────────────────────────────
# GET /scheduler/status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/scheduler/status")
async def get_scheduler_status():
    """
    Returns all 5 job definitions with last_ran, next_run, and last_status
    pulled from the scheduler_log table.
    """
    db = get_db()

    jobs_out = []
    for job_name, definition in JOB_DEFINITIONS.items():
        recent = (
            db.table(SCHEDULER_LOG_TABLE)
            .select("ran_at, status, duration_seconds, notes")
            .eq("job_name", job_name)
            .order("ran_at", desc=True)
            .limit(1)
            .execute()
            .data or []
        )
        last = recent[0] if recent else {}

        jobs_out.append({
            "job_name":              job_name,
            "description":           definition["description"],
            "schedule":              definition["schedule"],
            "last_ran":              last.get("ran_at"),
            "last_status":           last.get("status"),
            "last_duration_seconds": last.get("duration_seconds"),
            "last_notes":            last.get("notes"),
        })

    return {
        "status": "success",
        "jobs":   jobs_out,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /scheduler/run/{job_name}
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/scheduler/run/{job_name}")
async def run_job_manually(job_name: str):
    """
    Manually trigger a specific scheduled job by name.
    Valid names: classify_segments, enrich_stage1, cost_digest, stale_cleanup, email_clean_nightly, enrich_stage3_nightly, enrich_stage4_nightly.
    Runs synchronously and returns the job result immediately.
    """
    if job_name not in JOB_FUNCTIONS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Job '{job_name}' not found. "
                f"Valid jobs: {sorted(JOB_FUNCTIONS.keys())}"
            ),
        )

    logger.info(f"Manual trigger: {job_name}")
    result = await JOB_FUNCTIONS[job_name]()

    return {
        "status":       "success",
        "triggered_by": "manual",
        "result":       result,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /scheduler/log
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/scheduler/log")
async def get_scheduler_log(
    job_name: Optional[str] = Query(None, description="Filter by job name"),
    limit: int = Query(20, ge=1, le=200),
):
    """
    Returns recent scheduler_log entries sorted by ran_at desc.
    """
    db = get_db()

    query = (
        db.table(SCHEDULER_LOG_TABLE)
        .select("*")
        .order("ran_at", desc=True)
        .limit(limit)
    )

    if job_name:
        if job_name not in JOB_FUNCTIONS:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown job_name '{job_name}'. Valid: {sorted(JOB_FUNCTIONS.keys())}",
            )
        query = query.eq("job_name", job_name)

    rows = query.execute().data or []

    return {
        "status": "success",
        "filters": {"job_name": job_name, "limit": limit},
        "count":  len(rows),
        "log":    rows,
    }
