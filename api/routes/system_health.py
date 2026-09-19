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

    non_healthy = sum(
        1 for j in enabled_jobs
        if j.get("status") not in HEALTHY_STATUSES and j.get("status") is not None
    )

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
    }
