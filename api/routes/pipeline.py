"""
api/routes/pipeline.py
GET /pipeline/status — DataQualityPanel data source
"""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from ..database import get_db

router = APIRouter()


@router.get("/pipeline/status")
async def pipeline_status(db=Depends(get_db)):
    now = datetime.now(timezone.utc).isoformat()

    try:
        # ── Total active leads ─────────────────────────────────────────────
        total_res = (
            db.table("leads")
            .select("id", count="exact")
            .eq("is_active", True)
            .execute()
        )
        total = total_res.count or 0

        if total == 0:
            return {
                "status": "success",
                "total_leads": 0,
                "phone_coverage_pct": 0.0,
                "email_coverage_pct": 0.0,
                "website_coverage_pct": 0.0,
                "enrichment_stages": {f"stage_{i}": 0 for i in range(5)},
                "segment_counts": {"hot": 0, "warm": 0, "cold": 0, "unclassified": 0},
                "last_updated": now,
            }

        # ── Phone coverage ─────────────────────────────────────────────────
        phone_res = (
            db.table("leads")
            .select("id", count="exact")
            .eq("is_active", True)
            .not_.is_("phone", "null")
            .neq("phone", "")
            .execute()
        )
        phone_count = phone_res.count or 0

        # ── Email coverage ─────────────────────────────────────────────────
        email_res = (
            db.table("leads")
            .select("id", count="exact")
            .eq("is_active", True)
            .not_.is_("email", "null")
            .neq("email", "")
            .execute()
        )
        email_count = email_res.count or 0

        # ── Website coverage ───────────────────────────────────────────────
        website_res = (
            db.table("leads")
            .select("id", count="exact")
            .eq("is_active", True)
            .not_.is_("website", "null")
            .neq("website", "")
            .execute()
        )
        website_count = website_res.count or 0

        # ── Enrichment stage counts (0–4) ──────────────────────────────────
        enrichment_stages = {}
        for stage in range(5):
            stage_res = (
                db.table("leads")
                .select("id", count="exact")
                .eq("is_active", True)
                .eq("enrichment_stage", stage)
                .execute()
            )
            enrichment_stages[f"stage_{stage}"] = stage_res.count or 0

        # ── Segment label counts ───────────────────────────────────────────
        segment_counts = {"hot": 0, "warm": 0, "cold": 0, "unclassified": 0}
        for label in ("hot", "warm", "cold"):
            seg_res = (
                db.table("leads")
                .select("id", count="exact")
                .eq("is_active", True)
                .eq("segment_label", label)
                .execute()
            )
            segment_counts[label] = seg_res.count or 0

        # Unclassified = total minus all labeled
        labeled = segment_counts["hot"] + segment_counts["warm"] + segment_counts["cold"]
        segment_counts["unclassified"] = max(total - labeled, 0)

        # ── Percentages ────────────────────────────────────────────────────
        def pct(n):
            return round((n / total) * 100, 2) if total > 0 else 0.0

        return {
            "status": "success",
            "total_leads": total,
            "phone_coverage_pct": pct(phone_count),
            "email_coverage_pct": pct(email_count),
            "website_coverage_pct": pct(website_count),
            "enrichment_stages": enrichment_stages,
            "segment_counts": segment_counts,
            "last_updated": now,
        }

    except Exception as e:
        return {
            "status": "error",
            "detail": str(e),
            "total_leads": 0,
            "phone_coverage_pct": 0.0,
            "email_coverage_pct": 0.0,
            "website_coverage_pct": 0.0,
            "enrichment_stages": {f"stage_{i}": 0 for i in range(5)},
            "segment_counts": {"hot": 0, "warm": 0, "cold": 0, "unclassified": 0},
            "last_updated": now,
        }
