"""
KJLE — Website Quality Audit
File: api/routes/website_audit.py

Derives four boolean signals from a SINGLE Firecrawl rawHtml fetch per lead:
  has_chatbot       — live-chat / chatbot widget in page HTML
  mobile_friendly   — <meta name="viewport"> present
  has_schema_markup — JSON-LD or microdata schema.org markup present
  is_parked         — fetch failure, parking keywords, or near-empty page

AUDIT-ONLY hard constraints:
  • Writes ONLY: has_chatbot, mobile_friendly, has_schema_markup, is_parked
  • NEVER writes: enrichment_stage, pain_score, fit_*, firecrawl_*
  • NEVER calls or schedules enrichment Stage 1/3/4 endpoints
  • Firecrawl format = ["rawHtml"] only — no markdown, no extract

Cost: $0.005 / scrape (Firecrawl). confirm=true required to spend money.
Daily cap enforced per-lead via cost_guard.check_budget.
"""

import logging
import re
from typing import Optional, List

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


# ── Signal parsers (pure — no I/O) ───────────────────────────────────────────

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
    """All four audit signals from raw HTML. Pure — no I/O, no side-effects."""
    return {
        "has_chatbot":       _detect_chatbot(html),
        "mobile_friendly":   _detect_mobile(html),
        "has_schema_markup": _detect_schema(html),
        "is_parked":         _detect_parked(html),
    }


# ── Firecrawl caller — rawHtml only ──────────────────────────────────────────

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
        logger.warning("[website_audit] FIRECRAWL_API_KEY not set — skipping")
        return None

    allowed = await cost_guard.check_budget(
        service="website_audit",
        estimated_cost_usd=COST_PER_SCRAPE,
        job_name="website_audit_batch",
        leads_affected=1,
    )
    if not allowed:
        return None  # budget cap hit — caller records as skipped

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

    # Log cost — Firecrawl charges per attempt, not per success.
    await cost_guard.log_cost(
        stage="website_audit",
        service="website_audit",
        cost_usd=COST_PER_SCRAPE,
        lead_id=lead_id,
        items_fetched=1,
        metadata={"url": url},
    )
    return signals


# ── Models ───────────────────────────────────────────────────────────────────

class AuditBatchRequest(BaseModel):
    lead_ids:   Optional[List[str]] = None  # explicit list takes precedence
    niche_slug: Optional[str]       = None  # filter-based selection
    min_pain:   Optional[int]       = None
    limit:      int                 = 100   # ceiling applied before cost estimate


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
    Pre-flight cost estimate. Zero charges — safe to call freely.
    Returns how many leads WOULD be audited and the dollar cost at $0.005/scrape.
    """
    db   = get_db()
    body = AuditBatchRequest(niche_slug=niche_slug, min_pain=min_pain, limit=limit)

    count_res = (
        _lead_query(db, body)
        .select("id", count="exact")
        .limit(limit)
        .execute()
    )
    count          = min(count_res.count or 0, limit)
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

    confirm=false (default) → dry-run: returns count + cost estimate, zero charges.
    confirm=true            → live run: scrapes each lead, writes signals, logs cost.

    Cost: $0.005/lead scraped. Daily Firecrawl cap enforced per scrape.
    Recommended max batch: 5 000 leads ≈ $25. Hard limit: 500 per request.
    """
    if body.limit > MAX_BATCH:
        raise HTTPException(status_code=400, detail=f"limit must be ≤ {MAX_BATCH}")
    if body.lead_ids and len(body.lead_ids) > MAX_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"lead_ids list must be ≤ {MAX_BATCH} per request",
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

    # AUDIT-ONLY write set — enforced here, never expanded
    _AUDIT_COLUMNS = frozenset(
        {"has_chatbot", "mobile_friendly", "has_schema_markup", "is_parked"}
    )

    # In-memory tally: second guard if cost_guard.log_cost writes fail to DB.
    # Cap mirrors daily_audit_cap_usd default — blocking per batch run.
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
