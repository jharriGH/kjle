"""
KJLE — Website Quality Audit
File: api/routes/website_audit.py

Derives signals from page HTML via two paths:
  Firecrawl path ($0.005/scrape): _fetch_audit_signals -> _parse_signals (4 signals)
  Free path (httpx, $0.00):       _fetch_html_free -> _parse_signals_full (~22 signals)

AUDIT-ONLY hard constraints:
  - Firecrawl batch writes ONLY: has_chatbot, mobile_friendly, has_schema_markup, is_parked
  - Free batch writes all signals in _FULL_AUDIT_COLUMNS + last_audited_at
  - NEVER writes: enrichment_stage, pain_score, fit_*, firecrawl_*
  - NEVER calls or schedules enrichment Stage 1/3/4 endpoints
  - Firecrawl format = ["rawHtml"] only -- no markdown, no extract

Cost: $0.005 / scrape (Firecrawl). confirm=true required to spend money.
Daily cap enforced per-lead via cost_guard.check_budget.
Free path: $0.00 -- no cost_guard, no confirm required.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db
from ..config import settings
from ..lib import cost_guard

logger = logging.getLogger(__name__)
router = APIRouter()

FIRECRAWL_URL      = "https://api.firecrawl.dev/v1/scrape"
COST_PER_SCRAPE    = 0.005
HTTP_TIMEOUT       = 30.0
MAX_BATCH          = 500
FREE_FETCH_TIMEOUT = 15.0

_FREE_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Chatbot widget signatures ─────────────────────────────────────────────────
# Matched case-insensitively against raw page HTML.
_CHATBOT_SIGS = [
    "intercom",    "drift.com",     "tawk.to",       "crisp.chat",
    "livechatinc", "zendesk",       "tidio",          "olark",
    "jivochat",    "smartsupp",     "hubspot",        "__lc",
    "liveagent",   "chatra",        "freshdesk",      "helpscout",
    "kayako",      "userlike",      "liveperson",      "gorgias",
]

# ── Parked / broken domain signatures ────────────────────────────────────────
_PARKED_SIGS = [
    "domain for sale",       "this domain is for sale",
    "buy this domain",       "domain may be for sale",
    "sedoparking",           "godaddy.com/parking",
    "afternic",              "hugedomains",
    "parked domain",         "this site is parked",
    "domain is parked",      "under construction",
    "underconstruction",     "coming soon",
]

# Pages with fewer stripped bytes than this are treated as parked/empty.
_PARKED_MIN_LEN = 500

# ── Cookie consent platform signatures ───────────────────────────────────────
_COOKIE_CONSENT_SIGS = [
    "onetrust", "cookiebot", "cookieconsent", "cookie-consent",
    "cookieyes", "termly", "iubenda", "osano", "usercentrics",
]

# ── Booking platform signatures ───────────────────────────────────────────────
_BOOKING_SIGS = [
    "calendly", "acuityscheduling", "squareup.com/appointments",
    "setmore", "booksy", "schedulicity", "housecall", "servicetitan",
]

# ── Outdated / deprecated tech markers ───────────────────────────────────────
_OUTDATED_MARKERS = [
    "<marquee", "<center", "<font", "<frameset",
    "shockwave", "classid=", "codebase=", "swfobject", ".swf\"",
]


# ── Signal parsers (pure -- no I/O) ──────────────────────────────────────────

def _detect_chatbot(html: str) -> bool:
    lower = html.lower()
    return any(sig in lower for sig in _CHATBOT_SIGS)


def _detect_mobile(html: str) -> bool:
    return bool(re.search(
        r'<meta[^>]+name=["\']viewport["\']',
        html,
        re.IGNORECASE,
    ))


def _detect_schema(html: str) -> bool:
    if re.search(
        r'<script[^>]+type=["\']application/ld\+json["\']',
        html,
        re.IGNORECASE,
    ):
        return True
    return bool(re.search(
        r'itemtype=["\']https?://schema\.org/',
        html,
        re.IGNORECASE,
    ))


def _detect_parked(html: str) -> bool:
    if len(html.strip()) < _PARKED_MIN_LEN:
        return True
    lower = html.lower()
    return any(sig in lower for sig in _PARKED_SIGS)


def _parse_signals(html: str) -> dict:
    """All four audit signals from raw HTML. Pure -- no I/O, no side-effects."""
    return {
        "has_chatbot":       _detect_chatbot(html),
        "mobile_friendly":   _detect_mobile(html),
        "has_schema_markup": _detect_schema(html),
        "is_parked":         _detect_parked(html),
    }


# ── Firecrawl caller -- rawHtml only ─────────────────────────────────────────

async def _fetch_audit_signals(website: str, lead_id: str) -> Optional[dict]:
    """
    Fetch rawHtml via Firecrawl, parse signals, log cost.
    Returns None when the budget guard blocks the call (no charge incurred).
    Returns signals dict on both success and fetch-failure cases:
      - Success: all four booleans derived from real HTML
      - Fetch failure / empty: is_parked=True, others=None
    Firecrawl bills per attempt, so cost is logged on any non-blocked call.
    """
    api_key = settings.FIRECRAWL_API_KEY
    if not api_key:
        logger.warning("[website_audit] FIRECRAWL_API_KEY not set -- skipping")
        return None

    allowed = await cost_guard.check_budget(
        service="website_audit",
        estimated_cost_usd=COST_PER_SCRAPE,
        job_name="website_audit_batch",
        leads_affected=1,
    )
    if not allowed:
        return None  # budget cap hit -- caller records as skipped

    url = website if website.startswith(("http://", "https://")) else f"https://{website}"

    signals: dict
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(
                FIRECRAWL_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json={"url": url, "formats": ["rawHtml"]},
            )
            resp.raise_for_status()
            data = resp.json()

        if not data.get("success"):
            logger.warning(f"[website_audit] Firecrawl success=false for {url}")
            signals = {
                "has_chatbot": None, "mobile_friendly": None,
                "has_schema_markup": None, "is_parked": True,
            }
        else:
            html = (data.get("data") or {}).get("rawHtml") or ""
            signals = _parse_signals(html) if html else {
                "has_chatbot": None, "mobile_friendly": None,
                "has_schema_markup": None, "is_parked": True,
            }

    except httpx.TimeoutException:
        logger.warning(f"[website_audit] timeout for {url}")
        signals = {
            "has_chatbot": None, "mobile_friendly": None,
            "has_schema_markup": None, "is_parked": True,
        }
    except httpx.HTTPStatusError as e:
        logger.warning(f"[website_audit] HTTP {e.response.status_code} for {url}")
        signals = {
            "has_chatbot": None, "mobile_friendly": None,
            "has_schema_markup": None, "is_parked": True,
        }
    except Exception as e:
        logger.warning(f"[website_audit] unexpected error for {url}: {type(e).__name__}: {e}")
        signals = {
            "has_chatbot": None, "mobile_friendly": None,
            "has_schema_markup": None, "is_parked": True,
        }

    # Log cost -- Firecrawl charges per attempt, not per success.
    await cost_guard.log_cost(
        stage="website_audit",
        service="website_audit",
        cost_usd=COST_PER_SCRAPE,
        lead_id=lead_id,
        items_fetched=1,
        metadata={"url": url},
    )
    return signals


# ── Free HTTP fetch (no Firecrawl, no cost) ───────────────────────────────────

async def _fetch_html_free(website: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetch raw HTML via httpx with a real browser UA. FREE -- no Firecrawl, no cost_guard.
    Returns (html, final_url) on success, (None, None) on any failure or non-200.
    final_url is the resolved URL after redirects; used for SSL detection.
    Returns a tuple rather than Optional[str] to expose the post-redirect URL.
    """
    url = website if website.startswith(("http://", "https://")) else f"https://{website}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=15.0, write=10.0, pool=5.0),
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, headers=_FREE_FETCH_HEADERS)
            if resp.status_code != 200:
                return None, None
            return resp.text, str(resp.url)
    except Exception as e:
        logger.debug(
            f"[website_audit_free] fetch failed for {url}: {type(e).__name__}: {e}"
        )
        return None, None


