"""
KJLE — GET /kjle/v1/system/health

Returns current status of all enabled job_health rows, computed by the
job_heartbeat.py timer that writes runtime columns every 15 min.

Auth: same x-api-key header used by other GET routes.
"""

import os
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException

_SCAN_CONCURRENCY = min(int(os.environ.get("SCAN_CONCURRENCY", "4")), 16)
_SCAN_STALL_MINUTES = 5

logger = logging.getLogger(__name__)

router = APIRouter()

API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")

HEALTHY_STATUSES = {"ok", "healthy"}


def _verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


@router.get("/system/health")
async def system_health(_auth=Depends(_verify_api_key)):
    """
    Reads all rows from job_health (no full-table scan — PK-keyed SELECT).
    Returns a summary + per-job list with runtime metrics.
    """
    from ..database import get_db
    db = get_db()

    try:
        res = (
            db.table("job_health")
            .select(
                "job_key, display_name, category, systemd_unit, enabled, status, "
                "throughput_window, backlog, last_output_at, last_checked_at, "
                "last_alert_at, last_auto_action, detail, updated_at, "
                "window_minutes, stall_minutes, min_expected"
            )
            .order("job_key")
            .execute()
        )
    except Exception as e:
        logger.error(f"[system_health] job_health query failed: {e}")
        raise HTTPException(status_code=503, detail=f"job_health read failed: {e}")

    jobs = res.data or []
    enabled_jobs = [j for j in jobs if j.get("enabled")]

    # ── Live scan_daemon health (same queries as GET /scan/queue) ─────────────
    scan_daemon_health: dict = {}
    try:
        now_utc = datetime.now(timezone.utc)
        r_running = db.table("scan_jobs").select("id", count="exact").eq("status", "running").limit(1).execute()
        r_queued  = db.table("scan_jobs").select("id", count="exact").eq("status", "queued").limit(1).execute()
        r_p9      = db.table("scan_jobs").select("id", count="exact").eq("status", "queued").gte("priority", 9).limit(1).execute()
        r_oldest  = db.table("scan_jobs").select("enqueued_at").eq("status", "queued").order("enqueued_at", desc=False).limit(1).execute()
        r_last    = db.table("scan_jobs").select("finished_at").eq("status", "done").order("finished_at", desc=True).limit(1).execute()

        worker_count_busy         = r_running.count or 0
        queued_total              = r_queued.count or 0
        queued_p9plus             = r_p9.count or 0
        oldest_queued_enqueued_at = r_oldest.data[0]["enqueued_at"] if r_oldest.data else None
        last_output_at            = r_last.data[0]["finished_at"] if r_last.data else None

        if last_output_at is not None:
            ts = datetime.fromisoformat(last_output_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            minutes_stale = (now_utc - ts).total_seconds() / 60
        else:
            minutes_stale = float("inf")

        if queued_total == 0 or minutes_stale < _SCAN_STALL_MINUTES:
            sd_status = "healthy"
        elif minutes_stale >= 3 * _SCAN_STALL_MINUTES:
            sd_status = "down"
        else:
            sd_status = "degraded"

        scan_daemon_health = {
            "status":                    sd_status,
            "last_output_at":            last_output_at,
            "worker_count":              _SCAN_CONCURRENCY,
            "worker_count_busy":         worker_count_busy,
            "queued_total":              queued_total,
            "queued_p9plus":             queued_p9plus,
            "oldest_queued_enqueued_at": oldest_queued_enqueued_at,
            "stall_minutes":             _SCAN_STALL_MINUTES,
            "as_of":                     now_utc.isoformat(),
        }
    except Exception as e:
        logger.error(f"[system_health] scan_daemon query failed: {e}")
        scan_daemon_health = {"status": "unknown", "error": str(e)}

    non_healthy = sum(
        1 for j in enabled_jobs
        if j.get("status") not in HEALTHY_STATUSES and j.get("status") is not None
    )
    if scan_daemon_health.get("status") not in HEALTHY_STATUSES:
        non_healthy += 1

    checked_ats = [
        j["last_checked_at"] for j in enabled_jobs if j.get("last_checked_at")
    ]
    max_checked_at = max(checked_ats) if checked_ats else None

    return {
        "summary": {
            "all_ok":    non_healthy == 0 and bool(enabled_jobs),
            "issues":    non_healthy,
            "checked_at": max_checked_at,
        },
        "jobs": jobs,
        "scan_daemon": scan_daemon_health,
    }
