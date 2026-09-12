"""
KJLE API — Contacts Routes
GET /kjle/v1/contacts                     — list contacts (paginated, filterable)
GET /kjle/v1/contacts/seniority-breakdown — decision-maker counts by seniority
"""
import logging
import os

from fastapi import APIRouter, Header, HTTPException, Query
from typing import Optional
from ..database import get_db

_logger = logging.getLogger(__name__)

router = APIRouter()

API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


_SELECT_COLS = (
    "id, full_name, title, seniority, company, company_website, primary_email, "
    "niche_slug, city, state, email_provider, email_trust, email_status, "
    "has_chatbot, accessibility_score, website_word_count, linkedin_url"
)


# seniority-breakdown MUST be defined before any future /{contact_id} route
@router.get("/contacts/seniority-breakdown")
async def contacts_seniority_breakdown(
    niche_slug:          Optional[str]  = Query(None),
    email_provider:      Optional[str]  = Query(None),
    email_trust:         Optional[str]  = Query(None),
    email_status:        Optional[str]  = Query(None),
    state:               Optional[str]  = Query(None),
    has_company_website: Optional[bool] = Query(None),
):
    db = get_db()

    def _apply(q):
        if niche_slug:
            q = q.eq("niche_slug", niche_slug)
        if email_provider:
            vals = [p.strip() for p in email_provider.split(",") if p.strip()]
            q = q.eq("email_provider", vals[0]) if len(vals) == 1 else q.in_("email_provider", vals)
        if email_trust:
            vals = [t.strip() for t in email_trust.split(",") if t.strip()]
            q = q.eq("email_trust", vals[0]) if len(vals) == 1 else q.in_("email_trust", vals)
        if email_status:
            q = q.eq("email_status", email_status)
        if state:
            q = q.eq("state", state.upper())
        if has_company_website is not None:
            if has_company_website:
                q = q.not_.is_("company_website", "null")
            else:
                q = q.is_("company_website", "null")
        return q

    levels = ["owner", "c_level", "vp_director", "manager", "other"]
    breakdown = {}
    for level in levels:
        try:
            res = _apply(
                db.table("contacts").select("id", count="exact").eq("seniority", level)
            ).range(0, 0).execute()
            breakdown[level] = res.count if res.count is not None else 0
        except Exception as exc:
            _logger.warning("seniority_breakdown[%s] failed: %s", level, exc)
            breakdown[level] = 0

    return breakdown


@router.get("/contacts")
async def list_contacts(
    niche_slug:          Optional[str]  = Query(None, description="Exact niche: hvac|solar|cleaning|landscaping|plumbing|roofing|pool|flooring|pest_control|general_contractor|realestate|electrical"),
    seniority:           Optional[str]  = Query(None, description="Single or comma-separated: owner|c_level|vp_director|manager|other"),
    title:               Optional[str]  = Query(None, description="ILIKE partial match on job title"),
    company:             Optional[str]  = Query(None, description="ILIKE partial match on company name"),
    has_company_website: Optional[bool] = Query(None, description="True = company_website IS NOT NULL; False = IS NULL/empty"),
    email_provider:      Optional[str]  = Query(None, description="Single or comma-separated: gmail_consumer|google_workspace|office365|ms_consumer|yahoo|apple|other|self_hosted|unknown"),
    email_trust:         Optional[str]  = Query(None, description="Single or comma-separated: valid|catch_all|role|unconfirmable|invalid"),
    email_status:        Optional[str]  = Query(None, description="valid|invalid|unknown"),
    state:               Optional[str]  = Query(None, description="Exact US state abbreviation"),
    min_word_count:      Optional[int]  = Query(None, description="Minimum website_word_count"),
    page:                int            = Query(1, ge=1),
    page_size:           int            = Query(50, ge=1, le=100),
):
    db = get_db()
    offset = (page - 1) * page_size

    # All filters applied via this closure — called separately for count and rows
    # so each gets a FRESH builder (mutable builders must never be reused after execute)
    def apply_filters(q):
        if niche_slug:
            q = q.eq("niche_slug", niche_slug)
        if seniority:
            vals = [s.strip() for s in seniority.split(",") if s.strip()]
            if len(vals) == 1:
                q = q.eq("seniority", vals[0])
            elif len(vals) > 1:
                q = q.in_("seniority", vals)
        if title:
            q = q.ilike("title", f"%{title}%")
        if company:
            q = q.ilike("company", f"%{company}%")
        if has_company_website is not None:
            if has_company_website:
                q = q.not_.is_("company_website", "null")
            else:
                q = q.is_("company_website", "null")
        if email_provider:
            vals = [p.strip() for p in email_provider.split(",") if p.strip()]
            if len(vals) == 1:
                q = q.eq("email_provider", vals[0])
            elif len(vals) > 1:
                q = q.in_("email_provider", vals)
        if email_trust:
            vals = [t.strip() for t in email_trust.split(",") if t.strip()]
            if len(vals) == 1:
                q = q.eq("email_trust", vals[0])
            elif len(vals) > 1:
                q = q.in_("email_trust", vals)
        if email_status:
            q = q.eq("email_status", email_status)
        if state:
            q = q.eq("state", state.upper())
        if min_word_count is not None:
            q = q.gte("website_word_count", min_word_count)
        return q

    # Count query — fresh builder, range(0,0) for count-only (no data transfer)
    try:
        count_result = apply_filters(
            db.table("contacts").select("id", count="exact")
        ).range(0, 0).execute()
        total = count_result.count if count_result.count is not None else 0
    except Exception as exc:
        _logger.warning("contacts count_query failed: %s — total set to 0", exc)
        total = 0

    # Row query — fresh builder, never mutated after execute
    result = (
        apply_filters(db.table("contacts").select(_SELECT_COLS))
        .order("created_at", desc=True, nullsfirst=False)
        .range(offset, offset + page_size - 1)
        .execute()
    )

    return {
        "page":      page,
        "page_size": page_size,
        "total":     total,
        "count":     len(result.data),
        "contacts":  result.data,
    }