# ── New signal detectors (pure -- no I/O) ────────────────────────────────────

def _detect_ssl(url_final: str) -> bool:
    return url_final.startswith("https://")


def _detect_contact_form(html: str) -> bool:
    lower = html.lower()
    if "<form" in lower:
        return True
    if "mailto:" in lower:
        return True
    return bool(re.search(r'<a[^>]+>[^<]*contact[^<]*</a>', html, re.IGNORECASE))


def _detect_cta(html: str) -> bool:
    lower = html.lower()
    if "tel:" in lower:
        return True
    return bool(re.search(
        r'call\s+now|get\s+a\s+quote|book\s+now|schedule|request\s+appointment',
        lower,
    ))


def _detect_privacy_policy(html: str) -> bool:
    lower = html.lower()
    if "privacy policy" in lower:
        return True
    return bool(re.search(r'href=["\'][^"\']*privacy[^"\']*["\']', lower))


def _detect_terms(html: str) -> bool:
    lower = html.lower()
    if any(t in lower for t in ["terms of service", "terms & conditions", "terms and conditions"]):
        return True
    return bool(re.search(r'href=["\'][^"\']*terms[^"\']*["\']', lower))


def _detect_cookie_consent(html: str) -> bool:
    lower = html.lower()
    return any(sig in lower for sig in _COOKIE_CONSENT_SIGS)


