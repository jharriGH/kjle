"""
Segmentation Engine
KJLE - King James Lead Empire
Routes: /kjle/v1/segments/*

Classifies leads into hot / warm / cold segments based on
pain_score, fit_demoenginez, enrichment_stage, and website_reachable.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query
from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Classification Logic
# ─────────────────────────────────────────────────────────────────────────────

def _classify_lead(lead: dict) -> str:
    """
    Returns 'hot', 'warm', or 'cold' for a single lead dict.

    Hot (ALL must be true):
      - pain_score >= 70
      - fit_demoenginez = true
      - enrichment_stage >= 1
      - website_reachable = true OR enrichment_stage = 0

    Warm (ANY of these):
      - pain_score >= 40 AND pain_score < 70
      - pain_score >= 70 AND fit_demoenginez = false
      - pain_score >= 70 AND enrichment_stage = 0

    Cold: everything else
    """
    pain    = lead.get("pain_score") or 0
    fit     = bool(lead.get("fit_demoenginez"))
    stage   = lead.get("enrichment_stage") or 0
    reach   = bool(lead.get("website_reachable"))

    # ── Hot gate ─────────────────────────────────────────────────────────────
    if (
        pain >= 70
        and fit
        and stage >= 1
        and (reach or stage == 0)
    ):
        return "hot"

    # ── Warm gate ────────────────────────────────────────────────────────────
    if (40 <= pain < 70):
        return "warm"
    if pain >= 70 and not fit:
        return "warm"
    if pain >= 70 and stage == 0:
        return "warm"

    # ── Cold fallback ─────────────────────────────────────────────────────────
    return "cold"


# ─────────────────────────────────────────────────────────────────────────────
# POST /segments/classify
# Runs classification on all active leads, writes segment_label back to DB
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/segments/classify")
async def classify_leads(
    niche_slug: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
):
    """
    Classifies active leads into hot / warm / cold and writes segment_label
    back to the leads table. Processes in a single paginated fetch.
    Returns summary counts.
    """
    db = get_db()

    query = (
        db.table("leads")
        .select(
            "id, pain_score, fit_demoenginez, enrichment_stage, website_reachable"
        )
        .eq("is_active", True)
    )

    if niche_slug:
        query = query.eq("niche_slug", niche_slug)
    if state:
        query = query.eq("state", state.upper())

    query = query.limit(limit)
    result = query.execute()
    leads = result.data or []

    if not leads:
        return {
            "status": "success",
            "message": "No active leads found matching filters",
            "summary": {"hot": 0, "warm": 0, "cold": 0, "total_classified": 0},
            "filters": {"niche_slug": niche_slug, "state": state, "limit": limit},
        }

    counts = {"hot": 0, "warm": 0, "cold": 0}
    failed = 0

    for lead in leads:
        label = _classify_lead(lead)
        counts[label] += 1

        try:
            db.table("leads").update({
                "segment_label": label,
                "segmented_at":  datetime.now(timezone.utc).isoformat(),
            }).eq("id", lead["id"]).execute()
        except Exception as e:
            failed += 1
            logger.error(f"Failed to update segment_label for lead {lead['id']}: {e}")

    return {
        "status": "success",
        "summary": {
            "hot":              counts["hot"],
            "warm":             counts["warm"],
            "cold":             counts["cold"],
            "total_classified": len(leads),
            "failed_writes":    failed,
        },
        "filters": {"niche_slug": niche_slug, "state": state, "limit": limit},
        "classified_at": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /segments/summary
# Total hot/warm/cold counts + top 5 niches by hot lead count
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/segments/summary")
async def get_segment_summary(
    niche_slug: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
):
    """
    Returns total hot / warm / cold counts and top 5 niches by hot lead count.
    Reads from the segment_label field — run /classify first to ensure freshness.
    """
    db = get_db()

    def _count(label: str) -> int:
        q = (
            db.table("leads")
            .select("id", count="exact")
            .eq("is_active", True)
            .eq("segment_label", label)
        )
        if niche_slug:
            q = q.eq("niche_slug", niche_slug)
        if state:
            q = q.eq("state", state.upper())
        r = q.execute()
        return r.count if r.count is not None else 0

    hot_count  = _count("hot")
    warm_count = _count("warm")
    cold_count = _count("cold")
    total = hot_count + warm_count + cold_count

    def _pct(n):
        return round(n / total * 100, 2) if total > 0 else 0.0

    # Top 5 niches by hot lead count (no niche/state filter applied to ranking).
    # Uses segments_by_niche() RPC for server-side GROUP BY — the prior
    # implementation pulled raw rows and would silently truncate at ~1K
    # once total hot leads grew past PostgREST's default row cap.
    try:
        rpc_res = db.rpc("segments_by_niche", {
            "p_state":                None,
            "p_include_unclassified": False,
        }).execute()
        rpc_rows = rpc_res.data or []
    except Exception as e:
        logger.error(f"segments_by_niche RPC failed in /segments/summary: {e}")
        rpc_rows = []

    top_niches = sorted(
        rpc_rows,
        key=lambda r: int(r.get("hot_count") or 0),
        reverse=True,
    )[:5]

    return {
        "status": "success",
        "filters": {"niche_slug": niche_slug, "state": state},
        "counts": {
            "hot":  hot_count,
            "warm": warm_count,
            "cold": cold_count,
            "total_segmented": total,
        },
        "percentages": {
            "hot_pct":  _pct(hot_count),
            "warm_pct": _pct(warm_count),
            "cold_pct": _pct(cold_count),
        },
        "top_5_niches_by_hot": [
            {
                "niche_slug": r.get("niche_slug") or "unknown",
                "hot_count":  int(r.get("hot_count") or 0),
            }
            for r in top_niches
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Shared paginated lead fetcher
# ─────────────────────────────────────────────────────────────────────────────

def _paginated_leads(
    db,
    label: str,
    niche_slug: Optional[str],
    state: Optional[str],
    page: int,
    page_size: int,
):
    """Fetches a page of leads filtered by segment_label, sorted by pain_score desc."""
    offset = (page - 1) * page_size

    query = (
        db.table("leads")
        .select(
            "id, business_name, niche_slug, city, state, phone, email, website, "
            "pain_score, segment_label, enrichment_stage, fit_demoenginez, "
            "website_reachable, has_schema_markup, yelp_rating, "
            "outscraper_rating, outscraper_reviews_count, created_at"
        )
        .eq("is_active", True)
        .eq("segment_label", label)
    )

    if niche_slug:
        query = query.eq("niche_slug", niche_slug)
    if state:
        query = query.eq("state", state.upper())

    # Count total matching for pagination metadata
    count_query = (
        db.table("leads")
        .select("id", count="exact")
        .eq("is_active", True)
        .eq("segment_label", label)
    )
    if niche_slug:
        count_query = count_query.eq("niche_slug", niche_slug)
    if state:
        count_query = count_query.eq("state", state.upper())

    total = (count_query.execute().count or 0)

    leads = (
        query
        .order("pain_score", desc=True)
        .range(offset, offset + page_size - 1)
        .execute()
        .data or []
    )

    total_pages = max(1, -(-total // page_size))  # ceiling division

    return {
        "status": "success",
        "segment": label,
        "filters": {"niche_slug": niche_slug, "state": state},
        "pagination": {
            "page":        page,
            "page_size":   page_size,
            "total":       total,
            "total_pages": total_pages,
            "has_next":    page < total_pages,
            "has_prev":    page > 1,
        },
        "leads": leads,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /segments/hot
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/segments/hot")
async def get_hot_leads(
    niche_slug: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """Returns hot leads paginated, sorted by pain_score desc."""
    db = get_db()
    return _paginated_leads(db, "hot", niche_slug, state, page, page_size)


# ─────────────────────────────────────────────────────────────────────────────
# GET /segments/warm
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/segments/warm")
async def get_warm_leads(
    niche_slug: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """Returns warm leads paginated, sorted by pain_score desc."""
    db = get_db()
    return _paginated_leads(db, "warm", niche_slug, state, page, page_size)


# ─────────────────────────────────────────────────────────────────────────────
# GET /segments/cold
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/segments/cold")
async def get_cold_leads(
    niche_slug: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """Returns cold leads paginated, sorted by pain_score desc."""
    db = get_db()
    return _paginated_leads(db, "cold", niche_slug, state, page, page_size)


# ─────────────────────────────────────────────────────────────────────────────
# GET /segments/by-niche
# Segment breakdown (hot/warm/cold) per niche_slug
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/segments/by-niche")
async def get_segments_by_niche(
    state:                Optional[str] = Query(None),
    include_unclassified: bool          = Query(True, description="Include leads with no segment_label"),
):
    """
    Returns hot / warm / cold (and optionally unclassified) counts per
    niche_slug, sorted by total volume desc.

    Aggregation runs server-side in Postgres via the segments_by_niche()
    RPC — the previous Python-side aggregation silently truncated to
    ~1,000 rows due to PostgREST's default row cap.
    """
    db = get_db()

    res = db.rpc("segments_by_niche", {
        "p_state":                state.upper() if state else None,
        "p_include_unclassified": include_unclassified,
    }).execute()
    rows = res.data or []

    niches = []
    for r in rows:
        hot   = int(r.get("hot_count")          or 0)
        warm  = int(r.get("warm_count")         or 0)
        cold  = int(r.get("cold_count")         or 0)
        unc   = int(r.get("unclassified_count") or 0)
        total = int(r.get("total")              or 0)
        niches.append({
            "niche_slug":         r.get("niche_slug") or "unknown",
            "hot_count":          hot,
            "warm_count":         warm,
            "cold_count":         cold,
            "unclassified_count": unc,
            "total":              total,
            "hot_pct":            round(hot / total * 100, 2) if total else 0.0,
        })

    return {
        "status":      "success",
        "filters":     {"state": state, "include_unclassified": include_unclassified},
        "niche_count": len(niches),
        "niches":      niches,
    }
