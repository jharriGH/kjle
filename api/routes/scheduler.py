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
import csv
import io
import logging
import os
import time
import zipfile
from datetime import datetime, timezone, timedelta, date
from typing import Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import APIRouter, HTTPException, Query

from ..database import get_db
from ..lib import cost_guard
from .segments_engine import _classify_lead
from .enrichment import _fetch_and_parse
from .webhooks import fire_event
from .enrichment_email_clean import (
    select_uncleaned_leads as ec_select_uncleaned_leads,
    submit_batch as ec_submit_batch,
    poll_batch as ec_poll_batch,
    ingest_batch_result as ec_ingest_batch_result,
    _get_truelist_api_key as ec_get_truelist_api_key,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SCHEDULER_LOG_TABLE  = "scheduler_log"
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

# ── Campaign sync targets (config-driven) ──
# Each entry describes one (project, server) pair to sync hourly from ReachInbox.
# Adding a new project or server = append one dict. No logic changes required.
CAMPAIGN_SYNC_TARGETS = [
    {
        "project":           "kjle",
        "reachinbox_server": "server_1",
        "api_base":          "https://api.reachinbox.ai/api/v1",
        "api_key_attr":      "REACHINBOX_API_KEY",   # settings attribute name
        "statuses_to_sync":  ["active", "paused"],
    },
]


JOB_DEFINITIONS = {
    "classify_segments": {
        "description": "Auto-classify all active leads into hot/warm/cold segments",
        "schedule":    "Every 6 hours",
        "trigger":     "interval",
    },
    "enrich_stage1": {
        "description": "Auto-enrich unenriched Stage 0 leads via free website scrape — batch size tunable via admin_settings.enrich_batch_size (default 1000)",
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
        "description": "Nightly Truelist bulk-batch submitter — submits N x 25K batches via POST /api/v1/batches (midnight UTC)",
        "schedule":    "Daily at 00:00 UTC",
        "trigger":     "cron",
    },
    "email_clean_poll_batches": {
        "description": "Polls in-flight Truelist batches every 30 min, ingests annotated CSV when completed",
        "schedule":    "Every 30 minutes",
        "trigger":     "interval",
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
    "daily_cost_report": {
        "description": "Daily cost + lead-pipeline digest emailed via Resend (rolling 24h)",
        "schedule":    "Daily at 16:00 UTC (09:00 PDT / 08:00 PST)",
        "trigger":     "cron",
    },
    "campaign_sync_hourly": {
        "description": "Hourly campaign stats sync from ReachInbox — project/server-scoped via CAMPAIGN_SYNC_TARGETS",
        "schedule":    "Every 60 minutes",
        "trigger":     "interval",
    },
    "fed_dnc_refresh_monthly": {
        "description": "Refresh fed_dnc_list table from /opt/fed_dnc_latest.zip — warns and skips if file missing",
        "schedule":    "1st of every month at 04:00 UTC",
        "trigger":     "cron",
    },
    "nanpa_refresh_monthly": {
        "description": "Refresh nanpa_blocked_prefixes table from /opt/nanpa_latest.csv — warns and skips if file missing",
        "schedule":    "1st of every month at 04:30 UTC",
        "trigger":     "cron",
    },
}


# ── Federal DNC + NANPA refresh paths (Phase 4 Layer 1) ──────────────────────
# Both source files live on the Render disk and are refreshed out-of-band
# (FCC subscriber portal for DNC, NANPA monthly download for prefixes).
# Until Jim's FCC registration is approved, fed_dnc_latest.zip will not
# exist — the job logs a warning and exits status=skipped (NOT failed).
FED_DNC_FILE = os.environ.get("FED_DNC_FILE_PATH", "/opt/fed_dnc_latest.zip")
NANPA_FILE   = os.environ.get("NANPA_FILE_PATH",   "/opt/nanpa_latest.csv")
FED_DNC_UPSERT_CHUNK = 5_000
NANPA_UPSERT_CHUNK   = 2_000


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


def _map_ri_status(ri_status: Optional[str]) -> Optional[str]:
    """Map ReachInbox status strings to our lowercase canonical values."""
    if not ri_status:
        return None
    mapping = {
        "draft":     "draft",
        "active":    "active",
        "running":   "active",
        "paused":    "paused",
        "stopped":   "paused",
        "completed": "completed",
        "archived":  "archived",
    }
    return mapping.get(ri_status.strip().lower())


# ─────────────────────────────────────────────────────────────────────────────
# Job 1 — Auto-Classify Segments (every 6 hours)
# ─────────────────────────────────────────────────────────────────────────────

async def job_classify_segments() -> dict:
    """
    Chunked-UPDATE classifier (v3 — rewritten 2026-05-09).

    For each target segment we run a SELECT→UPDATE-by-id loop until convergence.
    Each chunk SELECTs up to CHUNK_SIZE ids that match the pain filter AND do
    NOT already carry the target label, then issues an UPDATE-by-id-list. This
    avoids the Postgres statement_timeout that killed an earlier "single bulk
    UPDATE per segment" attempt on the ~400K COLD row set: by-id UPDATEs hit
    the primary-key index and complete in well under the per-statement budget.

    The negative-label filter (`neq("segment_label", target)`) is what makes the
    loop converge — once a row is updated it falls out of subsequent SELECTs.

    Threshold logic MIRRORS api/routes/segments_engine.py::_classify_lead:
      HOT:  pain_score >= 30
      WARM: 15 <= pain_score < 30
      COLD: pain_score < 15
    Leads with NULL pain_score are skipped (no filter matches them) and their
    segment_label stays unchanged. Cross-file dependency: thresholds are
    duplicated here. If you change thresholds in segments_engine.py, update
    these chunks too. See the landmine breadcrumb in segments_engine.py.
    """
    job_name = "classify_segments"
    CHUNK_SIZE = 1000
    logger.info(f"[{job_name}] Starting (chunked-UPDATE mode, chunk={CHUNK_SIZE})...")
    t_start = time.monotonic()

    db = get_db()
    now_iso = _now_iso()
    counts = {"hot": 0, "warm": 0, "cold": 0}

    def _apply_pain_filter(query, label: str):
        if label == "hot":
            return query.gte("pain_score", 30)
        if label == "warm":
            return query.gte("pain_score", 15).lt("pain_score", 30)
        return query.lt("pain_score", 15)  # cold

    async def _classify_one(label: str) -> int:
        """
        Cursor-paginate then range-UPDATE per chunk. We avoid IN(ids) on the
        UPDATE because PostgREST/gateway URL length caps below 1000 UUIDs
        (~37KB serialized) — a probe at /scheduler/diag/classify-select
        confirmed UPDATE-by-IN-1000 returns "JSON could not be generated"
        while UPDATE-by-id-range with the same pain filter succeeds.

        Each chunk: SELECT 1000 ids in order, take first/last as bounds, then
        UPDATE WHERE pain-filter AND id BETWEEN first AND last. Since the
        pain filter is the same on SELECT and UPDATE, the row set is
        identical — total += len(rows) is exact.
        Cursor moves forward via gt(id, prev_last) so chunks don't overlap.
        """
        total = 0
        chunk_idx = 0
        last_id_cursor: Optional[str] = None
        while True:
            sel = db.table("leads").select("id").eq("is_active", True)
            sel = _apply_pain_filter(sel, label)
            if last_id_cursor is not None:
                sel = sel.gt("id", last_id_cursor)
            sel = sel.order("id", desc=False).limit(CHUNK_SIZE)
            try:
                rows = sel.execute().data or []
            except Exception as e:
                logger.error(f"[{job_name}] {label.upper()} SELECT chunk {chunk_idx} failed: {e}")
                raise
            if not rows:
                break
            first_id = rows[0]["id"]
            last_id  = rows[-1]["id"]
            try:
                upd = (
                    db.table("leads")
                    .update({"segment_label": label, "segmented_at": now_iso})
                    .eq("is_active", True)
                )
                upd = _apply_pain_filter(upd, label)
                upd = upd.gte("id", first_id).lte("id", last_id)
                upd.execute()
            except Exception as e:
                logger.error(f"[{job_name}] {label.upper()} UPDATE chunk {chunk_idx} failed: {e}")
                raise
            total += len(rows)
            last_id_cursor = last_id
            chunk_idx += 1
            if chunk_idx % 25 == 0:
                logger.info(f"[{job_name}] {label.upper()} progress: {total:,} rows ({chunk_idx} chunks)")
            await asyncio.sleep(0)  # cooperative yield
        logger.info(f"[{job_name}] {label.upper()} done: {total:,} rows in {chunk_idx} chunks")
        return total

    try:
        counts["hot"]  = await _classify_one("hot")
        counts["warm"] = await _classify_one("warm")
        counts["cold"] = await _classify_one("cold")
    except Exception as e:
        duration = time.monotonic() - t_start
        logger.error(f"[{job_name}] Chunked UPDATE failed: {e}")
        await _log_job(
            job_name,
            leads_processed=sum(counts.values()),
            hot=counts["hot"],
            warm=counts["warm"],
            cold=counts["cold"],
            duration_seconds=duration,
            status="failed",
            notes=f"chunked_update_error: {str(e)[:300]}",
        )
        return {
            "job": job_name,
            "status": "failed",
            "error": str(e),
            "partial_counts": counts,
            "duration_seconds": round(duration, 3),
        }

    duration = time.monotonic() - t_start
    total = sum(counts.values())
    notes = f"hot={counts['hot']}, warm={counts['warm']}, cold={counts['cold']}, total={total}, duration={duration:.1f}s"

    await _log_job(
        job_name,
        leads_processed=total,
        hot=counts["hot"],
        warm=counts["warm"],
        cold=counts["cold"],
        duration_seconds=duration,
        status="success",
        notes=notes,
    )

    result = {
        "job": job_name,
        "leads_processed": total,
        "hot":   counts["hot"],
        "warm":  counts["warm"],
        "cold":  counts["cold"],
        "duration_seconds": round(duration, 3),
        "status": "success",
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

    stage1_limit = int(await _get_admin_setting("enrich_batch_size", 1000))

    leads = (
        db.table("leads")
        .select("id, website, business_name, pain_score")
        .eq("is_active", True)
        .eq("enrichment_stage", 0)
        .gte("pain_score", ENRICH_MIN_PAIN)
        .not_.is_("website", "null")
        .neq("website", "")
        .order("pain_score", desc=True)
        .limit(stage1_limit)
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
    V2 nightly Truelist BATCH SUBMITTER (replaces v1 verify_inline loop).

    Submits up to N batches/night to Truelist /api/v1/batches, where:
      N         = admin_settings.email_clean_max_batches_per_night (default 4)
      batchsize = admin_settings.email_clean_batch_size_emails    (default 25000)

    Priority: HOT → WARM → COLD → unclassified, pain_score DESC within each.

    Result CSV ingestion happens via the separate poller cron
    (job_email_clean_poll_batches) or via the /webhooks/truelist/batch-complete
    receiver when Truelist's dashboard webhook is configured. Both paths
    converge on enrichment_email_clean.ingest_batch_result.
    """
    job_name = "email_clean_nightly"
    t_start = time.monotonic()
    db = get_db()

    # Enabled gate
    try:
        enabled_res = db.table("admin_settings").select("value").eq("key", "email_clean_enabled").execute()
        enabled = (enabled_res.data or [{}])[0].get("value", "false").lower() in ("true", "1", "yes")
    except Exception:
        enabled = False
    if not enabled:
        notes = "SKIPPED — email_clean_enabled is false"
        logger.warning(f"[{job_name}] {notes}")
        await _log_job(job_name, status="skipped", notes=notes)
        return {"job": job_name, "status": "skipped", "reason": notes}

    # API key (raises HTTPException if missing — we convert to clean skip)
    try:
        api_key = ec_get_truelist_api_key(db)
    except Exception as e:
        notes = f"SKIPPED — truelist_api_key not configured ({e})"
        logger.warning(f"[{job_name}] {notes}")
        await _log_job(job_name, status="skipped", notes=notes)
        return {"job": job_name, "status": "skipped", "reason": notes}

    # Load v2 batch sizing from admin_settings (with safe defaults)
    def _get_int(key: str, default: int) -> int:
        try:
            r = db.table("admin_settings").select("value").eq("key", key).execute()
            return int((r.data or [{}])[0].get("value") or default)
        except Exception:
            return default

    max_batches = max(1, min(_get_int("email_clean_max_batches_per_night", 4), 50))
    batch_size  = max(1, min(_get_int("email_clean_batch_size_emails", 25_000), 250_000))

    logger.info(f"[{job_name}] V2 starting — max_batches={max_batches}, batch_size={batch_size}")

    submitted_batches: list[dict] = []
    for i in range(max_batches):
        leads = ec_select_uncleaned_leads(db, batch_size)
        if not leads:
            logger.info(f"[{job_name}] No more uncleaned leads after {i} batches — stopping early")
            break
        try:
            r = await ec_submit_batch(
                db, api_key, leads,
                submitted_by="nightly_cron",
                notes=f"nightly_{i+1}_of_{max_batches}",
            )
            r["index"] = i
            submitted_batches.append(r)
            logger.info(
                f"[{job_name}] Batch {i+1}/{max_batches}: submitted={r.get('submitted')}, "
                f"batch_id={r.get('batch_id')}"
            )
        except Exception as e:
            logger.error(f"[{job_name}] Batch {i+1} submit FAILED: {e}")
            submitted_batches.append({"index": i, "submitted": 0, "error": str(e)})
            break  # don't hammer Truelist if first one failed

    duration = time.monotonic() - t_start
    total_emails = sum((b.get("submitted") or 0) for b in submitted_batches)
    notes = (
        f"batches_submitted={len([b for b in submitted_batches if b.get('submitted', 0) > 0])}, "
        f"total_emails={total_emails}, "
        f"batch_ids={[b.get('batch_id') for b in submitted_batches if b.get('batch_id')]}"
    )
    status = "success" if any(b.get("submitted", 0) > 0 for b in submitted_batches) else (
        "skipped" if not submitted_batches else "failed"
    )
    await _log_job(job_name, leads_processed=total_emails, duration_seconds=duration,
                   status=status, notes=notes)

    result = {
        "job":         job_name,
        "batches":     submitted_batches,
        "total_emails":total_emails,
        "duration_s":  round(duration, 2),
        "status":      status,
    }
    logger.info(f"[{job_name}] Done. {result}")
    return result


async def job_email_clean_poll_batches() -> dict:
    """
    Poll all in-flight truelist_batches rows (status in pending/processing)
    every 30 min. When a batch transitions to `completed`, ingest its
    annotated CSV into the leads table.

    Safe to run concurrently with the submitter — ingest_batch_result is
    idempotent (already-ingested batches return a cached summary).
    """
    job_name = "email_clean_poll_batches"
    t_start = time.monotonic()
    db = get_db()

    try:
        api_key = ec_get_truelist_api_key(db)
    except Exception as e:
        notes = f"SKIPPED — truelist_api_key not configured ({e})"
        await _log_job(job_name, status="skipped", notes=notes)
        return {"job": job_name, "status": "skipped", "reason": notes}

    try:
        rows = (
            db.table("truelist_batches")
            .select("id, status, submitted_at, email_count")
            .in_("status", ["pending", "processing"])
            .order("submitted_at", desc=False)
            .execute()
            .data or []
        )
    except Exception as e:
        notes = f"in_flight_query_failed: {e}"
        await _log_job(job_name, status="failed", notes=notes)
        return {"job": job_name, "status": "failed", "reason": notes}

    if not rows:
        await _log_job(job_name, leads_processed=0, status="success",
                       notes="no in-flight batches")
        return {"job": job_name, "status": "success", "in_flight": 0}

    ingested = 0
    still_running = 0
    ingest_summaries: list[dict] = []
    errors: list[dict] = []

    for r in rows:
        batch_id = r["id"]
        try:
            raw = await ec_poll_batch(api_key, batch_id)
            real_state = raw.get("batch_state")
            db.table("truelist_batches").update({
                "status":                    real_state,
                "annotated_csv_url":         raw.get("annotated_csv_url"),
                "safest_bet_csv_url":        raw.get("safest_bet_csv_url"),
                "highest_reach_csv_url":     raw.get("highest_reach_csv_url"),
                "only_invalid_csv_url":      raw.get("only_invalid_csv_url"),
                "ok_count":                  raw.get("ok_count"),
                "ok_for_all_count":          raw.get("ok_for_all_count"),
                "role_count":                raw.get("role_count"),
                "disposable_count":          raw.get("disposable_count"),
                "failed_syntax_check_count": raw.get("failed_syntax_check_count"),
                "failed_mx_check_count":     raw.get("failed_mx_check_count"),
                "failed_no_mailbox_count":   raw.get("failed_no_mailbox_count"),
                "completed_at":              _now_iso() if real_state == "completed" else None,
            }).eq("id", batch_id).execute()

            if real_state == "completed":
                summary = await ec_ingest_batch_result(db, api_key, batch_id)
                ingest_summaries.append(summary)
                ingested += 1
            else:
                still_running += 1
        except Exception as e:
            logger.error(f"[{job_name}] {batch_id} poll/ingest error: {e}")
            errors.append({"batch_id": batch_id, "error": str(e)[:300]})

    duration = time.monotonic() - t_start
    notes = (
        f"in_flight={len(rows)}, ingested={ingested}, still_running={still_running}, "
        f"errors={len(errors)}"
    )
    status = "success" if not errors else ("partial" if ingested > 0 else "failed")
    await _log_job(job_name, leads_processed=sum(s.get("leads_processed", 0) for s in ingest_summaries),
                   duration_seconds=duration, status=status, notes=notes)

    return {
        "job":           job_name,
        "in_flight":     len(rows),
        "ingested":      ingested,
        "still_running": still_running,
        "errors":        errors,
        "summaries":     ingest_summaries,
        "duration_s":    round(duration, 2),
        "status":        status,
    }




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

    # ── Kill-switch gate (admin_settings.stage3_nightly_enabled) ─────────────
    enabled_raw = await _get_admin_setting("stage3_nightly_enabled", "true")
    if str(enabled_raw).strip().lower() not in ("true", "1", "yes", "on"):
        notes = "paused via admin_settings, skipped"
        logger.info(f"[{job_name}] {notes}")
        await _log_job(job_name, status="skipped", notes=notes)
        return {"job": job_name, "status": "skipped", "reason": notes}

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
            # ── Hard budget guard (per-lead, inside loop) ────────────────────
            # If caps trip mid-batch, abort the batch cleanly rather than
            # continuing to burn money.
            if not await cost_guard.check_budget(
                service="outscraper",
                estimated_cost_usd=50 * cost_guard.OUTSCRAPER_COST_PER_REVIEW,
                job_name=job_name,
                leads_affected=len(leads) - succeeded - failed,
            ):
                logger.warning(f"[{job_name}] Budget guard halted batch mid-run")
                break

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

            # Log cost — legacy table (kept for existing readers like _budget_guard)
            db.table("api_cost_log").insert({
                "lead_id": lead_id,
                "service": "outscraper",
                "cost_per_unit": OUTSCRAPER_COST_PER_LEAD,
                "records_processed": 1,
                "source_system": "kjle",
                "created_at": _now_iso(),
            }).execute()

            # New hard-cap ledger — actual review-based cost
            review_count = int(place["reviews"]) if place.get("reviews") is not None else 0
            await cost_guard.log_cost(
                stage="stage3",
                service="outscraper",
                cost_usd=cost_guard.outscraper_cost(review_count),
                lead_id=lead_id,
                items_fetched=review_count,
                job_run_id=job_name,
                metadata={"entry_point": "scheduler_nightly"},
            )

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

    # ── Kill-switch gate (admin_settings.stage4_nightly_enabled) ─────────────
    enabled_raw = await _get_admin_setting("stage4_nightly_enabled", "true")
    if str(enabled_raw).strip().lower() not in ("true", "1", "yes", "on"):
        notes = "paused via admin_settings, skipped"
        logger.info(f"[{job_name}] {notes}")
        await _log_job(job_name, status="skipped", notes=notes)
        return {"job": job_name, "status": "skipped", "reason": notes}

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
            # ── Hard budget guard (per-lead) ─────────────────────────────────
            if not await cost_guard.check_budget(
                service="firecrawl",
                estimated_cost_usd=cost_guard.FIRECRAWL_COST_PER_SCRAPE,
                job_name=job_name,
                leads_affected=len(leads) - succeeded - failed,
            ):
                logger.warning(f"[{job_name}] Budget guard halted batch mid-run")
                break

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

            # Log cost — legacy table (kept for existing readers)
            db.table("api_cost_log").insert({
                "lead_id": lead_id,
                "service": "firecrawl",
                "cost_per_unit": FIRECRAWL_COST_PER_LEAD,
                "records_processed": 1,
                "source_system": "kjle",
                "created_at": _now_iso(),
            }).execute()

            # New hard-cap ledger
            await cost_guard.log_cost(
                stage="stage4",
                service="firecrawl",
                cost_usd=cost_guard.FIRECRAWL_COST_PER_SCRAPE,
                lead_id=lead_id,
                items_fetched=1,
                job_run_id=job_name,
                metadata={"entry_point": "scheduler_nightly"},
            )

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
# Job 8 — Daily Cost Report email (16:00 UTC = 09:00 PDT / 08:00 PST)
# Rolling 24h window. Honors admin_settings.daily_cost_report_enabled.
# ─────────────────────────────────────────────────────────────────────────────

async def job_daily_cost_report() -> dict:
    """
    Build + send the daily KJLE cost report email to
    admin_settings.daily_cost_report_email (default sales@mobilewebmds.com)
    via Resend. Skips if daily_cost_report_enabled='false'.
    """
    job_name = "daily_cost_report"
    logger.info(f"[{job_name}] Starting...")
    t_start = time.monotonic()

    db = get_db()

    # Enabled flag
    try:
        res = db.table("admin_settings").select("value").eq("key", "daily_cost_report_enabled").execute()
        raw = (res.data[0].get("value") if res.data else "true") or "true"
        enabled = raw.strip().lower() in ("true", "1", "yes", "on")
    except Exception as e:
        logger.warning(f"[{job_name}] Could not read enabled flag: {e}")
        enabled = True

    if not enabled:
        notes = "SKIPPED — daily_cost_report_enabled=false"
        logger.info(f"[{job_name}] {notes}")
        await _log_job(job_name, status="skipped", notes=notes)
        return {"job": job_name, "status": "skipped", "reason": notes}

    # Recipient
    try:
        res = db.table("admin_settings").select("value").eq("key", "daily_cost_report_email").execute()
        recipient = (res.data[0].get("value") if res.data else "") or "sales@mobilewebmds.com"
    except Exception:
        recipient = "sales@mobilewebmds.com"
    recipient = recipient.strip() or "sales@mobilewebmds.com"

    # Sender (from_addr)
    try:
        res = db.table("admin_settings").select("value").eq("key", "daily_cost_report_sender").execute()
        sender = (res.data[0].get("value") if res.data else "") or "KJLE Reports <kjle@kjreportz.com>"
    except Exception:
        sender = "KJLE Reports <kjle@kjreportz.com>"
    sender = sender.strip() or "KJLE Reports <kjle@kjreportz.com>"

    # Build body
    from ..lib.daily_report import build_daily_report
    from ..lib.email_sender import send_email, is_configured

    try:
        subject, body = build_daily_report()
    except Exception as e:
        logger.error(f"[{job_name}] Report build failed: {e}")
        duration = time.monotonic() - t_start
        await _log_job(job_name, duration_seconds=duration, status="failed",
                       notes=f"build_error: {e}")
        return {"job": job_name, "status": "failed", "error": str(e)}

    if not is_configured():
        notes = "SKIPPED — RESEND_API_KEY not set (preview-only mode)"
        logger.warning(f"[{job_name}] {notes}; subject={subject!r}")
        duration = time.monotonic() - t_start
        await _log_job(job_name, duration_seconds=duration, status="skipped", notes=notes)
        return {
            "job": job_name, "status": "skipped", "reason": notes,
            "preview": {"to": recipient, "subject": subject, "body_len": len(body)},
        }

    result = await send_email(to=recipient, subject=subject, body_text=body, from_addr=sender)
    duration = time.monotonic() - t_start

    status = "success" if result.get("ok") else "failed"
    notes = f"to={recipient}, subject_len={len(subject)}, body_len={len(body)}, id={result.get('id')}"
    if not result.get("ok"):
        notes += f", error={result.get('error')}"

    # ── Belt-and-suspenders Searchbug balance alert ──────────────────────────
    # Catches days with zero fresh DNC lookups but a previously-recorded low
    # balance. Anti-spam handled inside the monitor (1 alert per threshold per
    # UTC day). Best-effort — never breaks the digest send.
    balance_alerts = 0
    try:
        from ..lib.searchbug_balance_monitor import check_and_alert_balance
        alert_result = await check_and_alert_balance()
        balance_alerts = int(alert_result.get("alerts_sent") or 0)
        if balance_alerts:
            notes += f", balance_alerts={balance_alerts}"
    except Exception as e:
        logger.error(f"[{job_name}] balance monitor check failed: {e}")

    await _log_job(job_name, duration_seconds=duration, status=status, notes=notes)

    return {
        "job":       job_name,
        "status":    status,
        "recipient": recipient,
        "subject":   subject,
        "email_id":  result.get("id"),
        "error":     result.get("error"),
        "balance_alerts_sent": balance_alerts,
        "duration_seconds": round(duration, 3),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Job 9 — Hourly Campaign Stats Sync (every 60 minutes)
# Pulls RI campaign stats for configured (project, server) pairs — see
# CAMPAIGN_SYNC_TARGETS at top of file. Adding a new project = 1 dict entry.
# ─────────────────────────────────────────────────────────────────────────────

async def job_campaign_sync_hourly() -> dict:
    """
    Hourly campaign_performance sync.

    For each target in CAMPAIGN_SYNC_TARGETS:
      - SELECT rows matching (project, reachinbox_server, status IN statuses_to_sync)
      - GET {api_base}/campaigns/{id}/status for each row
      - Update sent/opened/replied/bounced/unsubscribed + status + last_synced_at

    Config-driven; no code changes needed to add projects/servers.
    Non-blocking on per-campaign failure (logs and continues).
    """
    job_name = "campaign_sync_hourly"
    logger.info(f"[{job_name}] Starting...")
    t_start = time.monotonic()

    from ..config import settings

    db = get_db()
    total_synced = 0
    total_failed = 0
    per_target = []

    for tgt in CAMPAIGN_SYNC_TARGETS:
        project  = tgt["project"]
        server   = tgt["reachinbox_server"]
        api_base = tgt["api_base"]
        api_key  = getattr(settings, tgt["api_key_attr"], "") or ""
        statuses = tgt["statuses_to_sync"]

        if not api_key:
            reason = f"no API key for settings.{tgt['api_key_attr']}"
            logger.warning(f"[{job_name}] target {project}/{server} skipped — {reason}")
            per_target.append({
                "project": project, "server": server, "candidates": 0,
                "synced": 0, "failed": 0, "skipped": reason,
            })
            continue

        try:
            rows = (
                db.table("campaign_performance")
                .select("id, reachinbox_campaign_id")
                .eq("project", project)
                .eq("reachinbox_server", server)
                .in_("status", statuses)
                .not_.is_("reachinbox_campaign_id", "null")
                .execute().data or []
            )
        except Exception as e:
            logger.error(f"[{job_name}] {project}/{server} fetch failed: {e}")
            per_target.append({
                "project": project, "server": server, "candidates": 0,
                "synced": 0, "failed": 0, "error": str(e)[:200],
            })
            continue

        synced = 0
        failed = 0
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            for row in rows:
                cid = row["reachinbox_campaign_id"]
                try:
                    r = await client.get(f"{api_base}/campaigns/{cid}/status", headers=headers)
                    if r.status_code != 200:
                        failed += 1
                        logger.warning(f"[{job_name}] {project}/{server} campaign {cid} HTTP {r.status_code}")
                        continue
                    data = (r.json() or {}).get("data") or {}
                    update_payload = {
                        "sent_count":         _safe_int(data.get("totalEmailSent")),
                        "opened_count":       _safe_int(data.get("totalUniqueEmailOpened")),
                        "replied_count":      _safe_int(data.get("totalEmailReplied")),
                        "bounced_count":      _safe_int(data.get("totalBounces")),
                        "unsubscribed_count": _safe_int(data.get("totalUnsubscribes")),
                        "last_synced_at":     _now_iso(),
                    }
                    mapped = _map_ri_status(data.get("status"))
                    if mapped:
                        update_payload["status"] = mapped
                    db.table("campaign_performance").update(update_payload).eq("id", row["id"]).execute()
                    synced += 1
                except Exception as e:
                    failed += 1
                    logger.error(f"[{job_name}] {project}/{server} campaign {cid} error: {e}")

        total_synced += synced
        total_failed += failed
        per_target.append({
            "project": project, "server": server,
            "candidates": len(rows), "synced": synced, "failed": failed,
        })

    duration = time.monotonic() - t_start
    notes = f"synced={total_synced}, failed={total_failed}, targets={len(CAMPAIGN_SYNC_TARGETS)}"
    status = (
        "partial" if total_failed > 0 and total_synced > 0
        else ("failed" if total_failed > 0 and total_synced == 0 else "success")
    )

    await _log_job(
        job_name,
        leads_processed=total_synced,
        duration_seconds=duration,
        status=status,
        notes=notes,
    )

    result = {
        "job":              job_name,
        "synced":           total_synced,
        "failed":           total_failed,
        "per_target":       per_target,
        "duration_seconds": round(duration, 3),
        "status":           status,
    }
    logger.info(f"[{job_name}] Done. {result}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Job 10 — Federal DNC list refresh (monthly, 1st @ 04:00 UTC)
# Source: /opt/fed_dnc_latest.zip (FCC subscriber portal download).
# Until Jim's FCC registration (submitted 2026-05-24) is approved, the
# file will not exist — job logs a warning and exits status=skipped.
# ─────────────────────────────────────────────────────────────────────────────

async def job_fed_dnc_refresh_monthly() -> dict:
    """
    Rebuild fed_dnc_list from /opt/fed_dnc_latest.zip.

    The FCC distributes the National DNC Registry as a zipped text file —
    one phone per line, 10 digits (no formatting). We normalize each to
    E.164 (+1XXXXXXXXXX) and chunk-upsert into fed_dnc_list.

    Missing file is NOT an error — Jim's FCC registration is still
    pending approval as of 2026-05-24. Logs a warning and returns
    status=skipped so the scheduler doesn't fire alerts.
    """
    job_name = "fed_dnc_refresh_monthly"
    logger.info(f"[{job_name}] Starting...")
    t_start = time.monotonic()

    if not os.path.exists(FED_DNC_FILE):
        notes = (
            f"SKIPPED — {FED_DNC_FILE} not present. "
            "Awaiting FCC National DNC Registry subscription approval "
            "(submitted 2026-05-24). Drop the latest .zip at this path "
            "and the next run will populate fed_dnc_list."
        )
        logger.warning(f"[{job_name}] {notes}")
        await _log_job(job_name, status="skipped", notes=notes)
        return {"job": job_name, "status": "skipped", "reason": notes}

    refresh_run = _today_iso()
    db = get_db()

    inserted = 0
    invalid  = 0

    try:
        with zipfile.ZipFile(FED_DNC_FILE, "r") as zf:
            # FCC bundles a single .txt — pick the first one regardless of name.
            text_members = [n for n in zf.namelist() if not n.endswith("/")]
            if not text_members:
                notes = f"FAILED — {FED_DNC_FILE} contains no files"
                logger.error(f"[{job_name}] {notes}")
                await _log_job(job_name, status="failed", notes=notes)
                return {"job": job_name, "status": "failed", "reason": notes}

            buffer: list[dict] = []
            with zf.open(text_members[0]) as fh:
                for raw_line in io.TextIOWrapper(fh, encoding="utf-8", errors="ignore"):
                    digits = "".join(c for c in raw_line.strip() if c.isdigit())
                    if len(digits) == 11 and digits.startswith("1"):
                        digits = digits[1:]
                    if len(digits) != 10:
                        invalid += 1
                        continue
                    buffer.append({
                        "phone": f"+1{digits}",
                    })
                    if len(buffer) >= FED_DNC_UPSERT_CHUNK:
                        db.table("fed_dnc_list").upsert(buffer, on_conflict="phone").execute()
                        inserted += len(buffer)
                        buffer.clear()

            if buffer:
                db.table("fed_dnc_list").upsert(buffer, on_conflict="phone").execute()
                inserted += len(buffer)
                buffer.clear()

    except Exception as e:
        duration = time.monotonic() - t_start
        notes = f"FAILED — {type(e).__name__}: {str(e)[:300]}"
        logger.error(f"[{job_name}] {notes}")
        await _log_job(job_name, duration_seconds=duration, status="failed", notes=notes)
        return {"job": job_name, "status": "failed", "error": str(e)}

    duration = time.monotonic() - t_start
    notes = f"inserted={inserted}, invalid={invalid}, refresh_run={refresh_run}"
    await _log_job(job_name, leads_processed=inserted, duration_seconds=duration,
                   status="success", notes=notes)
    return {
        "job":              job_name,
        "inserted":         inserted,
        "invalid":          invalid,
        "refresh_run":      refresh_run,
        "duration_seconds": round(duration, 3),
        "status":           "success",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Job 11 — NANPA invalid-prefix refresh (monthly, 1st @ 04:30 UTC)
# Source: /opt/nanpa_latest.csv (NANPA monthly export).
# Same first-run posture as the FCC job — file may not exist; we warn
# and skip rather than fail.
# ─────────────────────────────────────────────────────────────────────────────

async def job_nanpa_refresh_monthly() -> dict:
    """
    Rebuild nanpa_carrier_prefixes from /opt/nanpa_latest.csv (NANPA
    Thousands-Block Assignment, All Regions, Augmented).

    Source: https://reports.nanpa.com/public/ThousandsBlockAssignment_All_Augmented.zip
    Schema target: nanpa_carrier_prefixes (Slice 1A migration). PK is
    npa_nxx_x (7 chars = NPA + NXX + thousands-block digit).

    NANPA does NOT publish an explicit mobile/landline/voip flag - we derive
    it from the carrier name via carrier_lookup.classify_line_type().

    Defensive CSV parsing - accepts common NANPA-export header variants:
        npa,nxx,thousands-block,ocn,company,...
        NPA,NXX,Block,OCN,Company,...
    Stray columns are ignored. Malformed rows count as `invalid` and skip.

    Missing file is NOT an error - same posture as job_fed_dnc_refresh_monthly.
    """
    from ..lib.carrier_lookup import classify_line_type

    job_name = "nanpa_refresh_monthly"
    logger.info(f"[{job_name}] Starting...")
    t_start = time.monotonic()

    if not os.path.exists(NANPA_FILE):
        notes = (
            f"SKIPPED - {NANPA_FILE} not present. Drop the latest NANPA "
            "Thousands-Block Assignment CSV at this path and the next run "
            "will populate nanpa_carrier_prefixes."
        )
        logger.warning(f"[{job_name}] {notes}")
        await _log_job(job_name, status="skipped", notes=notes)
        return {"job": job_name, "status": "skipped", "reason": notes}

    db = get_db()

    inserted = 0
    invalid  = 0
    UPSERT_CHUNK = 2_000

    def _get(row, *keys):
        """Case-insensitive lookup over a list of candidate keys."""
        lower = {(k or "").strip().lower(): v for k, v in row.items()}
        for k in keys:
            v = lower.get(k.lower())
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    try:
        with open(NANPA_FILE, "r", encoding="utf-8", errors="ignore", newline="") as fh:
            reader = csv.DictReader(fh)
            buffer: list[dict] = []
            for row in reader:
                npa_raw   = _get(row, "npa", "area_code")
                nxx_raw   = _get(row, "nxx", "exchange")
                block_raw = _get(row, "block", "thousands-block", "thousands_block", "x")
                if not (npa_raw and nxx_raw and block_raw):
                    invalid += 1
                    continue

                npa = "".join(c for c in npa_raw if c.isdigit())
                nxx = "".join(c for c in nxx_raw if c.isdigit())
                block = "".join(c for c in block_raw if c.isdigit())

                if len(npa) != 3 or len(nxx) != 3 or len(block) != 1:
                    invalid += 1
                    continue

                npa_nxx_x = f"{npa}{nxx}{block}"

                carrier = _get(row, "assigned to", "code holder", "company", "carrier", "company_name", "block_holder")
                ocn     = _get(row, "ocn", "operating_company_number")
                state   = _get(row, "state", "region_state")
                region  = _get(row, "region", "nanpa_region")
                status  = _get(row, "status", "block_status")

                line_type = classify_line_type(carrier, ocn)

                buffer.append({
                    "npa_nxx_x":    npa_nxx_x,
                    "npa":          npa,
                    "nxx":          nxx,
                    "block":        block,
                    "carrier":      carrier,
                    "line_type":    line_type,
                    "ocn":          ocn,
                    "state":        (state or "").upper()[:2] or None,
                    "region":       region,
                    "status":       status,
                })

                if len(buffer) >= UPSERT_CHUNK:
                    db.table("nanpa_carrier_prefixes").upsert(
                        buffer, on_conflict="npa_nxx_x"
                    ).execute()
                    inserted += len(buffer)
                    buffer.clear()

            if buffer:
                db.table("nanpa_carrier_prefixes").upsert(
                    buffer, on_conflict="npa_nxx_x"
                ).execute()
                inserted += len(buffer)
                buffer.clear()

    except Exception as e:
        duration = time.monotonic() - t_start
        notes = f"FAILED - {type(e).__name__}: {str(e)[:300]}"
        logger.error(f"[{job_name}] {notes}")
        await _log_job(job_name, duration_seconds=duration, status="failed", notes=notes)
        return {"job": job_name, "status": "failed", "error": str(e)}

    # Clear the in-process lru_cache so subsequent lookups pick up fresh data
    try:
        from ..lib import carrier_lookup
        carrier_lookup.clear_cache()
    except Exception:
        pass

    duration = time.monotonic() - t_start
    notes = f"inserted={inserted}, invalid={invalid}"
    await _log_job(job_name, leads_processed=inserted, duration_seconds=duration,
                   status="success", notes=notes)
    return {
        "job":              job_name,
        "inserted":         inserted,
        "invalid":          invalid,
        "duration_seconds": round(duration, 3),
        "status":           "success",
    }

# ─────────────────────────────────────────────────────────────────────────────
# JOB DISPATCH MAP — maps name → async callable
# ─────────────────────────────────────────────────────────────────────────────

JOB_FUNCTIONS = {
    "classify_segments":    job_classify_segments,
    "enrich_stage1":        job_enrich_stage1,
    "cost_digest":          job_cost_digest,
    "stale_cleanup":        job_stale_cleanup,
    "email_clean_nightly":      job_email_clean_nightly,
    "email_clean_poll_batches": job_email_clean_poll_batches,
    "enrich_stage3_nightly":    job_enrich_stage3_nightly,
    "enrich_stage4_nightly": job_enrich_stage4_nightly,
    "daily_cost_report":    job_daily_cost_report,
    "campaign_sync_hourly": job_campaign_sync_hourly,
    "fed_dnc_refresh_monthly": job_fed_dnc_refresh_monthly,
    "nanpa_refresh_monthly":   job_nanpa_refresh_monthly,
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

    # Job 5: email_clean_nightly (V2) — daily at 00:00 UTC (midnight)
    # Submits N x 25K-email batches via POST /api/v1/batches. Defaults to 4
    # batches/night (configurable via admin_settings). Prioritizes HOT > WARM
    # > COLD > unclassified, pain_score DESC.
    scheduler.add_job(
        job_email_clean_nightly,
        trigger=CronTrigger(hour=0, minute=0),
        id="email_clean_nightly",
        name="Nightly Email Clean Submit (Truelist v2)",
        replace_existing=True,
        misfire_grace_time=3600,  # 1-hour grace window
    )

    # Job 5b: email_clean_poll_batches — every 30 minutes
    # Companion poller to the nightly submitter. Picks up in-flight
    # truelist_batches rows, persists state, and ingests annotated CSV
    # once Truelist marks a batch `completed`. Idempotent — also runs as
    # the webhook-fallback when /webhooks/truelist/batch-complete misses.
    scheduler.add_job(
        job_email_clean_poll_batches,
        trigger=IntervalTrigger(minutes=30),
        id="email_clean_poll_batches",
        name="Truelist Batch Poller (30min)",
        replace_existing=True,
        misfire_grace_time=900,
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

    # Job 8: daily_cost_report — daily at 16:00 UTC (09:00 PDT / 08:00 PST)
    # Rolling 24h cost + pipeline digest emailed via Resend
    scheduler.add_job(
        job_daily_cost_report,
        trigger=CronTrigger(hour=16, minute=0),
        id="daily_cost_report",
        name="Daily Cost Report Email",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # Job 9: campaign_sync_hourly — every 60 minutes
    # Pulls campaign stats from RI for configured (project, server) pairs
    scheduler.add_job(
        job_campaign_sync_hourly,
        trigger=IntervalTrigger(minutes=60),
        id="campaign_sync_hourly",
        name="Hourly Campaign Stats Sync",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Job 10: fed_dnc_refresh_monthly — 1st of every month @ 04:00 UTC
    # Rebuilds fed_dnc_list from /opt/fed_dnc_latest.zip. Until Jim's FCC
    # registration (submitted 2026-05-24) is approved, this job logs a
    # warning and exits skipped — does NOT fail.
    scheduler.add_job(
        job_fed_dnc_refresh_monthly,
        trigger=CronTrigger(day=1, hour=4, minute=0),
        id="fed_dnc_refresh_monthly",
        name="Federal DNC List Refresh (monthly)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Job 11: nanpa_refresh_monthly — 1st of every month @ 04:30 UTC
    # Rebuilds nanpa_blocked_prefixes from /opt/nanpa_latest.csv. Same
    # first-run posture as the FCC job — missing file warns and skips.
    scheduler.add_job(
        job_nanpa_refresh_monthly,
        trigger=CronTrigger(day=1, hour=4, minute=30),
        id="nanpa_refresh_monthly",
        name="NANPA Invalid Prefix Refresh (monthly)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    logger.info(f"⏰ APScheduler configured: {len(scheduler.get_jobs())} jobs registered")
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
async def run_job_manually(job_name: str, background: bool = False):
    """
    Manually trigger a scheduled job by name.

    Default: runs synchronously and returns the job result.
    Pass ?background=true to fire-and-forget — useful for jobs that may
    exceed the Render ingress timeout (e.g. classify_segments on the full
    549K row table). Poll /scheduler/status afterward to see completion.

    Valid names: classify_segments, enrich_stage1, cost_digest,
    stale_cleanup, email_clean_nightly, enrich_stage3_nightly,
    enrich_stage4_nightly.
    """
    if job_name not in JOB_FUNCTIONS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Job '{job_name}' not found. "
                f"Valid jobs: {sorted(JOB_FUNCTIONS.keys())}"
            ),
        )

    logger.info(f"Manual trigger: {job_name} (background={background})")

    if background:
        async def _runner():
            try:
                await JOB_FUNCTIONS[job_name]()
            except Exception as e:
                logger.exception(f"Background job {job_name} crashed: {e}")
        asyncio.create_task(_runner())
        return {
            "status":       "accepted",
            "triggered_by": "manual",
            "mode":         "background",
            "message":      f"Job '{job_name}' started in background. Poll /scheduler/status for completion.",
        }

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