def _detect_outdated_tech(html: str) -> bool:
    lower = html.lower()
    if any(m in lower for m in _OUTDATED_MARKERS):
        return True
    # Table-layout heuristic: >= 8 <table tags suggests table-based layout
    return lower.count("<table") >= 8


def _detect_video(html: str) -> bool:
    lower = html.lower()
    return any(sig in lower for sig in ["<video", "youtube.com/embed", "player.vimeo", "wistia"])


def _detect_blog(html: str) -> bool:
    lower = html.lower()
    return "/blog" in lower or bool(re.search(r'>blog<', lower))


def _detect_testimonials(html: str) -> bool:
    lower = html.lower()
    return any(t in lower for t in ["testimonial", "reviews", "what our clients say"])


def _detect_booking(html: str) -> bool:
    lower = html.lower()
    return any(sig in lower for sig in _BOOKING_SIGS)


def _count_img_alt_missing(html: str) -> int:
    imgs = re.findall(r'<img\b[^>]*>', html, re.IGNORECASE)
    return sum(1 for tag in imgs if not re.search(r'\balt\s*=', tag, re.IGNORECASE))


def _detect_missing_lang(html: str) -> bool:
    m = re.search(r'<html\b([^>]*?)>', html, re.IGNORECASE)
    if not m:
        return False  # no <html> tag -- conservative: don't flag
    return not re.search(r'\blang\s*=', m.group(1), re.IGNORECASE)


def _detect_skip_link(html: str) -> bool:
    lower = html.lower()
    return "skip to content" in lower or "skip to main" in lower


def _detect_sitemap_hint(html: str) -> bool:
    return "sitemap.xml" in html.lower()


def _detect_meta_desc(html: str) -> bool:
    return bool(re.search(
        r'<meta[^>]+name=["\']description["\'][^>]*content='
        r'|<meta[^>]+content=[^>]+name=["\']description["\']',
        html,
        re.IGNORECASE,
    ))


def _detect_meta_title(html: str) -> bool:
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    return bool(m and m.group(1).strip())


def _count_h1(html: str) -> int:
    return len(re.findall(r'<h1[\s>]', html, re.IGNORECASE))


def _detect_noindex(html: str) -> bool:
    m = re.search(
        r'<meta[^>]+name=["\']robots["\'][^>]*content=["\']([^"\']*)["\']'
        r'|<meta[^>]+content=["\']([^"\']*)["\'][^>]*name=["\']robots["\']',
        html,
        re.IGNORECASE,
    )
    if not m:
        return False
    content = (m.group(1) or m.group(2) or "").lower()
    return "noindex" in content


def _word_count(html: str) -> int:
    stripped = re.sub(r'<[^>]+>', ' ', html)
    stripped = re.sub(r'&[a-zA-Z]+;|&#\d+;', ' ', stripped)
    return len(stripped.split())


def _detect_phone_on_page(html: str) -> bool:
    if "tel:" in html.lower():
        return True
    return bool(re.search(r'\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}', html))


def _detect_address_on_page(html: str) -> bool:
    return bool(re.search(
        r'\b\d{1,5}\s+[a-z]{2,}\s+'
        r'(?:st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|ln|lane|way|court|ct|pl|place)\b',
        html.lower(),
    ))


# ── Full signal aggregator (free-path) ────────────────────────────────────────

