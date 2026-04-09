"""
KJLE API — Lead Routes
GET  /kjle/v1/leads              — list leads (paginated, filterable)
GET  /kjle/v1/leads/{id}         — single lead
GET  /kjle/v1/leads/search       — full-text search
PATCH /kjle/v1/leads/{id}        — update lead fields
DELETE /kjle/v1/leads/{id}       — soft delete (sets is_active=false)
"""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from ..database import get_db

router = APIRouter()


@router.get("/leads")
async def list_leads(
    niche_slug:     Optional[str]   = Query(None, description="Filter by niche slug"),
    state:          Optional[str]   = Query(None, description="Filter by US state (2-letter)"),
    city:           Optional[str]   = Query(None, description="Filter by city"),
    min_pain:       Optional[int]   = Query(None, description="Minimum pain score"),
    max_pain:       Optional[int]   = Query(None, description="Maximum pain score"),
    fit_demoenginez:    Optional[bool] = Query(None),
    fit_reputation:     Optional[bool] = Query(None),
    fit_schema_ranker:  Optional[bool] = Query(None),
    fit_voicedrop:      Optional[bool] = Query(None),
    enrichment_stage:   Optional[int]  = Query(None, description="0-4"),
    email_state:        Optional[str]  = Query(None, description="ok|risky"),
    is_active:          bool           = Query(True),
    page:           int             = Query(1, ge=1),
    page_size:      int             = Query(50, ge=1, le=500),
    order_by:       str             = Query("pain_score", description="Column to sort by"),
    order_dir:      str             = Query("desc", description="asc|desc"),
):
    db = get_db()
    offset = (page - 1) * page_size

    query = db.table("leads").select(
        "id, business_name, phone, email, website, city, state, niche_slug, "
        "pain_score, fit_demoenginez, fit_reputation, fit_schema_ranker, fit_voicedrop, "
        "google_stars, google_review_count, g_maps_claimed, enrichment_stage, "
        "data_quality_score, email_state, is_active, created_at"
    ).eq("is_active", is_active)

    count_query = db.table("leads").select("id", count="exact").eq("is_active", is_active)

    # Apply filters to both queries
    if niche_slug:
        query = query.eq("niche_slug", niche_slug)
        count_query = count_query.eq("niche_slug", niche_slug)
    if state:
        query = query.eq("state", state.upper())
        count_query = count_query.eq("state", state.upper())
    if city:
        query = query.ilike("city", f"%{city}%")
        count_query = count_query.ilike("city", f"%{city}%")
    if min_pain is not None:
        query = query.gte("pain_score", min_pain)
        count_query = count_query.gte("pain_score", min_pain)
    if max_pain is not None:
        query = query.lte("pain_score", max_pain)
        count_query = count_query.lte("pain_score", max_pain)
    if fit_demoenginez is not None:
        query = query.eq("fit_demoenginez", fit_demoenginez)
        count_query = count_query.eq("fit_demoenginez", fit_demoenginez)
    if fit_reputation is not None:
        query = query.eq("fit_reputation", fit_reputation)
        count_query = count_query.eq("fit_reputation", fit_reputation)
    if fit_schema_ranker is not None:
        query = query.eq("fit_schema_ranker", fit_schema_ranker)
        count_query = count_query.eq("fit_schema_ranker", fit_schema_ranker)
    if fit_voicedrop is not None:
        query = query.eq("fit_voicedrop", fit_voicedrop)
        count_query = count_query.eq("fit_voicedrop", fit_voicedrop)
    if enrichment_stage is not None:
        query = query.eq("enrichment_stage", enrichment_stage)
        count_query = count_query.eq("enrichment_stage", enrichment_stage)
    if email_state:
        query = query.eq("email_state", email_state)
        count_query = count_query.eq("email_state", email_state)

    # Get total count
    count_result = count_query.execute()
    total = count_result.count if count_result.count is not None else 0

    # Ordering + pagination
    query = query.order(order_by, desc=(order_dir == "desc"))
    query = query.range(offset, offset + page_size - 1)

    result = query.execute()

    return {
        "page":      page,
        "page_size": page_size,
        "total":     total,
        "count":     len(result.data),
        "leads":     result.data,
    }


@router.get("/leads/{lead_id}")
async def get_lead(lead_id: str):
    db = get_db()
    result = db.table("leads").select("*").eq("id", lead_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Lead not found")
    return result.data[0]


@router.get("/leads/search/q")
async def search_leads(
    q: str = Query(..., min_length=2, description="Search term"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    db = get_db()
    offset = (page - 1) * page_size
    result = (
        db.table("leads")
        .select("id, business_name, phone, email, website, city, state, niche_slug, pain_score")
        .or_(f"business_name.ilike.%{q}%,city.ilike.%{q}%,email.ilike.%{q}%,phone.ilike.%{q}%")
        .eq("is_active", True)
        .order("pain_score", desc=True)
        .range(offset, offset + page_size - 1)
        .execute()
    )
    return {
        "query":     q,
        "page":      page,
        "page_size": page_size,
        "count":     len(result.data),
        "leads":     result.data,
    }


@router.patch("/leads/{lead_id}")
async def update_lead(lead_id: str, updates: dict):
    db = get_db()
    # Prevent overwriting system fields
    protected = {"id", "fingerprint", "created_at", "pain_score_computed_at"}
    clean = {k: v for k, v in updates.items() if k not in protected}
    if not clean:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    result = db.table("leads").update(clean).eq("id", lead_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Lead not found")
    return result.data[0]


@router.delete("/leads/{lead_id}")
async def soft_delete_lead(lead_id: str):
    db = get_db()
    result = db.table("leads").update({"is_active": False}).eq("id", lead_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"status": "deleted", "id": lead_id}
