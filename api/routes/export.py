"""
Export Engine — CSV
KJLE - King James Lead Empire
Routes: /kjle/v1/export/*

Exports leads to CSV download.
Logs all exports to export_log table.
"""

import csv
import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..database import get_db

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

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class ExportLogRequest(BaseModel):
    export_type: str       # e.g. "csv"
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
# GET /export/history
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/export/history")
async def get_export_history(
    limit: int = Query(20, ge=1, le=200),
):
    """
    Returns recent export log entries sorted by created_at desc.
    Returns recent export log entries, sorted by created_at desc.
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