def _parse_signals_full(html: str, final_url: str) -> dict:
    """
    All ~22 signals from raw HTML + resolved URL. Maps to existing leads columns
    plus new columns added by websignalz_p1_columns.sql.
    Omitted (column uncertain): website_meta_title.
    Omitted (insufficient signal): website_is_franchise.
    Omitted (not in HTML): website_has_robots.
    Pure -- no I/O, no side-effects.
    """
    return {
        # Original 4 signals (shared with Firecrawl path)
        "has_chatbot":                _detect_chatbot(html),
        "mobile_friendly":            _detect_mobile(html),
        "has_schema_markup":          _detect_schema(html),
        "is_parked":                  _detect_parked(html),

        # Existing leads columns
        "website_has_ssl":            _detect_ssl(final_url),
        "website_has_contact_form":   _detect_contact_form(html),
        "website_has_cta":            _detect_cta(html),
        "website_has_video":          _detect_video(html),
        "website_has_blog":           _detect_blog(html),
        "website_has_testimonials":   _detect_testimonials(html),
        "website_has_booking":        _detect_booking(html),
        "website_has_sitemap":        _detect_sitemap_hint(html),
        "website_meta_desc":          _detect_meta_desc(html),
        "website_h1_count":           _count_h1(html),
        "website_noindex":            _detect_noindex(html),
        "website_word_count":         _word_count(html),
        "website_img_alt_missing":    _count_img_alt_missing(html),
        "has_phone_on_page":          _detect_phone_on_page(html),
        "has_address_on_page":        _detect_address_on_page(html),

        # New columns -- added by websignalz_p1_columns.sql migration
        "website_has_privacy_policy": _detect_privacy_policy(html),
        "website_has_terms":          _detect_terms(html),
        "website_has_cookie_consent": _detect_cookie_consent(html),
        "website_outdated_tech":      _detect_outdated_tech(html),
        "website_missing_lang":       _detect_missing_lang(html),
        "website_has_skip_link":      _detect_skip_link(html),
    }


# ── Models ───────────────────────────────────────────────────────────────────

class AuditBatchRequest(BaseModel):
    lead_ids:   Optional[List[str]] = None  # explicit list takes precedence
    niche_slug: Optional[str]       = None  # filter-based selection
    min_pain:   Optional[int]       = None
    limit:      int                 = 100   # ceiling applied before cost estimate


class AuditBatchFreeRequest(BaseModel):
    lead_ids: Optional[List[str]] = None
    limit:    int                 = 100


# ── Full audit column allow-list for the free path ───────────────────────────
_FULL_AUDIT_COLUMNS = frozenset({
    "has_chatbot", "mobile_friendly", "has_schema_markup", "is_parked",
    "website_has_ssl", "website_has_contact_form", "website_has_cta",
    "website_has_video", "website_has_blog", "website_has_testimonials",
    "website_has_booking", "website_has_sitemap", "website_meta_desc",
    "website_h1_count", "website_noindex", "website_word_count",
    "website_img_alt_missing", "has_phone_on_page", "has_address_on_page",
    "website_has_privacy_policy", "website_has_terms", "website_has_cookie_consent",
    "website_outdated_tech", "website_missing_lang", "website_has_skip_link",
    "last_audited_at",
})


# ── Lead query builder (reused by estimate + batch) ──────────────────────────

def _lead_query(db, body: AuditBatchRequest):
    """Scoped to active leads with a website. No stage gate."""
    q = (
        db.table("leads")
        .select("id, website")
        .eq("is_active", True)
        .not_.is_("website", "null")
    )
    if body.lead_ids:
        q = q.in_("id", body.lead_ids)
    if body.niche_slug:
        q = q.eq("niche_slug", body.niche_slug)
    if body.min_pain is not None:
        q = q.gte("pain_score", body.min_pain)
    return q


# ── GET /website-audit/cost-estimate ─────────────────────────────────────────

