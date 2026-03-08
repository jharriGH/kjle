"""
KJLE API — Pain Score Intelligence Routes
GET /kjle/v1/pain/top            — top N leads by pain score
GET /kjle/v1/pain/distribution   — bucket counts (0-20, 21-40, etc.)
GET /kjle/v1/pain/by-niche       — avg pain + hot lead count per niche
GET /kjle/v1/pain/hot-leads      — unenriched DemoEnginez-fit leads pain>=70
GET /kjle/v1/pain/benchmarks     — full stats for a specific niche
"""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from ..database import get_db

router = APIRouter()


@router.get("/pain/top")
async def get_top_pain_leads(
    niche_slug: Optional[str] = Query(None),
    state:      Optional[str] = Query(None),
    limit:      int           = Query(25, ge=1, le=100),
    min_pain:   int           = Query(60, ge=0, le=100),
):
    db = get_db()
    query = (
        db.table("leads")
        .select(
            "id, business_name, niche_slug, city, state, phone, email, "
            "website, pain_score, enrichment_stage, fit_demoenginez, created_at"
        )
        .eq("is_active", True)
        .gte("pain_score", min_pain)
        .order("pain_score", desc=True)
        .limit(limit)
    )
    if niche_slug:
        query = query.eq("niche_slug", niche_slug)
    if state:
        query = query.eq("state", state.upper())

    result = query.execute()
    return {
        "filters": {"niche_slug": niche_slug, "state": state, "min_pain": min_pain, "limit": limit},
        "count": len(result.data),
        "leads": result.data,
    }


@router.get("/pain/distribution")
async def get_pain_distribution(
    niche_slug: Optional[str] = Query(None),
):
    db = get_db()
    query = db.table("leads").select("pain_score").eq("is_active", True)
    if niche_slug:
        query = query.eq("niche_slug", niche_slug)
    result = query.execute()

    scores = [r["pain_score"] for r in result.data if r["pain_score"] is not None]
    total = len(scores) or 1

    buckets = [
        {"label": "0–20",   "range": [0, 20],   "count": sum(1 for s in scores if 0  <= s <= 20)},
        {"label": "21–40",  "range": [21, 40],  "count": sum(1 for s in scores if 21 <= s <= 40)},
        {"label": "41–60",  "range": [41, 60],  "count": sum(1 for s in scores if 41 <= s <= 60)},
        {"label": "61–80",  "range": [61, 80],  "count": sum(1 for s in scores if 61 <= s <= 80)},
        {"label": "81–100", "range": [81, 100], "count": sum(1 for s in scores if 81 <= s <= 100)},
    ]
    for b in buckets:
        b["percentage"] = round((b["count"] / total) * 100, 2)

    return {
        "filters": {"niche_slug": niche_slug},
        "total_leads": len(scores),
        "distribution": buckets,
    }


@router.get("/pain/by-niche")
async def get_pain_by_niche(
    state: Optional[str] = Query(None),
):
    db = get_db()
    query = db.table("leads").select("niche_slug, pain_score").eq("is_active", True)
    if state:
        query = query.eq("state", state.upper())
    result = query.execute()

    # Aggregate in Python
    niches: dict = {}
    for row in result.data:
        slug = row["niche_slug"]
        score = row["pain_score"]
        if not slug or score is None:
            continue
        if slug not in niches:
            niches[slug] = {"niche_slug": slug, "lead_count": 0, "total_pain": 0, "hot_lead_count": 0}
        niches[slug]["lead_count"] += 1
        niches[slug]["total_pain"] += score
        if score >= 70:
            niches[slug]["hot_lead_count"] += 1

    output = []
    for n in niches.values():
        output.append({
            "niche_slug":     n["niche_slug"],
            "lead_count":     n["lead_count"],
            "avg_pain":       round(n["total_pain"] / n["lead_count"], 2),
            "hot_lead_count": n["hot_lead_count"],
        })
    output.sort(key=lambda x: x["avg_pain"], reverse=True)

    return {
        "filters": {"state": state},
        "niche_count": len(output),
        "niches": output,
    }


@router.get("/pain/hot-leads")
async def get_hot_leads(
    niche_slug: Optional[str] = Query(None),
    state:      Optional[str] = Query(None),
    limit:      int           = Query(50, ge=1, le=200),
):
    db = get_db()
    query = (
        db.table("leads")
        .select(
            "id, business_name, niche_slug, city, state, phone, email, "
            "website, pain_score, enrichment_stage, fit_demoenginez, created_at"
        )
        .eq("is_active", True)
        .gte("pain_score", 70)
        .eq("fit_demoenginez", True)
        .eq("enrichment_stage", 0)
        .order("pain_score", desc=True)
        .limit(limit)
    )
    if niche_slug:
        query = query.eq("niche_slug", niche_slug)
    if state:
        query = query.eq("state", state.upper())

    result = query.execute()
    return {
        "filters": {"niche_slug": niche_slug, "state": state, "limit": limit},
        "description": "Unenriched, DemoEnginez-fit leads with pain_score >= 70",
        "count": len(result.data),
        "hot_leads": result.data,
    }


@router.get("/pain/benchmarks")
async def get_pain_benchmarks(
    niche_slug: str = Query(..., description="Niche slug (required)"),
):
    db = get_db()
    result = (
        db.table("leads")
        .select("pain_score")
        .eq("is_active", True)
        .eq("niche_slug", niche_slug)
        .execute()
    )

    scores = sorted([r["pain_score"] for r in result.data if r["pain_score"] is not None])
    if not scores:
        raise HTTPException(status_code=404, detail=f"No active leads found for niche_slug='{niche_slug}'")

    total = len(scores)
    mid = total // 2
    median = (scores[mid] if total % 2 != 0 else (scores[mid - 1] + scores[mid]) / 2)

    return {
        "niche_slug": niche_slug,
        "benchmarks": {
            "total_leads": total,
            "avg_pain":    round(sum(scores) / total, 2),
            "median_pain": round(median, 2),
            "min_pain":    scores[0],
            "max_pain":    scores[-1],
            "hot_leads":   sum(1 for s in scores if s >= 70),
            "warm_leads":  sum(1 for s in scores if 40 <= s < 70),
            "cold_leads":  sum(1 for s in scores if s < 40),
        },
    }
