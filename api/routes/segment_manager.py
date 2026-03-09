"""
Segment Management API
KJLE - King James Lead Empire
Routes: /kjle/v1/segment-manager/*

Full CRUD for named, reusable lead segments with stored filter criteria,
live lead counts, and paginated lead retrieval.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class SegmentCreateRequest(BaseModel):
    name: str
    description: str = ""
    filters: dict = {}
    segment_label: str = "custom"       # hot | warm | cold | custom
    niche_slug: Optional[str] = None
    state: Optional[str] = None


class SegmentUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None        # active | archived


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_leads_query(db, filters: dict, niche_slug: Optional[str], state: Optional[str], segment_label: Optional[str]):
    """
    Builds a Supabase leads query from stored segment filters.
    Supports: niche_slug, state, segment_label, min_pain, enrichment_stage,
              fit_demoenginez, website_reachable, has_schema_markup.
    Returns the query object (not yet executed).
    """
    query = db.table("leads").select(
        "id, business_name, niche_slug, city, state, phone, email, website, "
        "pain_score, segment_label, enrichment_stage, fit_demoenginez, "
        "website_reachable, has_schema_markup, yelp_rating, "
        "outscraper_rating, outscraper_reviews_count, created_at"
    ).eq("is_active", True)

    # niche_slug: param takes precedence over filters dict
    effective_niche = niche_slug or filters.get("niche_slug")
    if effective_niche:
        query = query.eq("niche_slug", effective_niche)

    # state
    effective_state = state or filters.get("state")
    if effective_state:
        query = query.eq("state", effective_state.upper())

    # segment_label
    effective_label = segment_label or filters.get("segment_label")
    if effective_label and effective_label != "custom":
        query = query.eq("segment_label", effective_label)

    # min_pain
    min_pain = filters.get("min_pain")
    if min_pain is not None:
        query = query.gte("pain_score", int(min_pain))

    # max_pain
    max_pain = filters.get("max_pain")
    if max_pain is not None:
        query = query.lte("pain_score", int(max_pain))

    # enrichment_stage (exact)
    enrich_stage = filters.get("enrichment_stage")
    if enrich_stage is not None:
        query = query.eq("enrichment_stage", int(enrich_stage))

    # min_enrichment_stage
    min_stage = filters.get("min_enrichment_stage")
    if min_stage is not None:
        query = query.gte("enrichment_stage", int(min_stage))

    # fit_demoenginez
    fit = filters.get("fit_demoenginez")
    if fit is not None:
        query = query.eq("fit_demoenginez", bool(fit))

    # website_reachable
    reachable = filters.get("website_reachable")
    if reachable is not None:
        query = query.eq("website_reachable", bool(reachable))

    # has_schema_markup
    schema = filters.get("has_schema_markup")
    if schema is not None:
        query = query.eq("has_schema_markup", bool(schema))

    return query


def _count_matching_leads(db, filters: dict, niche_slug: Optional[str], state: Optional[str], segment_label: Optional[str]) -> int:
    """Returns the count of leads matching the segment's stored filters."""
    query = db.table("leads").select("id", count="exact").eq("is_active", True)

    effective_niche = niche_slug or filters.get("niche_slug")
    if effective_niche:
        query = query.eq("niche_slug", effective_niche)

    effective_state = state or filters.get("state")
    if effective_state:
        query = query.eq("state", effective_state.upper())

    effective_label = segment_label or filters.get("segment_label")
    if effective_label and effective_label != "custom":
        query = query.eq("segment_label", effective_label)

    min_pain = filters.get("min_pain")
    if min_pain is not None:
        query = query.gte("pain_score", int(min_pain))

    max_pain = filters.get("max_pain")
    if max_pain is not None:
        query = query.lte("pain_score", int(max_pain))

    enrich_stage = filters.get("enrichment_stage")
    if enrich_stage is not None:
        query = query.eq("enrichment_stage", int(enrich_stage))

    min_stage = filters.get("min_enrichment_stage")
    if min_stage is not None:
        query = query.gte("enrichment_stage", int(min_stage))

    fit = filters.get("fit_demoenginez")
    if fit is not None:
        query = query.eq("fit_demoenginez", bool(fit))

    reachable = filters.get("website_reachable")
    if reachable is not None:
        query = query.eq("website_reachable", bool(reachable))

    schema = filters.get("has_schema_markup")
    if schema is not None:
        query = query.eq("has_schema_markup", bool(schema))

    result = query.execute()
    return result.count if result.count is not None else 0