@router.get("/website-audit/cost-estimate")
async def audit_cost_estimate(
    niche_slug: Optional[str] = Query(None),
    min_pain:   Optional[int] = Query(None),
    limit:      int           = Query(100, ge=1, le=MAX_BATCH),
):
    """
    Pre-flight cost estimate. Zero charges -- safe to call freely.
    Returns how many leads WOULD be audited and the dollar cost at $0.005/scrape.
    """
    db   = get_db()
    body = AuditBatchRequest(niche_slug=niche_slug, min_pain=min_pain, limit=limit)

    try:
        count_res = (
            _lead_query(db, body)
            .select("id", count="estimated", head=True)
            .limit(limit)
            .execute()
        )
        count = min(count_res.count or 0, limit)
    except Exception:
        count = 0
    estimated_cost = round(count * COST_PER_SCRAPE, 4)

    return {
        "eligible_count":      count,
        "limit_applied":       limit,
        "cost_per_scrape_usd": COST_PER_SCRAPE,
        "estimated_cost_usd":  estimated_cost,
        "filters": {"niche_slug": niche_slug, "min_pain": min_pain},
        "next_step": (
            f"POST /website-audit/batch?confirm=true with the same filters "
            f"to spend ${estimated_cost:.2f}. Daily audit cap: $5.00."
        ),
    }


# ── POST /website-audit/batch ─────────────────────────────────────────────────

@router.post("/website-audit/batch")
async def audit_batch(
    body:    AuditBatchRequest,
    confirm: bool = Query(False, description="Must be true to spend Firecrawl credits"),
):
    """
    Audit a batch of leads for four website-quality signals.

    AUDIT-ONLY: writes ONLY has_chatbot, mobile_friendly, has_schema_markup,
    is_parked. Never touches enrichment_stage, pain_score, fit_*, firecrawl_*.

    confirm=false (default) -> dry-run: returns count + cost estimate, zero charges.
    confirm=true            -> live run: scrapes each lead, writes signals, logs cost.

    Cost: $0.005/lead scraped. Daily Firecrawl cap enforced per scrape.
    Recommended max batch: 5 000 leads ~= $25. Hard limit: 500 per request.
    """
    if body.limit > MAX_BATCH:
        raise HTTPException(status_code=400, detail=f"limit must be <= {MAX_BATCH}")
    if body.lead_ids and len(body.lead_ids) > MAX_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"lead_ids list must be <= {MAX_BATCH} per request",
        )

    db = get_db()

    if body.lead_ids:
        leads = (
            db.table("leads")
            .select("id, website")
            .in_("id", body.lead_ids)
            .eq("is_active", True)
            .not_.is_("website", "null")
            .execute()
        ).data or []
    else:
        leads = (
            _lead_query(db, body)
            .order("pain_score", desc=True)
            .limit(body.limit)
            .execute()
        ).data or []

    count          = len(leads)
    estimated_cost = round(count * COST_PER_SCRAPE, 4)

    # ── Dry-run gate ──────────────────────────────────────────────────────────
    if not confirm:
        return {
            "dry_run":             True,
            "eligible_count":      count,
            "estimated_cost_usd":  estimated_cost,
            "message": (
                f"{count} leads eligible. Estimated cost: ${estimated_cost:.2f}. "
                "Add ?confirm=true to run. Daily audit cap: $5.00."
            ),
        }

    # ── Live run ──────────────────────────────────────────────────────────────
    audited        = 0
    skipped_budget = 0
    failed         = 0
    results        = []

    # AUDIT-ONLY write set -- enforced here, never expanded
    _AUDIT_COLUMNS = frozenset(
        {"has_chatbot", "mobile_friendly", "has_schema_markup", "is_parked"}
    )

    # In-memory tally: second guard if cost_guard.log_cost writes fail to DB.
    # Cap mirrors daily_audit_cap_usd default -- blocking per batch run.
    _inmem_cap   = cost_guard._DEFAULT_CAPS.get("website_audit", 5.00)
    running_spend = 0.0

    for lead in leads:
        lead_id = lead["id"]
        website = (lead.get("website") or "").strip()
        if not website:
            skipped_budget += 1
            continue

        # In-memory cap: blocks if prior log_cost writes missed the DB
        if running_spend + COST_PER_SCRAPE > _inmem_cap:
            skipped_budget += 1
            results.append({"lead_id": lead_id, "status": "skipped_inmem_cap"})
            continue

        signals = await _fetch_audit_signals(website, lead_id)

        if signals is None:
            # Budget cap hit for this lead (cost_guard.check_budget returned False)
            skipped_budget += 1
            results.append({"lead_id": lead_id, "status": "skipped_budget"})
            continue

        running_spend += COST_PER_SCRAPE  # Firecrawl charges per attempt

        # Paranoia guard: strip any key that isn't an audit column before writing
        safe_signals = {k: v for k, v in signals.items() if k in _AUDIT_COLUMNS}

        try:
            db.table("leads").update(safe_signals).eq("id", lead_id).execute()
            audited += 1
            results.append({"lead_id": lead_id, "status": "audited", **safe_signals})
        except Exception as e:
            failed += 1
            logger.error(f"[website_audit] DB write failed for {lead_id}: {e}")
            results.append({
                "lead_id": lead_id,
                "status":  "db_error",
                "error":   str(e)[:200],
            })

    actual_cost = round(audited * COST_PER_SCRAPE, 4)

    return {
        "dry_run": False,
        "summary": {
            "requested":           count,
            "audited":             audited,
            "skipped_budget":      skipped_budget,
            "failed":              failed,
            "actual_cost_usd":     actual_cost,
            "cost_per_scrape_usd": COST_PER_SCRAPE,
        },
        "results": results,
    }


