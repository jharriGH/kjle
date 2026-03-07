"""
KJLE API — Pipeline Health Routes
GET /kjle/v1/pipeline/health     — lead counts by niche + stage
GET /kjle/v1/pipeline/summary    — total counts + fit flags
GET /kjle/v1/pipeline/niches     — all niches with counts
"""
from fastapi import APIRouter
from ..database import get_db

router = APIRouter()


@router.get("/pipeline/health")
async def pipeline_health():
    db = get_db()
    result = db.table("v_pipeline_health").select("*").execute()
    return {"pipeline": result.data}


@router.get("/pipeline/summary")
async def pipeline_summary():
    db = get_db()

    # Total active leads
    total = db.table("leads").select("id", count="exact").eq("is_active", True).execute()

    # Fit flag counts
    demo    = db.table("leads").select("id", count="exact").eq("fit_demoenginez", True).eq("is_active", True).execute()
    rep     = db.table("leads").select("id", count="exact").eq("fit_reputation", True).eq("is_active", True).execute()
    schema  = db.table("leads").select("id", count="exact").eq("fit_schema_ranker", True).eq("is_active", True).execute()
    voice   = db.table("leads").select("id", count="exact").eq("fit_voicedrop", True).eq("is_active", True).execute()

    # Stage counts
    stage0  = db.table("leads").select("id", count="exact").eq("enrichment_stage", 0).eq("is_active", True).execute()
    stage1  = db.table("leads").select("id", count="exact").eq("enrichment_stage", 1).eq("is_active", True).execute()
    stage2  = db.table("leads").select("id", count="exact").eq("enrichment_stage", 2).eq("is_active", True).execute()
    stage3  = db.table("leads").select("id", count="exact").eq("enrichment_stage", 3).eq("is_active", True).execute()

    return {
        "total_leads":      total.count,
        "product_fit": {
            "demoenginez":  demo.count,
            "reputation":   rep.count,
            "schema_ranker":schema.count,
            "voicedrop":    voice.count,
        },
        "enrichment_stages": {
            "stage_0_raw":      stage0.count,
            "stage_1_scraped":  stage1.count,
            "stage_2_pagespeed":stage2.count,
            "stage_3_outscraper":stage3.count,
        },
    }


@router.get("/pipeline/niches")
async def pipeline_niches():
    db = get_db()
    result = (
        db.table("leads")
        .select("niche_slug")
        .eq("is_active", True)
        .execute()
    )
    # Count by niche
    counts = {}
    for row in result.data:
        slug = row["niche_slug"] or "other"
        counts[slug] = counts.get(slug, 0) + 1

    sorted_niches = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return {"niches": [{"niche": k, "count": v} for k, v in sorted_niches]}
