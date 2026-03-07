"""
KJLE API — Saved Segments Routes
GET    /kjle/v1/segments           — list all saved segments
GET    /kjle/v1/segments/{id}      — get segment + lead count
POST   /kjle/v1/segments           — create new segment
DELETE /kjle/v1/segments/{id}      — delete segment
GET    /kjle/v1/segments/{id}/leads — run segment, return matching leads
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any
from ..database import get_db

router = APIRouter()


class SegmentCreate(BaseModel):
    name: str
    description: Optional[str] = None
    filters: Dict[str, Any]
    product_target: Optional[str] = None  # demoenginez|reputation|schema_ranker|voicedrop


@router.get("/segments")
async def list_segments():
    db = get_db()
    result = db.table("saved_segments").select("*").order("created_at", desc=True).execute()
    return {"segments": result.data}


@router.get("/segments/{segment_id}")
async def get_segment(segment_id: str):
    db = get_db()
    result = db.table("saved_segments").select("*").eq("id", segment_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Segment not found")
    return result.data[0]


@router.post("/segments")
async def create_segment(segment: SegmentCreate):
    db = get_db()
    result = db.table("saved_segments").insert({
        "name":           segment.name,
        "description":    segment.description,
        "filters":        segment.filters,
        "product_target": segment.product_target,
    }).execute()
    return result.data[0]


@router.delete("/segments/{segment_id}")
async def delete_segment(segment_id: str):
    db = get_db()
    db.table("saved_segments").delete().eq("id", segment_id).execute()
    return {"status": "deleted", "id": segment_id}


@router.get("/segments/{segment_id}/leads")
async def run_segment(
    segment_id: str,
    page: int = 1,
    page_size: int = 100,
):
    db = get_db()

    # Load segment filters
    seg = db.table("saved_segments").select("*").eq("id", segment_id).execute()
    if not seg.data:
        raise HTTPException(status_code=404, detail="Segment not found")

    filters = seg.data[0].get("filters", {})
    offset = (page - 1) * page_size

    # Build query from filters
    query = db.table("leads").select(
        "id, business_name, phone, email, website, city, state, niche_slug, "
        "pain_score, fit_demoenginez, fit_reputation, fit_schema_ranker, fit_voicedrop, "
        "google_stars, google_review_count, g_maps_claimed, enrichment_stage, email_state"
    ).eq("is_active", True)

    # Apply filters dynamically
    for field, value in filters.items():
        if isinstance(value, dict):
            if "gte" in value: query = query.gte(field, value["gte"])
            if "lte" in value: query = query.lte(field, value["lte"])
            if "eq"  in value: query = query.eq(field, value["eq"])
        else:
            query = query.eq(field, value)

    result = (
        query
        .order("pain_score", desc=True)
        .range(offset, offset + page_size - 1)
        .execute()
    )

    return {
        "segment_id":   segment_id,
        "segment_name": seg.data[0]["name"],
        "page":         page,
        "page_size":    page_size,
        "count":        len(result.data),
        "leads":        result.data,
    }
