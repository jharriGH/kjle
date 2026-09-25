"""
KJLE API — Contacts Routes
GET /kjle/v1/contacts                     — list contacts (paginated, filterable)
GET /kjle/v1/contacts/seniority-breakdown — decision-maker counts by seniority

Business intel filters (money filters for campaign pulls):
  has_business       — linked to a lead record (business_id IS NOT NULL)
  linked_no_chatbot  — linked_has_chatbot = false  (denormalized column on contacts)
  linked_low_a11y    — linked_a11y_score < N       (denormalized column on contacts)

Money filters hit idx_contacts_money (niche_slug, seniority, linked_has_chatbot) directly —
no RPC, no JOIN. Requires niche_slug (returns 400 otherwise) to stay on that index.
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


# business_id included so we can batch-lookup linked lead intel
_SELECT_COLS = (
    "id, full_name, title, seniority, company, company_website, primary_email, "
    "niche_slug, city, state, email_provider, email_trust, email_status, "
    "has_chatbot, accessibility_score, website_word_count, linkedin_url, business_id"
)

# Fields fetched from leads for the linked_* response namespace (batch IN ≤100 rows)
_BIZ_SELECT_COLS = "id, business_name, has_chatbot, accessibility_score, website, pain_score"


def _seniority_list(seniority: Optional[str]) -> Optional[list]:
    if not seniority:
        return None
    vals = [s.strip() for s in seniority.split(",") if s.strip()]
    return vals if vals else None


def _str_list(val: Optional[str]) -> Optional[list]:
    if not val:
        return None
    vals = [s.strip() for s in val.split(",") if s.strip()]
    return vals if vals else None


def _stats_cache_read(db, key: str) -> dict:
    """Read one stats_cache row. Returns {} on miss or error — never raises,
    never triggers a live fallback scan. Populated by refresh_stats_cache()
    (pg_cron, every 30 min)."""
    try:
        res = (
            db.table("stats_cache")
            .select("data,refreshed_at")
            .eq("key", key)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]
    except Exception as exc:
        _logger.warning("stats_cache read failed for key=%s: %s", key, exc)
    return {}


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
            "linked_website":             biz.get("website")         if biz else None,
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
                db.table("contacts").select("id", count="estimated").eq("seniority", level)
            ).range(0, 0).execute()
            breakdown[level] = res.count if res.count is not None else 0
        except Exception as exc:
            _logger.warning("seniority_breakdown[%s] failed: %s", level, exc)
            breakdown[level] = 0

    return breakdown


@router.get("/contacts/category-breakdown")
async def contacts_category_breakdown():
    """Cached email_status / email_trust / email_provider breakdowns across all
    contacts, plus an email_cleaned_at recent-count. Served entirely from
    stats_cache (refreshed by refresh_stats_cache(), pg_cron every 30 min) —
    never a live GROUP BY / full-table scan. Missing or not-yet-seeded keys
    return empty — the endpoint never falls back to a live query.
    """
    db = get_db()

    def _data(key: str) -> dict:
        return _stats_cache_read(db, key).get("data") or {}

    return {
        "email_status":   _data("contacts_email_status"),
        "email_trust":    _data("contacts_email_trust"),
        "email_provider": _data("contacts_email_provider"),
        "cleaned_recent": _data("contacts_cleaned_recent") or {
            "cleaned_total": 0, "cleaned_last_24h": 0, "cleaned_last_7d": 0,
        },
    }


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
    linked_no_chatbot:   Optional[bool] = Query(None, description="True = linked business has no chatbot (linked_has_chatbot = false). Requires niche_slug."),
    linked_low_a11y:     Optional[int]  = Query(None, description="linked_a11y_score < value. Requires niche_slug."),
    page:                int            = Query(1, ge=1),
    page_size:           int            = Query(50, ge=1, le=100),
):
    db = get_db()
    offset = (page - 1) * page_size

    # Guard: money filters without niche_slug risk a full-table scan off the index
    use_money_filter = (linked_no_chatbot is not None or linked_low_a11y is not None)
    if use_money_filter and not niche_slug:
        raise HTTPException(
            status_code=400,
            detail="linked_no_chatbot and linked_low_a11y require niche_slug (needed for idx_contacts_money index)",
        )

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
        # Money filters — direct column access, no RPC/JOIN needed
        if linked_no_chatbot is not None:
            if linked_no_chatbot:
                q = q.is_("linked_has_chatbot", "false")
            else:
                q = q.is_("linked_has_chatbot", "true")
        if linked_low_a11y is not None:
            q = q.lt("linked_a11y_score", linked_low_a11y)
        return q

    # Count query — fresh builder, range(0,0) transfers no rows.
    # "estimated" uses the query planner's row estimate instead of a live
    # COUNT(*) scan — same fix already applied to GET /leads for this table size.
    try:
        count_result = apply_filters(
            db.table("contacts").select("id", count="estimated")
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
