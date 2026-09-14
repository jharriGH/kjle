"""
KJLE API — Contacts Routes
GET /kjle/v1/contacts                     — list contacts (paginated, filterable)
GET /kjle/v1/contacts/seniority-breakdown — decision-maker counts by seniority

Business intel filters (money filters for campaign pulls):
  has_business       — linked to a lead record (business_id IS NOT NULL)
  linked_no_chatbot  — linked business has no chatbot (BizReply filter)
  linked_low_a11y    — linked business accessibility_score < N (ComplianceMDs filter)

Response enrichment: each contact with a business_id gets linked_* fields populated
via a single batch lookup against leads (<=100 IDs per page, always fast).
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


# business_id added so we can batch-lookup linked lead intel
_SELECT_COLS = (
    "id, full_name, title, seniority, company, company_website, primary_email, "
    "niche_slug, city, state, email_provider, email_trust, email_status, "
    "has_chatbot, accessibility_score, website_word_count, linkedin_url, business_id"
)

# Fields fetched from leads for the linked_* response namespace
_BIZ_SELECT_COLS = "id, business_name, has_chatbot, accessibility_score, website, pain_score"


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
    # Business intel / money filters
    has_business:        Optional[bool] = Query(None, description="True = linked to a business (business_id IS NOT NULL); False = unlinked"),
    linked_no_chatbot:   Optional[bool] = Query(None, description="True = linked business has_chatbot=false. BizReply money filter. Uses idx_contacts_business_id + has_chatbot."),
    linked_low_a11y:     Optional[int]  = Query(None, description="Linked business accessibility_score < value. ComplianceMDs filter."),
    page:                int            = Query(1, ge=1),
    page_size:           int            = Query(50, ge=1, le=100),
):
    db = get_db()
    offset = (page - 1) * page_size

    # All filters applied via this closure — called separately for count and rows
    # so each gets a FRESH builder (mutable builders must never be reused after execute).
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
        # Business link filters — contacts.has_chatbot and accessibility_score are
        # denormalized from the linked lead during contact-link, so filtering here
        # is equivalent to a JOIN on leads and avoids a cross-table query.
        # Uses idx_contacts_business_id + idx_contacts_niche_seniority (fast).
        if has_business is not None:
            if has_business:
                q = q.not_.is_("business_id", "null")
            else:
                q = q.is_("business_id", "null")
        if linked_no_chatbot is not None:
            q = q.not_.is_("business_id", "null")
            # .is_() required for boolean columns — .eq('false') bug in PostgREST
            if linked_no_chatbot:
                q = q.is_("has_chatbot", "false")
            else:
                q = q.is_("has_chatbot", "true")
        if linked_low_a11y is not None:
            q = q.not_.is_("business_id", "null")
            q = q.lt("accessibility_score", linked_low_a11y)
        return q

    # Count query — fresh builder, range(0,0) transfers no rows
    try:
        count_result = apply_filters(
            db.table("contacts").select("id", count="exact")
        ).range(0, 0).execute()
        total = count_result.count if count_result.count is not None else 0
    except Exception as exc:
        _logger.warning("contacts count_query failed: %s -- total set to 0", exc)
        total = 0

    # Row query — fresh builder, never mutated after execute
    result = (
        apply_filters(db.table("contacts").select(_SELECT_COLS))
        .order("created_at", desc=True, nullsfirst=False)
        .range(offset, offset + page_size - 1)
        .execute()
    )

    # Batch-enrich with linked business intel.
    # Page is <=100 rows, so this IN() lookup is always tiny and fast.
    business_ids = [c["business_id"] for c in result.data if c.get("business_id")]
    biz_map: dict = {}
    if business_ids:
        try:
            biz_result = (
                db.table("leads")
                .select(_BIZ_SELECT_COLS)
                .in_("id", business_ids)
                .execute()
            )
            biz_map = {b["id"]: b for b in biz_result.data}
        except Exception as exc:
            _logger.warning("contacts biz_lookup failed: %s -- linked_* fields will be null", exc)

    contacts_out = []
    for c in result.data:
        biz = biz_map.get(c.get("business_id"))
        contacts_out.append({
            **c,
            "linked_business_name":       biz["business_name"]       if biz else None,
            "linked_has_chatbot":         biz["has_chatbot"]          if biz else None,
            "linked_accessibility_score": biz["accessibility_score"]  if biz else None,
            "linked_website":             biz["website"]              if biz else None,
            "linked_pain_score":          biz.get("pain_score")       if biz else None,
        })

    return {
        "page":      page,
        "page_size": page_size,
        "total":     total,
        "count":     len(contacts_out),
        "contacts":  contacts_out,
    }
