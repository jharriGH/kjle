"""
KJLE — Prompt 29: Segment Builder Routes
File: api/routes/segment_builder.py
"""

import os
import logging
from typing import Optional, List, Any, Dict
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from supabase import create_client, Client

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")

router = APIRouter()


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


# ─────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────

class SegmentFilters(BaseModel):
    niche_slug: Optional[str] = None
    segment_label: Optional[str] = None        # hot | warm | cold | unclassified
    email_status: Optional[str] = None         # valid | invalid | unknown | error
    has_phone: Optional[bool] = None
    has_email: Optional[bool] = None
    min_pain_score: Optional[float] = None
    max_pain_score: Optional[float] = None
    state: Optional[str] = None
    city: Optional[str] = None
    enrichment_stage: Optional[int] = None


class SegmentCreate(BaseModel):
    name: str
    description: Optional[str] = None
    filters: SegmentFilters


class SegmentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    filters: Optional[SegmentFilters] = None


class SegmentPreviewRequest(BaseModel):
    filters: SegmentFilters
    sample_size: Optional[int] = 10  # number of sample leads to return


# ─────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────

def apply_filters(query, filters: SegmentFilters):
    """Apply SegmentFilters to a Supabase query and return the modified query."""
    if filters.niche_slug:
        query = query.eq("niche_slug", filters.niche_slug)
    if filters.segment_label:
        if filters.segment_label == "unclassified":
            query = query.is_("segment_label", "null")
        else:
            query = query.eq("segment_label", filters.segment_label)
    if filters.email_status:
        query = query.eq("email_status", filters.email_status)
    if filters.has_phone is True:
        query = query.neq("phone", None)
    elif filters.has_phone is False:
        query = query.is_("phone", "null")
    if filters.has_email is True:
        query = query.neq("email", None)
    elif filters.has_email is False:
        query = query.is_("email", "null")
    if filters.min_pain_score is not None:
        query = query.gte("pain_score", filters.min_pain_score)
    if filters.max_pain_score is not None:
        query = query.lte("pain_score", filters.max_pain_score)
    if filters.state:
        query = query.eq("state", filters.state)
    if filters.city:
        query = query.ilike("city", f"%{filters.city}%")
    if filters.enrichment_stage is not None:
        query = query.eq("enrichment_stage", filters.enrichment_stage)
    return query


# ─────────────────────────────────────────────────
# GET /kjle/v1/segments/saved — List all saved segments
# ─────────────────────────────────────────────────

@router.get("/segments/saved")
async def list_saved_segments(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        result = supabase.table("saved_segments").select("*").order("created_at", desc=True).execute()
        return {
            "total": len(result.data or []),
            "segments": result.data or [],
        }
    except Exception as e:
        logger.error(f"list_saved_segments error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/segments/preview — Live filter preview (no save)
# Registered BEFORE /{segment_id} to avoid route shadowing
# ─────────────────────────────────────────────────

@router.post("/segments/preview")
async def preview_segment(payload: SegmentPreviewRequest, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    sample_size = max(1, min(payload.sample_size or 10, 50))

    try:
        # Count query
        count_query = supabase.table("leads").select("id", count="exact")
        count_query = apply_filters(count_query, payload.filters)
        count_result = count_query.execute()
        total = count_result.count or 0

        # Sample query
        sample_query = supabase.table("leads").select(
            "id, business_name, email, phone, city, state, niche_slug, pain_score, segment_label, email_status"
        )
        sample_query = apply_filters(sample_query, payload.filters)
        sample_query = sample_query.order("pain_score", desc=True).limit(sample_size)
        sample_result = sample_query.execute()

        return {
            "total_matches": total,
            "sample_size": sample_size,
            "sample": sample_result.data or [],
            "filters_applied": payload.filters.dict(exclude_none=True),
        }

    except Exception as e:
        logger.error(f"preview_segment error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/segments/saved — Create a named segment
# ─────────────────────────────────────────────────

@router.post("/segments/saved")
async def create_saved_segment(payload: SegmentCreate, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "name": payload.name,
            "description": payload.description,
            "filters": payload.filters.dict(exclude_none=True),
            "created_at": now,
            "updated_at": now,
        }
        result = supabase.table("saved_segments").insert(record).execute()
        if not result.data:
            raise HTTPException(status_code=500, detail="Insert returned no data")
        return result.data[0]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create_saved_segment error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# GET /kjle/v1/segments/saved/{segment_id}
# ─────────────────────────────────────────────────

@router.get("/segments/saved/{segment_id}")
async def get_saved_segment(segment_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        result = supabase.table("saved_segments").select("*").eq("id", segment_id).single().execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Segment not found")
        return result.data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"get_saved_segment error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# PUT /kjle/v1/segments/saved/{segment_id}
# ─────────────────────────────────────────────────

@router.put("/segments/saved/{segment_id}")
async def update_saved_segment(segment_id: str, payload: SegmentUpdate, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        existing = supabase.table("saved_segments").select("*").eq("id", segment_id).single().execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Segment not found")

        updates: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
        if payload.name is not None:
            updates["name"] = payload.name
        if payload.description is not None:
            updates["description"] = payload.description
        if payload.filters is not None:
            updates["filters"] = payload.filters.dict(exclude_none=True)

        result = supabase.table("saved_segments").update(updates).eq("id", segment_id).execute()
        if not result.data:
            raise HTTPException(status_code=500, detail="Update returned no data")
        return result.data[0]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"update_saved_segment error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# DELETE /kjle/v1/segments/saved/{segment_id}
# ─────────────────────────────────────────────────

@router.delete("/segments/saved/{segment_id}")
async def delete_saved_segment(segment_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        existing = supabase.table("saved_segments").select("id").eq("id", segment_id).single().execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Segment not found")

        supabase.table("saved_segments").delete().eq("id", segment_id).execute()
        return {"deleted": True, "segment_id": segment_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete_saved_segment error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/segments/saved/{segment_id}/execute
# Run a saved segment — returns all matching lead IDs + summary
# ─────────────────────────────────────────────────

@router.post("/segments/saved/{segment_id}/execute")
async def execute_saved_segment(segment_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        seg_result = supabase.table("saved_segments").select("*").eq("id", segment_id).single().execute()
        if not seg_result.data:
            raise HTTPException(status_code=404, detail="Segment not found")

        segment = seg_result.data
        raw_filters = segment.get("filters") or {}
        filters = SegmentFilters(**raw_filters)

        # Count
        count_query = supabase.table("leads").select("id", count="exact")
        count_query = apply_filters(count_query, filters)
        count_result = count_query.execute()
        total = count_result.count or 0

        # Fetch all matching lead IDs
        leads_query = supabase.table("leads").select(
            "id, business_name, email, phone, city, state, niche_slug, pain_score, segment_label, email_status"
        )
        leads_query = apply_filters(leads_query, filters)
        leads_query = leads_query.order("pain_score", desc=True)

        # Paginate through all results in batches of 1000
        all_leads = []
        batch_size = 1000
        offset = 0
        while True:
            batch = leads_query.range(offset, offset + batch_size - 1).execute()
            if not batch.data:
                break
            all_leads.extend(batch.data)
            if len(batch.data) < batch_size:
                break
            offset += batch_size

        # Update last_executed_at on the segment
        supabase.table("saved_segments").update({
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", segment_id).execute()

        return {
            "segment_id": segment_id,
            "segment_name": segment.get("name"),
            "total_matches": total,
            "leads": all_leads,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"execute_saved_segment error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
