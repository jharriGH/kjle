"""
Export Engine — CSV + ReachInbox
KJLE - King James Lead Empire
Routes: /kjle/v1/export/*

Exports leads to CSV download or ReachInbox campaigns.
Logs all exports to export_log table.
"""

import csv
import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..database import get_db
from ..config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "business_name",
    "phone",
    "email",
    "website",
    "city",
    "state",
    "niche_slug",
    "pain_score",
    "segment_label",
    "enrichment_stage",
    "google_stars",
    "yelp_rating",
    "outscraper_rating",
    "has_schema_markup",
    "fit_demoenginez",
    "created_at",
]

REACHINBOX_BASE = "https://api.reachinbox.ai/api/v1/onebox/campaigns"
HTTP_TIMEOUT = 15.0


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class ReachInboxExportRequest(BaseModel):
    campaign_id: str
    segment_label: Optional[str] = None
    niche_slug: Optional[str] = None
    state: Optional[str] = None
    min_pain: int = 0
    limit: int = 100
    email_field: str = "email"


class ExportLogRequest(BaseModel):
    export_type: str       # "csv" | "reachinbox"
    destination: str       # filename or campaign_id
    filters_used: dict = {}
    lead_count: int = 0
    status: str = "success"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _extract_first_name(business_name: str) -> str:
    """
    Best-effort first name extraction from a business name.
    Handles patterns like "John's Plumbing", "Smith & Sons", "Dr. Maria Lopez".
    Falls back to empty string — never crashes.
    """
    if not business_name:
        return ""
    # Strip common business suffixes first
    cleaned = re.sub(
        r"\b(LLC|Inc|Corp|Co|Ltd|DBA|and|&|Sons|Sisters|Brothers|Group|Services|Solutions)\b",
        "", business_name, flags=re.IGNORECASE
    ).strip()
    # Take the first word that looks like a proper name (capitalized, 2+ chars)
    words = cleaned.split()
    for word in words:
        word_clean = re.sub(r"[^a-zA-Z]", "", word)
        if len(word_clean) >= 2 and word_clean[0].isupper():
            return word_clean
    return ""


def _build_lead_query(
    db,
    segment_label: Optional[str],
    niche_slug: Optional[str],
    state: Optional[str],
    min_pain: int,
    enrichment_stage: Optional[int],
    limit: int,
    require_email: bool = False,
):
    """Builds a filtered leads query. Returns Supabase query object."""
    query = (
        db.table("leads")
        .select(", ".join(CSV_COLUMNS))
        .eq("is_active", True)
    )

    if segment_label:
        query = query.eq("segment_label", segment_label)
    if niche_slug:
        query = query.eq("niche_slug", niche_slug)
    if state:
        query = query.eq("state", state.upper())
    if min_pain > 0:
        query = query.gte("pain_score", min_pain)
    if enrichment_stage is not None:
        query = query.eq("enrichment_stage", enrichment_stage)
    if require_email:
        query = query.not_.is_("email", "null").neq("email", "")

    return query.order("pain_score", desc=True).limit(limit)


