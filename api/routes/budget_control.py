"""
KJLE — Prompt 31: Budget Control Routes
File: api/routes/budget_control.py
"""

import os
import logging
from typing import Optional
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Header, Query
from pydantic import BaseModel
from supabase import create_client, Client

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")

KNOWN_SERVICES = ["outscraper", "firecrawl", "yelp", "truelist", "openai", "anthropic", "other"]

router = APIRouter()


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


def current_month_range():
    """Return ISO strings for start and end of current UTC month."""
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), now.isoformat()


# ─────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────

class GuardrailUpsert(BaseModel):
    service: str
    monthly_limit_usd: float
    alert_threshold_pct: Optional[float] = 80.0  # alert when spend hits this % of limit


# ─────────────────────────────────────────────────
# GET /kjle/v1/budget/summary
# MTD spend vs guardrail limits for all services
# ─────────────────────────────────────────────────

@router.get("/budget/summary")
async def budget_summary(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    month_start, now_iso = current_month_range()

    try:
        # Pull all MTD cost log entries
        cost_res = supabase.table("api_cost_log").select(
            "service, cost_usd"
        ).gte("created_at", month_start).lte("created_at", now_iso).execute()

        # Pull all guardrails
        guardrail_res = supabase.table("budget_guardrails").select("*").execute()
        guardrails = {g["service"]: g for g in (guardrail_res.data or [])}

        # Aggregate spend by service
        spend_by_service: dict = {}
        for row in (cost_res.data or []):
            svc = row.get("service", "other")
            spend_by_service[svc] = spend_by_service.get(svc, 0.0) + float(row.get("cost_usd") or 0)

        total_mtd = sum(spend_by_service.values())

        # Build per-service summary
        services = []
        all_services = set(list(spend_by_service.keys()) + list(guardrails.keys()))
        for svc in sorted(all_services):
            spent = spend_by_service.get(svc, 0.0)
            guardrail = guardrails.get(svc)
            limit = float(guardrail["monthly_limit_usd"]) if guardrail else None
            threshold_pct = float(guardrail["alert_threshold_pct"]) if guardrail else 80.0
            pct_used = round((spent / limit * 100), 1) if limit else None

            status = "ok"
            if limit:
                if spent >= limit:
                    status = "over_limit"
                elif pct_used >= threshold_pct:
                    status = "alert"

            services.append({
                "service": svc,
                "mtd_spend_usd": round(spent, 4),
                "monthly_limit_usd": limit,
                "alert_threshold_pct": threshold_pct,
                "pct_used": pct_used,
                "status": status,
            })

        return {
            "month": datetime.now(timezone.utc).strftime("%Y-%m"),
            "total_mtd_spend_usd": round(total_mtd, 4),
            "services": services,
        }

    except Exception as e:
        logger.error(f"budget_summary error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# GET /kjle/v1/budget/alerts
# Services currently over alert threshold or monthly limit
# ─────────────────────────────────────────────────

@router.get("/budget/alerts")
async def budget_alerts(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    month_start, now_iso = current_month_range()

    try:
        cost_res = supabase.table("api_cost_log").select(
            "service, cost_usd"
        ).gte("created_at", month_start).lte("created_at", now_iso).execute()

        guardrail_res = supabase.table("budget_guardrails").select("*").execute()
        guardrails = {g["service"]: g for g in (guardrail_res.data or [])}

        spend_by_service: dict = {}
        for row in (cost_res.data or []):
            svc = row.get("service", "other")
            spend_by_service[svc] = spend_by_service.get(svc, 0.0) + float(row.get("cost_usd") or 0)

        alerts = []
        for svc, guardrail in guardrails.items():
            spent = spend_by_service.get(svc, 0.0)
            limit = float(guardrail["monthly_limit_usd"])
            threshold_pct = float(guardrail.get("alert_threshold_pct") or 80.0)
            pct_used = round((spent / limit * 100), 1) if limit else 0

            if spent >= limit:
                alerts.append({
                    "service": svc,
                    "level": "over_limit",
                    "message": f"{svc} has exceeded monthly limit of ${limit:.2f} (spent ${spent:.4f})",
                    "mtd_spend_usd": round(spent, 4),
                    "monthly_limit_usd": limit,
                    "pct_used": pct_used,
                })
            elif pct_used >= threshold_pct:
                alerts.append({
                    "service": svc,
                    "level": "alert",
                    "message": f"{svc} is at {pct_used}% of monthly limit (${spent:.4f} of ${limit:.2f})",
                    "mtd_spend_usd": round(spent, 4),
                    "monthly_limit_usd": limit,
                    "pct_used": pct_used,
                })

        return {
            "alert_count": len(alerts),
            "alerts": alerts,
        }

    except Exception as e:
        logger.error(f"budget_alerts error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# GET /kjle/v1/budget/guardrails — List all guardrails
# ─────────────────────────────────────────────────

@router.get("/budget/guardrails")
async def list_guardrails(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        result = supabase.table("budget_guardrails").select("*").order("service").execute()
        return {
            "total": len(result.data or []),
            "guardrails": result.data or [],
        }
    except Exception as e:
        logger.error(f"list_guardrails error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/budget/guardrails — Upsert a guardrail
# ─────────────────────────────────────────────────

@router.post("/budget/guardrails")
async def upsert_guardrail(payload: GuardrailUpsert, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    if payload.monthly_limit_usd <= 0:
        raise HTTPException(status_code=400, detail="monthly_limit_usd must be greater than 0")
    if not (0 < payload.alert_threshold_pct <= 100):
        raise HTTPException(status_code=400, detail="alert_threshold_pct must be between 1 and 100")

    try:
        now = datetime.now(timezone.utc).isoformat()

        # Check if guardrail exists for this service
        existing = supabase.table("budget_guardrails").select("id").eq("service", payload.service).execute()

        if existing.data:
            # Update existing
            result = supabase.table("budget_guardrails").update({
                "monthly_limit_usd": payload.monthly_limit_usd,
                "alert_threshold_pct": payload.alert_threshold_pct,
                "updated_at": now,
            }).eq("service", payload.service).execute()
        else:
            # Insert new
            result = supabase.table("budget_guardrails").insert({
                "service": payload.service,
                "monthly_limit_usd": payload.monthly_limit_usd,
                "alert_threshold_pct": payload.alert_threshold_pct,
                "created_at": now,
                "updated_at": now,
            }).execute()

        if not result.data:
            raise HTTPException(status_code=500, detail="Upsert returned no data")

        return result.data[0]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"upsert_guardrail error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# DELETE /kjle/v1/budget/guardrails/{guardrail_id}
# ─────────────────────────────────────────────────

@router.delete("/budget/guardrails/{guardrail_id}")
async def delete_guardrail(guardrail_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        existing = supabase.table("budget_guardrails").select("id").eq("id", guardrail_id).single().execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Guardrail not found")

        supabase.table("budget_guardrails").delete().eq("id", guardrail_id).execute()
        return {"deleted": True, "guardrail_id": guardrail_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete_guardrail error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# GET /kjle/v1/budget/cost-log — Paginated cost log
# ─────────────────────────────────────────────────

@router.get("/budget/cost-log")
async def cost_log(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    service: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, description="ISO date string e.g. 2026-03-01"),
    date_to: Optional[str] = Query(None, description="ISO date string e.g. 2026-03-31"),
    x_api_key: str = Header(...),
):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    offset = (page - 1) * page_size

    try:
        query = supabase.table("api_cost_log").select("*", count="exact")

        if service:
            query = query.eq("service", service)
        if date_from:
            query = query.gte("created_at", date_from)
        if date_to:
            query = query.lte("created_at", date_to)

        query = query.order("created_at", desc=True).range(offset, offset + page_size - 1)
        result = query.execute()

        return {
            "total": result.count or 0,
            "page": page,
            "page_size": page_size,
            "results": result.data or [],
        }

    except Exception as e:
        logger.error(f"cost_log error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/budget/reset
# Admin-only: delete all api_cost_log entries for current month (testing use)
# ─────────────────────────────────────────────────

@router.post("/budget/reset")
async def reset_mtd_costs(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    month_start, now_iso = current_month_range()

    try:
        result = supabase.table("api_cost_log").delete().gte(
            "created_at", month_start
        ).lte("created_at", now_iso).execute()

        deleted_count = len(result.data) if result.data else 0

        return {
            "reset": True,
            "month": datetime.now(timezone.utc).strftime("%Y-%m"),
            "entries_deleted": deleted_count,
        }

    except Exception as e:
        logger.error(f"reset_mtd_costs error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
