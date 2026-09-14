"""
KJLE API — Contacts Routes
GET /kjle/v1/contacts                     — list contacts (paginated, filterable)
GET /kjle/v1/contacts/seniority-breakdown — decision-maker counts by seniority

Business intel filters (money filters for campaign pulls):
  has_business       — linked to a lead record (business_id IS NOT NULL)
  linked_no_chatbot  — linked business has no chatbot (BizReply filter)
  linked_low_a11y    — linked business accessibility_score < N (ComplianceMDs filter)

When linked_no_chatbot or linked_low_a11y is set, the query routes through
contacts_biz_count / contacts_biz_rows RPCs (JOIN contacts->leads on business_id).
contacts.has_chatbot is NULL (not denormalized), so the join is required.
Indexes: idx_contacts_business_id (contacts), idx_leads_has_chatbot (leads).

For unlinked or non-chatbot filters: direct contacts table query + in-memory
batch lookup of linked_* fields (<=100 IDs per page, always fast).
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


def _seniority_list(seniority: Optional[str]) -> Optional[list]:
    """Parse comma-separated seniority string to list, or None."""
    if not seniority:
        return None
    vals = [s.strip() for s in seniority.split(",") if s.strip()]
    return vals if vals else None


def _str_list(val: Optional[str]) -> Optional[list]:
    """Parse comma-separated string to list, or None."""
    if not val:
        return None
    vals = [s.strip() for s in val.split(",") if s.strip()]
    return vals if vals else None


def _enrich_with_biz(contacts: list, db) -> list:
    """Batch-lookup linked business intel and add linked_* fields.
    Page is <=100 rows so the IN() is always tiny.
    """
    business_ids = [c["business_id"] for c in contacts if c.get("business_id")]
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

    out = []
    for c in contacts:
        biz = biz_map.get(c.get("business_id"))
        out.append({
            **c,
            "linked_business_name":       biz["business_name"]      if biz else None,
            "linked_has_chatbot":         biz["has_chatbot"]         if biz else None,
            "linked_accessibility_score": biz["accessibility_score"] if biz else None,
            "linked_website":             biz["website"]             if biz else None,
            "linked_pain_score":          biz.get("pain_score")      if biz else None,
        })
    return out


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
    linked_no_chatbot:   Optional[bool] = Query(None, description="True = linked business has_chatbot=false. BizReply money filter. Routes through contacts_biz_count/contacts_biz_rows RPCs."),
    linked_low_a11y:     Optional[int]  = Query(None, description="Linked business accessibility_score < value. ComplianceMDs filter."),
    page:                int            = Query(1, ge=1),
    page_size:           int            = Query(50, ge=1, le=100),
):
    db = get_db()
    offset = (page - 1) * page_size

    # When linked filters require a JOIN to leads, route through RPCs.
    use_rpc = (linked_no_chatbot is not None or linked_low_a11y is not None)

    if use_rpc:
        return await _list_contacts_via_rpc(
            db, page, page_size, offset,
            niche_slug, seniority, title, company, has_company_website,
            email_provider, email_trust, email_status, state, min_word_count,
            linked_no_chatbot, linked_low_a11y,
        )

    # ── Standard path (no JOIN required) ────────────────────────────────────────

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
        if has_business is not None:
            if has_business:
                q = q.not_.is_("business_id", "null")
            else:
                q = q.is_("business_id", "null")
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

    contacts_out = _enrich_with_biz(result.data, db)

    return {
        "page":      page,
        "page_size": page_size,
        "total":     total,
        "count":     len(contacts_out),
        "contacts":  contacts_out,
    }


async def _list_contacts_via_rpc(
    db, page, page_size, offset,
    niche_slug, seniority, title, company, has_company_website,
    email_provider, email_trust, email_status, state, min_word_count,
    linked_no_chatbot, linked_low_a11y,
):
    """Handle linked_no_chatbot / linked_low_a11y via contacts_biz_count/rows RPCs.
    These RPCs do a JOIN contacts->leads on business_id using indexed has_chatbot.
    """
    seniority_arr  = _seniority_list(seniority)
    email_prov_arr = _str_list(email_provider)
    email_trust_arr = _str_list(email_trust)

    rpc_params = {
        "p_niche_slug":          niche_slug,
        "p_seniority":           seniority_arr,
        "p_title":               title,
        "p_company":             company,
        "p_has_company_website": has_company_website,
        "p_email_provider":      email_prov_arr,
        "p_email_trust":         email_trust_arr,
        "p_email_status":        email_status,
        "p_state":               state.upper() if state else None,
        "p_min_word_count":      min_word_count,
        "p_linked_no_chatbot":   linked_no_chatbot,
        "p_linked_low_a11y":     linked_low_a11y,
    }

    # Count via RPC — fresh call
    try:
        count_result = db.rpc("contacts_biz_count", rpc_params).execute()
        total = count_result.data if isinstance(count_result.data, int) else (
            count_result.data[0] if isinstance(count_result.data, list) and count_result.data else 0
        )
    except Exception as exc:
        _logger.warning("contacts_biz_count RPC failed: %s -- total set to 0", exc)
        total = 0

    # Rows via RPC
    rows_params = {
        **rpc_params,
        "p_limit":  page_size,
        "p_offset": offset,
    }
    try:
        rows_result = db.rpc("contacts_biz_rows", rows_params).execute()
        contacts_out = rows_result.data or []
    except Exception as exc:
        _logger.error("contacts_biz_rows RPC failed: %s", exc)
        raise HTTPException(status_code=500, detail="contacts_biz_rows RPC error") from exc

    return {
        "page":      page,
        "page_size": page_size,
        "total":     total,
        "count":     len(contacts_out),
        "contacts":  contacts_out,
    }
