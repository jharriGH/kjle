"""
Scheduled Automation — APScheduler Cron Jobs
KJLE - King James Lead Empire
Routes: /kjle/v1/scheduler/*

4 background jobs registered at startup via setup_scheduler():
  1. classify_segments  — every 6 hours
  2. enrich_stage1      — every 12 hours
  3. cost_digest        — daily at 08:00 UTC
  4. stale_cleanup      — daily at 02:00 UTC

Call setup_scheduler() inside FastAPI lifespan (main.py).
"""

import logging
import time
from datetime import datetime, timezone, timedelta, date
from typing import Optional

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

SCHEDULER_LOG_TABLE   = "scheduler_log"
CLASSIFY_LIMIT        = 10_000
ENRICH_STAGE1_LIMIT   = 50
ENRICH_MIN_PAIN       = 50
STALE_DAYS            = 180
STALE_MAX_PAIN        = 30

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
                "lead_id":          lead_id,
                "service":          "stage1_scrape",
                "cost_per_unit":    0.0,
                "records_processed": 1,
                "source_system":    "kjle",
                "created_at":       _now_iso(),
            }).execute()

            succeeded += 1

        except Exception as e:
            failed += 1
            logger.error(f"[{job_name}] Enrichment failed for lead {lead_id}: {e}")
            # Still advance stage to avoid infinite retry
            try:
                db.table("leads").update({
                    "website_reachable": False,
                    "has_schema_markup": False,
                    "schema_types":      None,
                    "has_phone_on_page": False,
                    "has_address_on_page": False,
                    "enrichment_stage":  1,
                    "enriched_at":       _now_iso(),
                }).eq("id", lead_id).execute()
            except Exception:
                pass

    duration = time.monotonic() - t_start
    notes = f"succeeded={succeeded}, failed={failed}"
    status = "partial" if failed > 0 and succeeded > 0 else ("failed" if failed > 0 and succeeded == 0 else "success")

    await _log_job(
        job_name,
        leads_processed=len(leads),
        duration_seconds=duration,
        status=status,
        notes=notes,
    )

    result = {
        "job":              job_name,
        "leads_fetched":    len(leads),
        "succeeded":        succeeded,
        "failed":           failed,
        "duration_seconds": round(duration, 3),
        "status":           status,
    }
    logger.info(f"[{job_name}] Done. {result}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Job 3 — Daily Cost Digest (08:00 UTC)
# ─────────────────────────────────────────────────────────────────────────────

async def job_cost_digest() -> dict:
    """
    Fetches today's spend from api_cost_log, aggregates by service,
    checks active budget guardrails, fires webhook with digest payload.
    """
    job_name = "cost_digest"
    logger.info(f"[{job_name}] Starting...")
    t_start = time.monotonic()

    db = get_db()
    today = _today_iso()

    # Today's cost rows
    today_rows = (
        db.table("api_cost_log")
        .select("service, cost_per_unit, records_processed")
        .gte("created_at", today)
        .execute()
        .data or []
    )

    # Aggregate by service
    service_map: dict = {}
    for row in today_rows:
        svc = row.get("service") or "unknown"
        cost = _safe_float(row.get("cost_per_unit", 0)) * _safe_int(row.get("records_processed", 1), 1)
        service_map[svc] = round(service_map.get(svc, 0.0) + cost, 6)

    total_today = round(sum(service_map.values()), 6)

    # Check guardrails
    guardrails = (
        db.table("budget_guardrails")
        .select("name, period, limit_amount, action")
        .eq("active", True)
        .eq("period", "daily")
        .execute()
        .data or []
    )

    breached_guardrails = []
    for g in guardrails:
        if total_today >= _safe_float(g.get("limit_amount", 0)):
            breached_guardrails.append({
                "name":         g.get("name"),
                "limit_usd":    g.get("limit_amount"),
                "current_usd":  total_today,
                "action":       g.get("action"),
            })

    digest_payload = {
        "date":               today,
        "total_spend_usd":    total_today,
        "by_service":         service_map,
        "guardrails_breached": breached_guardrails,
        "guardrail_count":    len(breached_guardrails),
    }

    # Fire webhook (reuse existing infrastructure)
    try:
        await fire_event("segment.classified", digest_payload)
    except Exception as e:
        logger.warning(f"[{job_name}] Webhook fire failed: {e}")

    duration = time.monotonic() - t_start
    notes = (
        f"total_usd={total_today}, services={len(service_map)}, "
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
        "total_spend_usd":   total_today,
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

    # Fetch IDs of stale leads (fetch then update — Supabase client has no bulk conditional update)
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
# JOB DISPATCH MAP — maps name → async callable
# ─────────────────────────────────────────────────────────────────────────────

JOB_FUNCTIONS = {
    "classify_segments": job_classify_segments,
    "enrich_stage1":     job_enrich_stage1,
    "cost_digest":       job_cost_digest,
    "stale_cleanup":     job_stale_cleanup,
}


# ─────────────────────────────────────────────────────────────────────────────
# setup_scheduler — called from main.py lifespan
# ─────────────────────────────────────────────────────────────────────────────

def setup_scheduler() -> AsyncIOScheduler:
    """
    Creates and configures the APScheduler AsyncIOScheduler.
    Register all 4 jobs. Returns the scheduler (caller must call .start() and .shutdown()).
    """
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Job 1: classify_segments — every 6 hours
    scheduler.add_job(
        job_classify_segments,
        trigger=IntervalTrigger(hours=6),
        id="classify_segments",
        name="Auto-Classify Segments",
        replace_existing=True,
        misfire_grace_time=300,    # 5-min grace if Render spins up late
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
        misfire_grace_time=1800,   # 30-min grace for daily cron
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

    logger.info("⏰ APScheduler configured: 4 jobs registered")
    return scheduler


# ─────────────────────────────────────────────────────────────────────────────
# GET /scheduler/status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/scheduler/status")
async def get_scheduler_status():
    """
    Returns all 4 job definitions with last_ran, next_run, and last_status
    pulled from the scheduler_log table.
    """
    db = get_db()

    jobs_out = []
    for job_name, definition in JOB_DEFINITIONS.items():
        # Latest log entry for this job
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
            "job_name":     job_name,
            "description":  definition["description"],
            "schedule":     definition["schedule"],
            "last_ran":     last.get("ran_at"),
            "last_status":  last.get("status"),
            "last_duration_seconds": last.get("duration_seconds"),
            "last_notes":   last.get("notes"),
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
    Valid names: classify_segments, enrich_stage1, cost_digest, stale_cleanup.
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
