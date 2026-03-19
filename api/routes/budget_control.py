"""
KJLE — Prompt 31: Budget Control Routes
File: api/routes/budget_control.py

Real schema:
  api_cost_log:      service, cost_per_unit, records_processed, created_at
  budget_guardrails: id, service (nullable), period, limit_amount, action, active, created_at

Cost calculation: cost_per_unit * records_processed
Upsert key: service + period (unique combination)
Delete: by id
"""

import os
import logging
from typing import Optional
from datetime import datetime, timezone, date, timedelta
from fastapi import APIRouter, HTTPException, Header, Query
from pydantic import BaseModel
from supabase import create_client, Client

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")

router = APIRouter()


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


def _safe_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _safe_int(val, default=1) -> int:
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _row_cost(row: dict) -> float:
    return _safe_float(row.get("cost_per_unit", 0)) * _safe_int(row.get("records_processed", 1))


def _month_start_iso() -> str:
    return date.today().replace(day=1).isoformat()


def _today_iso() -> str:
    return date.today().isoformat()


# ─────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────

class GuardrailCreate(BaseModel):
    service: Optional[str] = None   # None = global (applies to all services)
    period: str                      # daily | monthly
    limit_amount: float
    action: str                      # alert | pause


# ─────────────────────────────────────────────────
# GET /kjle/v1/budget/summary
# ─────────────────────────────────────────────────

