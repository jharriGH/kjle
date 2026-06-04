"""
KJLE — Scrape Job Queue (Phase 4.2)
File: api/routes/scrape_jobs.py

Routes (PREFIX /kjle/v1 added by main.py):
  POST   /scrape/start                       — auth (master);  enqueue a job
  GET    /scrape/jobs/poll                   — auth (worker);  claim queued jobs
  POST   /scrape/jobs/{job_id}/started       — auth (worker);  mark running
  POST   /scrape/jobs/{job_id}/complete      — auth (worker);  mark complete/failed
  GET    /scrape/jobs                        — auth (master);  paginated list
  GET    /scrape/jobs/{job_id}               — auth (master);  single lookup

Design notes (Phase 4.2):
- REVERSE-POLL architecture. Workers (Z820 + VPS) run a stateless daemon that
  POLLs /scrape/jobs/poll every ~15s. Workers have no public URL; KJLE is the
  sole source of truth for the queue.
- Two auth realms:
    * verify_api_key            (X-API-Key)        — operators, n8n, Lovable UI
    * verify_worker_api_key     (X-Worker-API-Key) — least-privilege daemon key
  Misconfigured WORKER_API_KEY env returns 503 (not 401) so the failure is
  visibly an ops issue, not a fake-credentials attack.
- Optimistic locking on poll: SELECT queued rows, then UPDATE with
  WHERE status='queued' guard. If a competing worker won the race, the
  UPDATE affects 0 rows and we skip that job. Only confirmed claims are
  returned to the caller.
- No external HTTP libs imported here. Worker dispatch is pull-only; KJLE
  never reaches out to the workers.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from ..database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Auth ─────────────────────────────────────────────────────────────────────
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")
WORKER_API_KEY = os.environ.get("WORKER_API_KEY")


def verify_api_key(x_api_key: str = Header(...)) -> None:
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def verify_worker_api_key(
    x_worker_api_key: str = Header(..., alias="X-Worker-API-Key"),
) -> None:
    """
    Worker-only auth. Daemons on Z820 / VPS use this least-privilege key
    instead of the master API_SECRET_KEY.

    Misconfiguration (WORKER_API_KEY env unset) returns 503 so the failure
    is visibly an ops issue rather than masquerading as a credential rejection.
    """
    if not WORKER_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="server_misconfigured: WORKER_API_KEY env var unset",
        )
    if x_worker_api_key != WORKER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid worker API key")


# ── Constants ────────────────────────────────────────────────────────────────
ALLOWED_TARGETS = {
    "Google Maps Quick",
    "Google Maps Full",
    "Google Places",
    "Yahoo Local",
    "Yellow USA Full",
    "Yellow USA Quick",
    "Email Finder",
    "Bing Maps Full",
    "Bing Maps Quick",
    "Yellow AU",
    "Yellow DE",
}

ALLOWED_WORKERS = {"z820", "vps", "auto"}
ALLOWED_LIST_FILTER_STATUSES = {
    "queued", "claimed", "running", "complete", "failed", "cancelled",
}


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class ScrapeStartRequest(BaseModel):
    target: str
    keyword: Optional[str] = None
    location: Optional[str] = None
    keyword_list: Optional[str] = None
    location_list: Optional[str] = None
    custom_url: Optional[str] = None
    max_listings: Optional[int] = Field(None, ge=1, le=10000)
    find_emails: bool = False
    requested_worker: str = "auto"
    requested_by: str = "operator"
    correlation_id: Optional[str] = None
    metadata: Optional[dict] = None


class ScrapeStartedRequest(BaseModel):
    worker_id: str


class ScrapeCompleteRequest(BaseModel):
    worker_id: str
    success: bool
    result_run_id: Optional[str] = None
    result_total_records: Optional[int] = None
    result_inserted: Optional[int] = None
    result_duplicates: Optional[int] = None
    result_filtered: Optional[int] = None
    error_message: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_runnable_inputs(req: ScrapeStartRequest) -> bool:
    """At least one of: (keyword AND location), keyword_list, location_list, custom_url."""
    if req.keyword and req.location:
        return True
    if req.keyword_list:
        return True
    if req.location_list:
        return True
    if req.custom_url:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# POST /scrape/start
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/scrape/start")
async def scrape_start(
    body: ScrapeStartRequest,
    x_api_key: str = Header(...),
) -> dict:
    """
    Enqueue a Local Scraper job. Worker daemons will pick it up via /poll
    on their next ~15s tick.
    """
    verify_api_key(x_api_key)

    if body.target not in ALLOWED_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_target: must be one of {sorted(ALLOWED_TARGETS)}",
        )

    if body.requested_worker not in ALLOWED_WORKERS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_requested_worker: must be one of "
                   f"{sorted(ALLOWED_WORKERS)}",
        )

    if not _has_runnable_inputs(body):
        raise HTTPException(
            status_code=400,
            detail="missing_inputs: provide (keyword AND location), "
                   "keyword_list, location_list, or custom_url",
        )

    now = _now_iso()
    row = {
        "target":           body.target,
        "keyword":          body.keyword,
        "location":         body.location,
        "keyword_list":     body.keyword_list,
        "location_list":    body.location_list,
        "custom_url":       body.custom_url,
        "max_listings":     body.max_listings,
        "find_emails":      body.find_emails,
        "requested_worker": body.requested_worker,
        "status":           "queued",
        "queued_at":        now,
        "requested_by":     body.requested_by,
        "correlation_id":   body.correlation_id,
        "metadata":         body.metadata or {},
    }
    row = {k: v for k, v in row.items() if v is not None}

    db = get_db()
    try:
        r = db.table("scrape_jobs").insert(row).execute()
    except Exception as e:
        logger.error("scrape_jobs.start: insert failed: %s", e)
        raise HTTPException(
            status_code=500, detail=f"insert_failed: {str(e)[:200]}"
        )

    inserted = r.data[0] if r.data else {}
    return {
        "id":               inserted.get("id"),
        "status":           inserted.get("status", "queued"),
        "requested_worker": inserted.get("requested_worker",
                                         body.requested_worker),
        "queued_at":        inserted.get("queued_at", now),
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /scrape/jobs/poll
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/scrape/jobs/poll")
async def scrape_jobs_poll(
    worker_id: str = Query(..., description="z820 | vps"),
    max_jobs: int = Query(1, ge=1, le=5),
    x_worker_api_key: str = Header(..., alias="X-Worker-API-Key"),
) -> dict:
    """
    Worker poll: claim up to max_jobs queued rows whose requested_worker
    matches the caller (or is 'auto'). Returns ONLY rows the caller
    successfully CAS-locked.
    """
    verify_worker_api_key(x_worker_api_key)

    if worker_id not in ("z820", "vps"):
        raise HTTPException(
            status_code=400,
            detail="invalid_worker_id: must be 'z820' or 'vps'",
        )

    db = get_db()

    # 1) Pull candidates oldest-first, restricted to this worker (or 'auto').
    try:
        candidates = (
            db.table("scrape_jobs").select("*")
              .eq("status", "queued")
              .in_("requested_worker", [worker_id, "auto"])
              .order("queued_at", desc=False)
              .limit(max_jobs)
              .execute()
              .data
            or []
        )
    except Exception as e:
        logger.error("scrape_jobs.poll: candidate read failed: %s", e)
        raise HTTPException(
            status_code=500, detail=f"poll_read_failed: {str(e)[:200]}"
        )

    claimed: list[dict] = []
    now = _now_iso()

    # 2) Best-effort CAS claim per candidate. Skip rows the UPDATE missed.
    for cand in candidates:
        job_id = cand.get("id")
        if not job_id:
            continue
        try:
            upd = (
                db.table("scrape_jobs").update({
                    "status":          "claimed",
                    "assigned_worker": worker_id,
                    "claimed_at":      now,
                    "updated_at":      now,
                })
                .eq("id", job_id)
                .eq("status", "queued")
                .execute()
            )
        except Exception as e:
            logger.warning(
                "scrape_jobs.poll: CAS update failed for %s: %s", job_id, e
            )
            continue

        rows = upd.data or []
        if rows:
            claimed.append(rows[0])

    return {"jobs": claimed}


# ─────────────────────────────────────────────────────────────────────────────
# POST /scrape/jobs/{job_id}/started
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/scrape/jobs/{job_id}/started")
async def scrape_jobs_started(
    job_id: str,
    body: ScrapeStartedRequest,
    x_worker_api_key: str = Header(..., alias="X-Worker-API-Key"),
) -> dict:
    """
    Worker reports the job has begun execution.
    Transitions claimed → running. 409 if the row is not in the expected state.
    """
    verify_worker_api_key(x_worker_api_key)

    db = get_db()
    now = _now_iso()
    try:
        upd = (
            db.table("scrape_jobs").update({
                "status":     "running",
                "started_at": now,
                "updated_at": now,
            })
            .eq("id", job_id)
            .eq("assigned_worker", body.worker_id)
            .eq("status", "claimed")
            .execute()
        )
    except Exception as e:
        logger.error("scrape_jobs.started: update failed: %s", e)
        raise HTTPException(
            status_code=500, detail=f"started_update_failed: {str(e)[:200]}"
        )

    rows = upd.data or []
    if not rows:
        raise HTTPException(
            status_code=409,
            detail="state_mismatch: job not in 'claimed' state for this worker",
        )

    return {
        "id":         job_id,
        "status":     "running",
        "started_at": now,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /scrape/jobs/{job_id}/complete
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/scrape/jobs/{job_id}/complete")
async def scrape_jobs_complete(
    job_id: str,
    body: ScrapeCompleteRequest,
    x_worker_api_key: str = Header(..., alias="X-Worker-API-Key"),
) -> dict:
    """
    Worker reports the job has finished (success or failure).
    Sets terminal state + result fields. 404 if no matching row.
    """
    verify_worker_api_key(x_worker_api_key)

    now = _now_iso()
    if body.success:
        patch = {
            "status":               "complete",
            "completed_at":         now,
            "updated_at":           now,
            "result_run_id":        body.result_run_id,
            "result_total_records": body.result_total_records,
            "result_inserted":      body.result_inserted,
            "result_duplicates":    body.result_duplicates,
            "result_filtered":      body.result_filtered,
            "error_message":        None,
        }
    else:
        patch = {
            "status":               "failed",
            "failed_at":            now,
            "updated_at":           now,
            "result_run_id":        body.result_run_id,
            "error_message":        body.error_message
                                    or "worker_reported_failure",
        }
    patch = {k: v for k, v in patch.items() if v is not None or k in
             ("error_message",)}

    db = get_db()
    try:
        upd = (
            db.table("scrape_jobs").update(patch)
            .eq("id", job_id)
            .eq("assigned_worker", body.worker_id)
            .execute()
        )
    except Exception as e:
        logger.error("scrape_jobs.complete: update failed: %s", e)
        raise HTTPException(
            status_code=500, detail=f"complete_update_failed: {str(e)[:200]}"
        )

    rows = upd.data or []
    if not rows:
        raise HTTPException(
            status_code=404,
            detail="job_not_found: no row matched id + worker_id",
        )

    return {
        "id":     job_id,
        "status": patch["status"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /scrape/jobs
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/scrape/jobs")
async def scrape_jobs_list(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None),
    worker: Optional[str] = Query(
        None, description="Filter by assigned_worker"
    ),
    x_api_key: str = Header(...),
) -> dict:
    """
    Paginated list of scrape jobs, newest first. Operator / Lovable UI view.
    """
    verify_api_key(x_api_key)

    if status and status not in ALLOWED_LIST_FILTER_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_status_filter: must be one of "
                   f"{sorted(ALLOWED_LIST_FILTER_STATUSES)}",
        )

    db = get_db()
    try:
        q = (
            db.table("scrape_jobs").select("*")
              .order("created_at", desc=True)
              .range(offset, offset + limit - 1)
        )
        if status:
            q = q.eq("status", status)
        if worker:
            q = q.eq("assigned_worker", worker)
        rows = q.execute().data or []
    except Exception as e:
        logger.error("scrape_jobs.list: query failed: %s", e)
        raise HTTPException(
            status_code=500, detail=f"list_query_failed: {str(e)[:200]}"
        )

    return {
        "count":   len(rows),
        "limit":   limit,
        "offset":  offset,
        "filters": {"status": status, "worker": worker},
        "jobs":    rows,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /scrape/jobs/{job_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/scrape/jobs/{job_id}")
async def scrape_jobs_get(
    job_id: str,
    x_api_key: str = Header(...),
) -> dict:
    """Single-job lookup. 404 if not found."""
    verify_api_key(x_api_key)

    db = get_db()
    try:
        r = (
            db.table("scrape_jobs").select("*")
              .eq("id", job_id).limit(1).execute()
        )
        row = r.data[0] if r.data else None
    except Exception as e:
        logger.error("scrape_jobs.get: query failed: %s", e)
        raise HTTPException(
            status_code=500, detail=f"get_query_failed: {str(e)[:200]}"
        )

    if not row:
        raise HTTPException(
            status_code=404, detail=f"job_not_found: {job_id}"
        )

    return row
