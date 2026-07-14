"""
KJLE API — Scan Routes (WebSignalz Phase 3)
POST /kjle/v1/scan              — enqueue a scan job
GET  /kjle/v1/scan/{scan_job_id} — job status + result when done
"""
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from ..database import get_db

router = APIRouter()

API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


class ScanRequest(BaseModel):
    url: str
    lead_id: Optional[str] = None
    client_id: Optional[str] = None
    priority: Optional[int] = 0


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
