"""
KJLE API — Scan Routes (WebSignalz Phase 3)
POST /kjle/v1/scan               — enqueue a scan job
GET  /kjle/v1/scan/queue         — scanner health & queue depth (read-only, polled ~60s)
GET  /kjle/v1/scan/{scan_job_id} — job status + result when done
"""
import os
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from ..database import get_db

router = APIRouter()

API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")
# Matches daemon STALL_THRESHOLD_S=300 in workers/scan_daemon/daemon.py.
_STALL_MINUTES = 5
# SCAN_CONCURRENCY mirrors daemon default; env var is the authoritative source.
_SCAN_CONCURRENCY = min(int(os.environ.get("SCAN_CONCURRENCY", "4")), 16)
# In-process response cache — avoids hammering DB on every 60-s poll cycle.
_CACHE_TTL_S = 30
_queue_cache: dict = {"data": None, "expires_at": 0.0}


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


class ScanRequest(BaseModel):
    url: str
    lead_id: Optional[str] = None
    client_id: Optional[str] = None
    priority: Optional[int] = 0


# ── IMPORTANT: this route must stay above GET /scan/{scan_job_id} so the literal
#    path segment "queue" is never parsed as a numeric scan_job_id. ─────────────
@router.get("/scan/queue", dependencies=[Depends(verify_api_key)])
async def get_scan_queue(db=Depends(get_db)):
    now_ts = time.monotonic()
    if _queue_cache["data"] is not None and now_ts < _queue_cache["expires_at"]:
        return _queue_cache["data"]

    now_utc = datetime.now(timezone.utc)

    # running rows = authoritative live worker count (partial idx: idx_scan_jobs_running)
    r_running = (
        db.table("scan_jobs")
        .select("id", count="exact")
        .eq("status", "running")
        .limit(1)
        .execute()
    )
    worker_count_busy = r_running.count or 0

    # total queued (partial idx: idx_scan_jobs_queued)
    r_queued = (
        db.table("scan_jobs")
        .select("id", count="exact")
        .eq("status", "queued")
        .limit(1)
        .execute()
    )
    queued_total = r_queued.count or 0

    # high-priority queued — priority >= 9 (partial idx: idx_scan_jobs_queued_priority)
    r_p9 = (
        db.table("scan_jobs")
        .select("id", count="exact")
        .eq("status", "queued")
        .gte("priority", 9)
        .limit(1)
        .execute()
    )
    queued_p9plus = r_p9.count or 0

    # oldest enqueued (partial idx: idx_scan_jobs_queued_enqueued)
    r_oldest = (
        db.table("scan_jobs")
        .select("enqueued_at")
        .eq("status", "queued")
        .order("enqueued_at", desc=False)
        .limit(1)
        .execute()
    )
    oldest_queued_enqueued_at = r_oldest.data[0]["enqueued_at"] if r_oldest.data else None

    # last completed job (partial idx: idx_scan_jobs_done_finished)
    r_last = (
        db.table("scan_jobs")
        .select("finished_at")
        .eq("status", "done")
        .order("finished_at", desc=True)
        .limit(1)
        .execute()
    )
    last_output_at = r_last.data[0]["finished_at"] if r_last.data else None

    # Status classification (stall_minutes=5 matches daemon STALL_THRESHOLD_S=300):
    # healthy  – queue empty OR a job finished within the last stall window
    # degraded – jobs are queued but nothing has finished within stall_minutes
    # down     – no completion within 3x stall window while queued_total > 0
    if last_output_at is not None:
        ts = datetime.fromisoformat(last_output_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        minutes_stale = (now_utc - ts).total_seconds() / 60
    else:
        minutes_stale = float("inf")

    if queued_total == 0 or minutes_stale < _STALL_MINUTES:
        status = "healthy"
    elif queued_total > 0 and minutes_stale >= 3 * _STALL_MINUTES:
        status = "down"
    else:
        status = "degraded"

    result = {
        "as_of": now_utc.isoformat(),
        "status": status,
        "last_output_at": last_output_at,
        "worker_count": _SCAN_CONCURRENCY,
        "worker_count_busy": worker_count_busy,
        "queued_total": queued_total,
        "queued_p9plus": queued_p9plus,
        "oldest_queued_enqueued_at": oldest_queued_enqueued_at,
        "stall_minutes": _STALL_MINUTES,
    }

    _queue_cache["data"] = result
    _queue_cache["expires_at"] = now_ts + _CACHE_TTL_S
    return result


@router.post("/scan", dependencies=[Depends(verify_api_key)])
async def enqueue_scan(req: ScanRequest, db=Depends(get_db)):
    row = {
        "url": req.url,
        "lead_id": req.lead_id,
        "client_id": req.client_id,
        "priority": req.priority or 0,
        "status": "queued",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    resp = db.table("scan_jobs").insert(row).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to enqueue scan job")
    job = resp.data[0]
    return {"scan_job_id": job["id"], "status": "queued"}


@router.get("/scan/{scan_job_id}", dependencies=[Depends(verify_api_key)])
async def get_scan_job(scan_job_id: int, db=Depends(get_db)):
    resp = db.table("scan_jobs").select("*").eq("id", scan_job_id).execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Scan job not found")
    job = resp.data[0]
    result = None
    if job.get("scan_result_id"):
        res_resp = (
            db.table("scan_results")
            .select("*")
            .eq("id", job["scan_result_id"])
            .execute()
        )
        result = res_resp.data[0] if res_resp.data else None
    return {"job": job, "result": result}