# ── POST /website-audit/batch-free ────────────────────────────────────────────

@router.post("/website-audit/batch-free")
async def audit_batch_free(body: AuditBatchFreeRequest):
    """
    Audit a batch of leads using free httpx fetch. $0.00 -- no Firecrawl, no confirm required.
    Writes ~22 website-quality signals + last_audited_at.

    Default mode (no lead_ids): pulls leads where website IS NOT NULL AND
    last_audited_at IS NULL, ordered by pain_score desc. (Bulk-fill mode.)
    lead_ids mode: audits specified leads regardless of last_audited_at.
    Max 500 leads per request.

    Unreachable sites: sets is_parked=True, website_has_ssl=False, last_audited_at=now().
    All other signals remain null for unreachable sites.
    """
    if body.limit > MAX_BATCH:
        raise HTTPException(status_code=400, detail=f"limit must be <= {MAX_BATCH}")
    if body.lead_ids and len(body.lead_ids) > MAX_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"lead_ids list must be <= {MAX_BATCH} per request",
        )

    db = get_db()

    if body.lead_ids:
        leads = (
            db.table("leads")
            .select("id, website")
            .in_("id", body.lead_ids)
            .not_.is_("website", "null")
            .execute()
        ).data or []
    else:
        leads = (
            db.table("leads")
            .select("id, website")
            .eq("is_active", True)
            .not_.is_("website", "null")
            .is_("last_audited_at", "null")
            .order("pain_score", desc=True)
            .limit(body.limit)
            .execute()
        ).data or []

    count       = len(leads)
    audited     = 0
    unreachable = 0
    failed      = 0
    results     = []

    for lead in leads:
        lead_id = lead["id"]
        website = (lead.get("website") or "").strip()
        if not website:
            unreachable += 1
            continue

        html, final_url = await _fetch_html_free(website)

        if html is None:
            # Site unreachable -- mark parked, stamp audit time, leave other signals null
            try:
                db.table("leads").update({
                    "is_parked":       True,
                    "website_has_ssl": False,
                    "last_audited_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", lead_id).execute()
                unreachable += 1
                results.append({"lead_id": lead_id, "status": "unreachable"})
            except Exception as e:
                failed += 1
                logger.error(f"[website_audit_free] DB write failed for {lead_id}: {e}")
                results.append({
                    "lead_id": lead_id,
                    "status":  "db_error",
                    "error":   str(e)[:200],
                })
            continue

        signals = _parse_signals_full(html, final_url)

        # Paranoia guard: only write columns in our known allow-list
        safe_signals = {k: v for k, v in signals.items() if k in _FULL_AUDIT_COLUMNS}
        safe_signals["last_audited_at"] = datetime.now(timezone.utc).isoformat()

        try:
            db.table("leads").update(safe_signals).eq("id", lead_id).execute()
            audited += 1
            results.append({
                "lead_id":   lead_id,
                "status":    "audited",
                "final_url": final_url,
            })
        except Exception as e:
            failed += 1
            logger.error(f"[website_audit_free] DB write failed for {lead_id}: {e}")
            results.append({
                "lead_id": lead_id,
                "status":  "db_error",
                "error":   str(e)[:200],
            })

    return {
        "cost_usd": 0.00,
        "summary": {
            "requested":   count,
            "audited":     audited,
            "unreachable": unreachable,
            "failed":      failed,
        },
        "results": results,
    }
