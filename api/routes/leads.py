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
    email_sub_state:    Optional[str]  = Query(None, description="Truelist sub-state (e.g. is_role, accept_all, failed_mx_check)"),
    email_status:       Optional[str]  = Query(None, description="Truelist validity: valid|invalid|unknown|error|pending_batch"),
    min_stars:      Optional[float] = Query(None, description="Minimum Google rating (e.g. 0)"),
    max_stars:      Optional[float] = Query(None, description="Maximum Google rating (e.g. 3.5 for reputation targets)"),
    min_reviews:    Optional[int]   = Query(None, description="Minimum Google review count"),
    max_reviews:    Optional[int]   = Query(None, description="Maximum Google review count (e.g. 20 for low-review targets)"),
    has_facebook:   Optional[bool]  = Query(None, description="True = only leads with a Facebook URL; False = only without"),
    has_instagram:  Optional[bool]  = Query(None, description="True = only leads with an Instagram URL; False = only without"),
    has_linkedin:   Optional[bool]  = Query(None, description="True = only leads with a LinkedIn URL; False = only without"),
    has_website:       Optional[bool]  = Query(None, description="True = website IS NOT NULL; False = website IS NULL"),
    ssl:               Optional[bool]  = Query(None, description="True = website LIKE 'https://%'; False = website LIKE 'http://%'"),
    has_chatbot:       Optional[bool]  = Query(None, description="True = chatbot widget detected; False = none detected; None = not yet audited"),
    mobile_friendly:   Optional[bool]  = Query(None, description="True = viewport meta present; False = absent; None = not yet audited"),
    parked:            Optional[bool]  = Query(None, description="True = domain parked/broken; False = live; None = not yet audited"),
    has_schema_markup: Optional[bool]  = Query(None, description="True = JSON-LD or microdata schema found; False = none; None = not yet audited"),
    source:            Optional[str]   = Query(None, description="Filter by ingest source (e.g. local_scraper, csv)"),
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
        "google_stars, google_review_count, g_maps_claimed, "
        "facebook, instagram, twitter, linkedin, timezone, "
        "enrichment_stage, "
        "data_quality_score, email_state, email_sub_state, email_status, email_valid, is_active, created_at, "
        "has_chatbot, mobile_friendly, is_parked, has_schema_markup"
    ).eq("is_active", is_active)

    count_query = db.table("leads").select("id", count="estimated", head=True).eq("is_active", is_active)

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
    if email_sub_state:
        query = query.eq("email_sub_state", email_sub_state)
        count_query = count_query.eq("email_sub_state", email_sub_state)
    if email_status:
        query = query.eq("email_status", email_status)
        count_query = count_query.eq("email_status", email_status)
    if min_stars is not None:
        query = query.gte("google_stars", min_stars)
        count_query = count_query.gte("google_stars", min_stars)
    if max_stars is not None:
        query = query.lte("google_stars", max_stars)
        count_query = count_query.lte("google_stars", max_stars)
    if min_reviews is not None:
        query = query.gte("google_review_count", min_reviews)
        count_query = count_query.gte("google_review_count", min_reviews)
    if max_reviews is not None:
        query = query.lte("google_review_count", max_reviews)
        count_query = count_query.lte("google_review_count", max_reviews)
    if source:
        query = query.eq("source", source)
        count_query = count_query.eq("source", source)
    if has_facebook is not None:
        if has_facebook:
            query = query.not_.is_("facebook", "null")
            count_query = count_query.not_.is_("facebook", "null")
        else:
            query = query.is_("facebook", "null")
            count_query = count_query.is_("facebook", "null")
    if has_instagram is not None:
        if has_instagram:
            query = query.not_.is_("instagram", "null")
            count_query = count_query.not_.is_("instagram", "null")
        else:
            query = query.is_("instagram", "null")
            count_query = count_query.is_("instagram", "null")
    if has_linkedin is not None:
        if has_linkedin:
            query = query.not_.is_("linkedin", "null")
            count_query = count_query.not_.is_("linkedin", "null")
        else:
            query = query.is_("linkedin", "null")
            count_query = count_query.is_("linkedin", "null")
    if has_website is not None:
        if has_website:
            query = query.not_.is_("website", "null")
            count_query = count_query.not_.is_("website", "null")
        else:
            query = query.is_("website", "null")
            count_query = count_query.is_("website", "null")
    if ssl is not None:
        if ssl:
            query = query.like("website", "https://%")
            count_query = count_query.like("website", "https://%")
        else:
            query = query.like("website", "http://%")
            count_query = count_query.like("website", "http://%")
    if has_chatbot is not None:
        query = query.eq("has_chatbot", has_chatbot)
        count_query = count_query.eq("has_chatbot", has_chatbot)
    if mobile_friendly is not None:
        query = query.eq("mobile_friendly", mobile_friendly)
        count_query = count_query.eq("mobile_friendly", mobile_friendly)
    if parked is not None:
        query = query.eq("is_parked", parked)
        count_query = count_query.eq("is_parked", parked)
    if has_schema_markup is not None:
        query = query.eq("has_schema_markup", has_schema_markup)
        count_query = count_query.eq("has_schema_markup", has_schema_markup)

    # Get total count (estimated — avoids full-table scan timeout on 1.5M-row table)
    try:
        count_result = count_query.execute()
        total = count_result.count if count_result.count is not None else 0
    except Exception:
        total = 0

    # Ordering + pagination
    query = query.order(order_by, desc=(order_dir == "desc"), nullsfirst=False)
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