async def _write_export_log(
    db,
    export_type: str,
    destination: str,
    filters_used: dict,
    lead_count: int,
    status: str,
) -> None:
    """Inserts a record into export_log. Silently absorbs failures."""
    try:
        db.table("export_log").insert({
            "export_type":   export_type,
            "destination":   destination,
            "filters_used":  filters_used,
            "lead_count":    lead_count,
            "status":        status,
            "created_at":    _now_iso(),
        }).execute()
    except Exception as e:
        logger.error(f"Failed to write export log: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# GET /export/csv
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/export/csv")
async def export_csv(
    segment_label: Optional[str] = Query(None, description="hot | warm | cold"),
    niche_slug: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    min_pain: int = Query(0, ge=0, le=100),
    enrichment_stage: Optional[int] = Query(None, ge=0, le=4),
    limit: int = Query(500, ge=1, le=5000),
):
    """
    Exports leads as a downloadable CSV file.
    Returns StreamingResponse with Content-Disposition attachment header.
    """
    db = get_db()

    query = _build_lead_query(
        db, segment_label, niche_slug, state, min_pain, enrichment_stage, limit
    )
    leads = query.execute().data or []

    # Build CSV in memory
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()

    for lead in leads:
        # Ensure all columns present, missing = empty string
        row = {col: (lead.get(col) if lead.get(col) is not None else "") for col in CSV_COLUMNS}
        writer.writerow(row)

    output.seek(0)
    csv_bytes = output.getvalue().encode("utf-8")
    filename = f"kjle_export_{_timestamp_slug()}.csv"

    # Log the export
    filters_used = {
        "segment_label": segment_label,
        "niche_slug": niche_slug,
        "state": state,
        "min_pain": min_pain,
        "enrichment_stage": enrichment_stage,
        "limit": limit,
    }
    await _write_export_log(db, "csv", filename, filters_used, len(leads), "success")

    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Lead-Count": str(len(leads)),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /export/reachinbox
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/export/reachinbox")
async def export_to_reachinbox(body: ReachInboxExportRequest):
    """
    Exports leads to a ReachInbox campaign via the Fusion API.
    Only exports leads with non-null, non-risky emails.
    Returns summary: exported, skipped_no_email, skipped_risky, failed, campaign_id.
    """
    db = get_db()

    api_key = settings.REACHINBOX_API_KEY
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="REACHINBOX_API_KEY is not configured. Set it in your Render environment variables.",
        )

    limit = min(body.limit, 1000)

    # Fetch leads — require email at DB level for efficiency
    query = _build_lead_query(
        db,
        body.segment_label,
        body.niche_slug,
        body.state,
        body.min_pain,
        None,   # enrichment_stage not filtered for RI exports
        limit,
        require_email=True,
    )
    # Also pull email_state for risky filtering
    leads_raw = (
        db.table("leads")
        .select(", ".join(CSV_COLUMNS) + ", email_state, business_name")
        .eq("is_active", True)
        .not_.is_("email", "null")
        .neq("email", "")
        .order("pain_score", desc=True)
        .limit(limit)
    )

    if body.segment_label:
        leads_raw = leads_raw.eq("segment_label", body.segment_label)
    if body.niche_slug:
        leads_raw = leads_raw.eq("niche_slug", body.niche_slug)
    if body.state:
        leads_raw = leads_raw.eq("state", body.state.upper())
    if body.min_pain > 0:
        leads_raw = leads_raw.gte("pain_score", body.min_pain)

    leads = leads_raw.execute().data or []

    if not leads:
        return {
            "status": "success",
            "message": "No leads found matching export filters",
            "summary": {
                "exported": 0,
                "skipped_no_email": 0,
                "skipped_risky": 0,
                "failed": 0,
                "campaign_id": body.campaign_id,
            },
        }

    endpoint = f"{REACHINBOX_BASE}/{body.campaign_id}/leads"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    exported = 0
    skipped_no_email = 0
    skipped_risky = 0
    failed = 0

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for lead in leads:
            email = (lead.get(body.email_field) or lead.get("email") or "").strip()

            if not email:
                skipped_no_email += 1
                continue

            if (lead.get("email_state") or "").lower() == "risky":
                skipped_risky += 1
                continue

            business_name = lead.get("business_name") or ""
            first_name = _extract_first_name(business_name)

            ri_payload = {
                "email":       email,
                "firstName":   first_name,
                "lastName":    "",
                "companyName": business_name,
                "phone":       lead.get("phone") or "",
                "website":     lead.get("website") or "",
                "customFields": {
                    "pain_score": lead.get("pain_score"),
                    "niche":      lead.get("niche_slug"),
                    "city":       lead.get("city"),
                    "state":      lead.get("state"),
                },
            }

            try:
                resp = await client.post(endpoint, headers=headers, json=ri_payload)
                if resp.status_code in (200, 201):
                    exported += 1
                elif resp.status_code == 409:
                    # Lead already exists in campaign — count as exported
                    exported += 1
                    logger.info(f"RI duplicate (409) for {email} — campaign already has this lead")
                else:
                    failed += 1
                    logger.warning(f"RI push failed for {email}: HTTP {resp.status_code} — {resp.text[:200]}")
            except httpx.TimeoutException:
                failed += 1
                logger.warning(f"RI push timeout for {email}")
            except Exception as e:
                failed += 1
                logger.error(f"RI push error for {email}: {type(e).__name__}: {e}")

    filters_used = {
        "campaign_id":   body.campaign_id,
        "segment_label": body.segment_label,
        "niche_slug":    body.niche_slug,
        "state":         body.state,
        "min_pain":      body.min_pain,
        "limit":         limit,
    }
    status_str = "success" if failed == 0 else ("partial" if exported > 0 else "failed")
    await _write_export_log(
        db, "reachinbox", body.campaign_id, filters_used, exported, status_str
    )

    return {
        "status": status_str,
        "summary": {
            "exported":          exported,
            "skipped_no_email":  skipped_no_email,
            "skipped_risky":     skipped_risky,
            "failed":            failed,
            "total_fetched":     len(leads),
            "campaign_id":       body.campaign_id,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /export/history
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/export/history")
async def get_export_history(
    limit: int = Query(20, ge=1, le=200),
):
    """
    Returns recent export log entries sorted by created_at desc.
    Covers both CSV and ReachInbox export events.
    """
    db = get_db()

    rows = (
        db.table("export_log")
        .select("id, export_type, destination, filters_used, lead_count, status, created_at")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data or []
    )

    return {
        "status": "success",
        "count": len(rows),
        "history": rows,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /export/log
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/export/log")
async def log_export(body: ExportLogRequest):
    """
    Internal endpoint to manually log an export event.
    Useful for logging exports triggered by external tools or scripts.
    """
    db = get_db()

    payload = {
        "export_type":  body.export_type,
        "destination":  body.destination,
        "filters_used": body.filters_used,
        "lead_count":   body.lead_count,
        "status":       body.status,
        "created_at":   _now_iso(),
    }

    result = db.table("export_log").insert(payload).execute()
    inserted = result.data[0] if result.data else payload

    return {
        "status": "success",
        "message": "Export event logged",
        "log_entry": inserted,
    }