def _get_segment_or_404(db, segment_id: str) -> dict:
    """Fetches a segment by ID or raises 404."""
    result = (
        db.table("segments")
        .select("*")
        .eq("id", segment_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail=f"Segment '{segment_id}' not found")
    return result.data


# ─────────────────────────────────────────────────────────────────────────────
# POST /segment-manager/create
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/segment-manager/create")
async def create_segment(body: SegmentCreateRequest):
    """
    Creates a new named segment with stored filter criteria.
    Counts matching leads at creation time and stores in lead_count.
    """
    db = get_db()

    # Validate segment_label
    valid_labels = {"hot", "warm", "cold", "custom"}
    if body.segment_label not in valid_labels:
        raise HTTPException(
            status_code=422,
            detail=f"segment_label must be one of: {', '.join(sorted(valid_labels))}",
        )

    # Check for duplicate name
    existing = (
        db.table("segments")
        .select("id")
        .eq("name", body.name)
        .eq("status", "active")
        .execute()
        .data or []
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"An active segment named '{body.name}' already exists",
        )

    # Count matching leads
    lead_count = _count_matching_leads(
        db, body.filters, body.niche_slug, body.state, body.segment_label
    )

    now = _now_iso()
    payload = {
        "name":          body.name,
        "description":   body.description,
        "filters":       body.filters,
        "lead_count":    lead_count,
        "segment_label": body.segment_label,
        "niche_slug":    body.niche_slug,
        "state":         body.state.upper() if body.state else None,
        "status":        "active",
        "created_at":    now,
        "updated_at":    now,
    }

    result = db.table("segments").insert(payload).execute()
    created = result.data[0] if result.data else payload

    return {
        "status": "success",
        "message": f"Segment '{body.name}' created with {lead_count} matching leads",
        "segment": created,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /segment-manager/list
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/segment-manager/list")
async def list_segments(
    status: str = Query("active", description="Filter by status: active | archived"),
    segment_label: Optional[str] = Query(None, description="Filter by segment label"),
):
    """
    Lists all segments, sorted by created_at desc.
    Filters by status (default: active) and optionally by segment_label.
    """
    db = get_db()

    query = (
        db.table("segments")
        .select("*")
        .eq("status", status)
        .order("created_at", desc=True)
    )

    if segment_label:
        query = query.eq("segment_label", segment_label)

    result = query.execute()
    segments = result.data or []

    return {
        "status": "success",
        "filters": {"status": status, "segment_label": segment_label},
        "count": len(segments),
        "segments": segments,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /segment-manager/{segment_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/segment-manager/{segment_id}")
async def get_segment(
    segment_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """
    Returns segment metadata plus paginated leads matching its stored filters.
    """
    db = get_db()
    segment = _get_segment_or_404(db, segment_id)

    filters       = segment.get("filters") or {}
    niche_slug    = segment.get("niche_slug")
    state         = segment.get("state")
    segment_label = segment.get("segment_label")

    # Count total matching leads for pagination
    total = _count_matching_leads(db, filters, niche_slug, state, segment_label)
    total_pages = max(1, -(-total // page_size))
    offset = (page - 1) * page_size

    leads_query = _build_leads_query(db, filters, niche_slug, state, segment_label)
    leads = (
        leads_query
        .order("pain_score", desc=True)
        .range(offset, offset + page_size - 1)
        .execute()
        .data or []
    )

    return {
        "status": "success",
        "segment": segment,
        "pagination": {
            "page":        page,
            "page_size":   page_size,
            "total":       total,
            "total_pages": total_pages,
            "has_next":    page < total_pages,
            "has_prev":    page > 1,
        },
        "leads": leads,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /segment-manager/{segment_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/segment-manager/{segment_id}")
async def update_segment(segment_id: str, body: SegmentUpdateRequest):
    """
    Updates segment name, description, and/or status.
    Also recounts leads and refreshes lead_count and updated_at.
    """
    db = get_db()
    segment = _get_segment_or_404(db, segment_id)

    if body.status and body.status not in ("active", "archived"):
        raise HTTPException(
            status_code=422,
            detail="status must be 'active' or 'archived'",
        )

    # Check for duplicate name if renaming
    if body.name and body.name != segment.get("name"):
        duplicate = (
            db.table("segments")
            .select("id")
            .eq("name", body.name)
            .eq("status", "active")
            .neq("id", segment_id)
            .execute()
            .data or []
        )
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail=f"An active segment named '{body.name}' already exists",
            )

    # Recount leads using stored filters
    filters       = segment.get("filters") or {}
    niche_slug    = segment.get("niche_slug")
    state         = segment.get("state")
    segment_label = segment.get("segment_label")
    fresh_count   = _count_matching_leads(db, filters, niche_slug, state, segment_label)

    update_payload: dict = {
        "lead_count": fresh_count,
        "updated_at": _now_iso(),
    }
    if body.name is not None:
        update_payload["name"] = body.name
    if body.description is not None:
        update_payload["description"] = body.description
    if body.status is not None:
        update_payload["status"] = body.status

    db.table("segments").update(update_payload).eq("id", segment_id).execute()

    # Return fresh record
    updated = db.table("segments").select("*").eq("id", segment_id).single().execute().data

    return {
        "status": "success",
        "message": f"Segment updated. Current lead count: {fresh_count}",
        "segment": updated,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /segment-manager/{segment_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/segment-manager/{segment_id}")
async def delete_segment(segment_id: str):
    """
    Soft-deletes a segment by setting status='archived'.
    The segment and its stored filters are preserved for audit purposes.
    """
    db = get_db()
    segment = _get_segment_or_404(db, segment_id)

    if segment.get("status") == "archived":
        return {
            "status": "success",
            "message": f"Segment '{segment.get('name')}' was already archived",
            "segment_id": segment_id,
        }

    db.table("segments").update({
        "status":     "archived",
        "updated_at": _now_iso(),
    }).eq("id", segment_id).execute()

    return {
        "status": "success",
        "message": f"Segment '{segment.get('name')}' has been archived (soft delete)",
        "segment_id": segment_id,
        "name": segment.get("name"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /segment-manager/{segment_id}/refresh
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/segment-manager/{segment_id}/refresh")
async def refresh_segment(segment_id: str):
    """
    Recounts leads matching the segment's stored filters and updates
    lead_count and updated_at. Use after running /classify or ingesting new leads.
    """
    db = get_db()
    segment = _get_segment_or_404(db, segment_id)

    filters       = segment.get("filters") or {}
    niche_slug    = segment.get("niche_slug")
    state         = segment.get("state")
    segment_label = segment.get("segment_label")

    previous_count = segment.get("lead_count", 0)
    fresh_count    = _count_matching_leads(db, filters, niche_slug, state, segment_label)
    delta          = fresh_count - (previous_count or 0)

    db.table("segments").update({
        "lead_count": fresh_count,
        "updated_at": _now_iso(),
    }).eq("id", segment_id).execute()

    return {
        "status": "success",
        "segment_id":      segment_id,
        "name":            segment.get("name"),
        "previous_count":  previous_count,
        "current_count":   fresh_count,
        "delta":           delta,
        "refreshed_at":    _now_iso(),
    }