@router.get("/budget/summary")
async def budget_summary(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    month_start = _month_start_iso()
    today = _today_iso()

    try:
        month_rows = supabase.table("api_cost_log").select(
            "service, cost_per_unit, records_processed"
        ).gte("created_at", month_start).execute().data or []

        today_rows = supabase.table("api_cost_log").select(
            "service, cost_per_unit, records_processed"
        ).gte("created_at", today).execute().data or []

        guardrails = supabase.table("budget_guardrails").select("*").eq("active", True).execute().data or []

        # Aggregate spend by service
        mtd_by_service: dict = {}
        for row in month_rows:
            svc = row.get("service") or "unknown"
            mtd_by_service[svc] = mtd_by_service.get(svc, 0.0) + _row_cost(row)

        today_by_service: dict = {}
        for row in today_rows:
            svc = row.get("service") or "unknown"
            today_by_service[svc] = today_by_service.get(svc, 0.0) + _row_cost(row)

        total_mtd = round(sum(mtd_by_service.values()), 6)
        total_today = round(sum(today_by_service.values()), 6)

        # Build per-service summary
        service_rows = []
        for svc in sorted(set(mtd_by_service.keys())):
            mtd_spent = round(mtd_by_service.get(svc, 0.0), 6)
            today_spent = round(today_by_service.get(svc, 0.0), 6)

            svc_monthly = next((g for g in guardrails if g.get("service") == svc and g.get("period") == "monthly"), None)
            svc_daily   = next((g for g in guardrails if g.get("service") == svc and g.get("period") == "daily"), None)
            global_monthly = next((g for g in guardrails if g.get("service") is None and g.get("period") == "monthly"), None)
            global_daily   = next((g for g in guardrails if g.get("service") is None and g.get("period") == "daily"), None)

            monthly_limit = _safe_float((svc_monthly or global_monthly or {}).get("limit_amount")) or None
            daily_limit   = _safe_float((svc_daily or global_daily or {}).get("limit_amount")) or None

            monthly_pct = round((mtd_spent / monthly_limit * 100), 1) if monthly_limit else None
            daily_pct   = round((today_spent / daily_limit * 100), 1) if daily_limit else None

            status = "ok"
            if monthly_limit and mtd_spent >= monthly_limit:
                status = "over_monthly_limit"
            elif daily_limit and today_spent >= daily_limit:
                status = "over_daily_limit"
            elif monthly_pct and monthly_pct >= 80:
                status = "alert"

            service_rows.append({
                "service": svc,
                "mtd_spend_usd": mtd_spent,
                "today_spend_usd": today_spent,
                "monthly_limit_usd": monthly_limit,
                "daily_limit_usd": daily_limit,
                "monthly_pct_used": monthly_pct,
                "daily_pct_used": daily_pct,
                "status": status,
            })

        return {
            "month": date.today().strftime("%Y-%m"),
            "total_mtd_spend_usd": total_mtd,
            "total_today_spend_usd": total_today,
            "services": service_rows,
            "active_guardrails": len(guardrails),
        }

    except Exception as e:
        logger.error(f"budget_summary error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# GET /kjle/v1/budget/alerts
# ─────────────────────────────────────────────────

@router.get("/budget/alerts")
async def budget_alerts(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    month_start = _month_start_iso()
    today = _today_iso()

    try:
        month_rows = supabase.table("api_cost_log").select(
            "service, cost_per_unit, records_processed"
        ).gte("created_at", month_start).execute().data or []

        today_rows = supabase.table("api_cost_log").select(
            "service, cost_per_unit, records_processed"
        ).gte("created_at", today).execute().data or []

        guardrails = supabase.table("budget_guardrails").select("*").eq("active", True).execute().data or []

        mtd_by_service: dict = {}
        for row in month_rows:
            svc = row.get("service") or "unknown"
            mtd_by_service[svc] = mtd_by_service.get(svc, 0.0) + _row_cost(row)

        today_by_service: dict = {}
        for row in today_rows:
            svc = row.get("service") or "unknown"
            today_by_service[svc] = today_by_service.get(svc, 0.0) + _row_cost(row)

        alerts = []
        for g in guardrails:
            svc = g.get("service")
            period = g.get("period")
            limit = _safe_float(g.get("limit_amount"))
            action = g.get("action", "alert")

            spent = 0.0
            if period == "monthly":
                spent = mtd_by_service.get(svc, 0.0) if svc else sum(mtd_by_service.values())
            else:
                spent = today_by_service.get(svc, 0.0) if svc else sum(today_by_service.values())

            spent = round(spent, 6)
            pct_used = round((spent / limit * 100), 1) if limit else 0

            label = f"[{svc}]" if svc else "[global]"

            if spent >= limit:
                alerts.append({
                    "guardrail_id": g.get("id"),
                    "service": svc or "all",
                    "period": period,
                    "level": "over_limit",
                    "action": action,
                    "message": f"{label} {period} spend ${spent:.4f} exceeded limit of ${limit:.2f}",
                    "spend_usd": spent,
                    "limit_usd": limit,
                    "pct_used": pct_used,
                })
            elif pct_used >= 80:
                alerts.append({
                    "guardrail_id": g.get("id"),
                    "service": svc or "all",
                    "period": period,
                    "level": "warning",
                    "action": action,
                    "message": f"{label} {period} spend at {pct_used}% of ${limit:.2f} limit",
                    "spend_usd": spent,
                    "limit_usd": limit,
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
# GET /kjle/v1/budget/guardrails
# ─────────────────────────────────────────────────

@router.get("/budget/guardrails")
async def list_guardrails(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        result = supabase.table("budget_guardrails").select("*").order("created_at").execute()
        return {
            "total": len(result.data or []),
            "guardrails": result.data or [],
        }
    except Exception as e:
        logger.error(f"list_guardrails error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/budget/guardrails
# Upsert by service + period combination
# ─────────────────────────────────────────────────

@router.post("/budget/guardrails")
async def upsert_guardrail(payload: GuardrailCreate, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    if payload.period not in ("daily", "monthly"):
        raise HTTPException(status_code=400, detail="period must be 'daily' or 'monthly'")
    if payload.action not in ("alert", "pause"):
        raise HTTPException(status_code=400, detail="action must be 'alert' or 'pause'")
    if payload.limit_amount <= 0:
        raise HTTPException(status_code=400, detail="limit_amount must be greater than 0")

    try:
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "service": payload.service,
            "period": payload.period,
            "limit_amount": payload.limit_amount,
            "action": payload.action,
            "active": True,
        }

        # Find existing by service + period
        query = supabase.table("budget_guardrails").select("id").eq("period", payload.period)
        if payload.service:
            query = query.eq("service", payload.service)
        else:
            query = query.is_("service", "null")
        existing = query.execute()

        if existing.data:
            result = supabase.table("budget_guardrails").update(record).eq("id", existing.data[0]["id"]).execute()
            operation = "updated"
        else:
            record["created_at"] = now
            result = supabase.table("budget_guardrails").insert(record).execute()
            operation = "created"

        return {"operation": operation, "guardrail": result.data[0] if result.data else record}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"upsert_guardrail error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# DELETE /kjle/v1/budget/guardrails/{guardrail_id}
# Soft delete by id
# ─────────────────────────────────────────────────

@router.delete("/budget/guardrails/{guardrail_id}")
async def delete_guardrail(guardrail_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        existing = supabase.table("budget_guardrails").select("id").eq("id", guardrail_id).execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail=f"Guardrail '{guardrail_id}' not found")

        supabase.table("budget_guardrails").update({"active": False}).eq("id", guardrail_id).execute()
        return {"deleted": True, "guardrail_id": guardrail_id, "note": "Soft delete — set to inactive"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete_guardrail error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# GET /kjle/v1/budget/cost-log
# ─────────────────────────────────────────────────

@router.get("/budget/cost-log")
async def cost_log(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    service: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, description="ISO date e.g. 2026-03-01"),
    date_to: Optional[str] = Query(None, description="ISO date e.g. 2026-03-31"),
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
# Admin: delete current month cost log entries
# ─────────────────────────────────────────────────

@router.post("/budget/reset")
async def reset_mtd_costs(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    month_start = _month_start_iso()
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        result = supabase.table("api_cost_log").delete().gte(
            "created_at", month_start
        ).lte("created_at", now_iso).execute()

        return {
            "reset": True,
            "month": date.today().strftime("%Y-%m"),
            "entries_deleted": len(result.data) if result.data else 0,
        }

    except Exception as e:
        logger.error(f"reset_mtd_costs error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
