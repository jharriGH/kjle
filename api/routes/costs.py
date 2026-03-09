"""
Cost Counter + Budget Guardrails
KJLE - King James Lead Empire
Routes: /kjle/v1/costs/*

Tracks all API spend across enrichment stages, detects anomalies,
manages budget guardrails, and calculates ROI.
"""

import logging
from datetime import datetime, timezone, date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

STAGE_COSTS = {
    0: 0.000,
    1: 0.000,
    2: 0.000,
    3: 0.002,
    4: 0.005,
}

ANOMALY_THRESHOLD_PCT = 20.0   # flag if today > rolling_avg * 1.20


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class GuardrailUpsertRequest(BaseModel):
    name: str
    period: str                # "daily" | "monthly"
    limit_amount: float
    action: str                # "warn" | "stop"
    active: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _today_iso() -> str:
    return date.today().isoformat()

def _week_start_iso() -> str:
    today = date.today()
    return (today - timedelta(days=today.weekday())).isoformat()

def _month_start_iso() -> str:
    today = date.today()
    return today.replace(day=1).isoformat()

def _days_ago_iso(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()

def _safe_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default

def _safe_int(val, default=0) -> int:
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# EXISTING — GET /costs/summary
# today / week / month totals
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/costs/summary")
async def get_cost_summary():
    """
    Returns total spend across three time windows:
    today, this week (Mon–Sun), and this calendar month.
    """
    db = get_db()

    today = _today_iso()
    week_start = _week_start_iso()
    month_start = _month_start_iso()

    def _sum(rows):
        return round(sum(_safe_float(r.get("cost_per_unit", 0)) * _safe_int(r.get("records_processed", 1)) for r in rows), 6)

    today_rows   = db.table("api_cost_log").select("cost_per_unit, records_processed").gte("created_at", today).execute().data or []
    week_rows    = db.table("api_cost_log").select("cost_per_unit, records_processed").gte("created_at", week_start).execute().data or []
    month_rows   = db.table("api_cost_log").select("cost_per_unit, records_processed").gte("created_at", month_start).execute().data or []
    running_rows = db.table("api_cost_log").select("cost_per_unit, records_processed").execute().data or []

    return {
        "status": "success",
        "summary": {
            "today_usd":        _sum(today_rows),
            "this_week_usd":    _sum(week_rows),
            "this_month_usd":   _sum(month_rows),
            "running_total_usd": _sum(running_rows),
        },
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# EXISTING — GET /costs/by-service
# Breakdown by API service name
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/costs/by-service")
async def get_costs_by_service():
    """
    Returns total spend and call count broken down by service name
    (e.g. pagespeed, yelp_search, outscraper, firecrawl).
    """
    db = get_db()

    rows = db.table("api_cost_log").select("service, cost_per_unit, records_processed").execute().data or []

    # Aggregate in Python — Supabase client doesn't support GROUP BY natively
    service_map: dict = {}
    for row in rows:
        svc = row.get("service") or "unknown"
        cost = _safe_float(row.get("cost_per_unit", 0)) * _safe_int(row.get("records_processed", 1), 1)
        if svc not in service_map:
            service_map[svc] = {"service": svc, "total_cost_usd": 0.0, "call_count": 0}
        service_map[svc]["total_cost_usd"] = round(service_map[svc]["total_cost_usd"] + cost, 6)
        service_map[svc]["call_count"] += 1

    services = sorted(service_map.values(), key=lambda x: x["total_cost_usd"], reverse=True)
    grand_total = round(sum(s["total_cost_usd"] for s in services), 6)

    return {
        "status": "success",
        "grand_total_usd": grand_total,
        "by_service": services,
    }


# ─────────────────────────────────────────────────────────────────────────────
# EXISTING — GET /costs/guardrails
# Returns current budget guardrail status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/costs/guardrails")
async def get_guardrails():
    """
    Returns all active budget guardrails with current spend vs limit status.
    """
    db = get_db()

    guardrails = db.table("budget_guardrails").select("*").eq("active", True).execute().data or []

    if not guardrails:
        return {"status": "success", "guardrails": [], "message": "No active guardrails configured"}

    today = _today_iso()
    month_start = _month_start_iso()

    # Get today and month spend for evaluation
    today_rows  = db.table("api_cost_log").select("cost_per_unit, records_processed").gte("created_at", today).execute().data or []
    month_rows  = db.table("api_cost_log").select("cost_per_unit, records_processed").gte("created_at", month_start).execute().data or []

    def _sum(rows):
        return round(sum(_safe_float(r.get("cost_per_unit", 0)) * _safe_int(r.get("records_processed", 1), 1) for r in rows), 6)

    today_spend = _sum(today_rows)
    month_spend = _sum(month_rows)

    results = []
    for g in guardrails:
        period = g.get("period", "daily")
        limit = _safe_float(g.get("limit_amount", 0))
        current = today_spend if period == "daily" else month_spend
        pct_used = round((current / limit * 100) if limit > 0 else 0, 2)
        breached = current >= limit

        results.append({
            "name":          g.get("name"),
            "period":        period,
            "limit_usd":     limit,
            "current_usd":   current,
            "pct_used":      pct_used,
            "action":        g.get("action", "warn"),
            "breached":      breached,
            "status":        "🔴 BREACHED" if breached else ("🟡 WARNING" if pct_used >= 80 else "🟢 OK"),
        })

    return {
        "status": "success",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "guardrails": results,
    }


# ─────────────────────────────────────────────────────────────────────────────
# NEW — GET /costs/enrichment-funnel
# Cost and lead count at each enrichment stage
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/costs/enrichment-funnel")
async def get_enrichment_funnel():
    """
    Shows cost and lead count at each enrichment stage (0–4).
    Includes total spend, avg cost per lead, and funnel drop-off %.
    """
    db = get_db()

    # Lead counts per stage
    stage_counts = {}
    for stage in range(5):
        result = db.table("leads").select("id", count="exact").eq("enrichment_stage", stage).eq("is_active", True).execute()
        stage_counts[stage] = result.count if result.count is not None else 0

    # Spend per service (to map to stages)
    service_to_stage = {
        "stage1_scrape": 1,
        "pagespeed":     2,
        "yelp_search":   2,
        "outscraper":    3,
        "firecrawl":     4,
    }

    all_cost_rows = db.table("api_cost_log").select("service, cost_per_unit, records_processed").execute().data or []

    stage_spend: dict = {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
    for row in all_cost_rows:
        svc = row.get("service") or ""
        stage = service_to_stage.get(svc)
        if stage is not None:
            cost = _safe_float(row.get("cost_per_unit", 0)) * _safe_int(row.get("records_processed", 1), 1)
            stage_spend[stage] = round(stage_spend[stage] + cost, 6)

    stage0_count = stage_counts.get(0, 1) or 1  # avoid division by zero
    total_leads_all = sum(stage_counts.values()) or 1

    funnel = []
    for stage in range(5):
        count = stage_counts.get(stage, 0)
        spent = stage_spend.get(stage, 0.0)
        avg_cost = round(spent / count, 6) if count > 0 else 0.0
        # % of all leads that reached this stage
        pct_of_total = round(count / total_leads_all * 100, 2)

        funnel.append({
            "stage":              stage,
            "stage_label":        ["Unenriched", "Stage 1 (Free)", "Stage 2 (PageSpeed+Yelp)", "Stage 3 (Outscraper)", "Stage 4 (Firecrawl)"][stage],
            "lead_count":         count,
            "total_spent_usd":    spent,
            "avg_cost_per_lead":  avg_cost,
            "pct_of_total_leads": pct_of_total,
            "cost_per_record_reference": STAGE_COSTS[stage],
        })

    return {
        "status": "success",
        "total_active_leads": sum(stage_counts.values()),
        "total_enrichment_spend_usd": round(sum(stage_spend.values()), 6),
        "funnel": funnel,
    }


# ─────────────────────────────────────────────────────────────────────────────
# NEW — GET /costs/anomalies
# Detects daily spend anomalies vs 7-day rolling average
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/costs/anomalies")
async def get_spend_anomalies():
    """
    For each service, compares today's spend to the 7-day rolling average.
    Flags services where today's spend is >20% above the rolling average.
    """
    db = get_db()

    today = _today_iso()
    seven_days_ago = _days_ago_iso(7)

    today_rows      = db.table("api_cost_log").select("service, cost_per_unit, records_processed").gte("created_at", today).execute().data or []
    rolling_rows    = db.table("api_cost_log").select("service, cost_per_unit, records_processed").gte("created_at", seven_days_ago).lt("created_at", today).execute().data or []

    # Aggregate today's spend by service
    today_by_svc: dict = {}
    for row in today_rows:
        svc = row.get("service") or "unknown"
        cost = _safe_float(row.get("cost_per_unit", 0)) * _safe_int(row.get("records_processed", 1), 1)
        today_by_svc[svc] = round(today_by_svc.get(svc, 0.0) + cost, 6)

    # Aggregate rolling 7-day spend by service (avg per day)
    rolling_by_svc: dict = {}
    for row in rolling_rows:
        svc = row.get("service") or "unknown"
        cost = _safe_float(row.get("cost_per_unit", 0)) * _safe_int(row.get("records_processed", 1), 1)
        rolling_by_svc[svc] = round(rolling_by_svc.get(svc, 0.0) + cost, 6)

    # All services seen in either window
    all_services = set(today_by_svc.keys()) | set(rolling_by_svc.keys())

    anomalies = []
    for svc in sorted(all_services):
        today_spend = today_by_svc.get(svc, 0.0)
        rolling_total = rolling_by_svc.get(svc, 0.0)
        rolling_avg = round(rolling_total / 7, 6)  # per-day average

        if rolling_avg > 0:
            variance_pct = round(((today_spend - rolling_avg) / rolling_avg) * 100, 2)
        else:
            # No historical baseline — flag only if today has spend
            variance_pct = 100.0 if today_spend > 0 else 0.0

        is_anomaly = variance_pct > ANOMALY_THRESHOLD_PCT

        anomalies.append({
            "service":         svc,
            "today_spend_usd": today_spend,
            "rolling_avg_7d":  rolling_avg,
            "variance_pct":    variance_pct,
            "is_anomaly":      is_anomaly,
            "status":          "🔴 ANOMALY" if is_anomaly else "🟢 NORMAL",
        })

    # Sort anomalies first, then by variance desc
    anomalies.sort(key=lambda x: (-int(x["is_anomaly"]), -x["variance_pct"]))

    return {
        "status": "success",
        "anomaly_threshold_pct": ANOMALY_THRESHOLD_PCT,
        "anomaly_count": sum(1 for a in anomalies if a["is_anomaly"]),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "services": anomalies,
    }


# ─────────────────────────────────────────────────────────────────────────────
# NEW — POST /costs/guardrails
# Upsert a budget guardrail
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/costs/guardrails")
async def upsert_guardrail(body: GuardrailUpsertRequest):
    """
    Create or update a budget guardrail by name.
    Upserts into budget_guardrails table.
    """
    if body.period not in ("daily", "monthly"):
        raise HTTPException(status_code=422, detail="period must be 'daily' or 'monthly'")

    if body.action not in ("warn", "stop"):
        raise HTTPException(status_code=422, detail="action must be 'warn' or 'stop'")

    if body.limit_amount <= 0:
        raise HTTPException(status_code=422, detail="limit_amount must be greater than 0")

    db = get_db()

    payload = {
        "name":         body.name,
        "period":       body.period,
        "limit_amount": body.limit_amount,
        "action":       body.action,
        "active":       body.active,
        "updated_at":   datetime.now(timezone.utc).isoformat(),
    }

    # Check if guardrail exists by name
    existing = db.table("budget_guardrails").select("id").eq("name", body.name).execute().data or []

    if existing:
        db.table("budget_guardrails").update(payload).eq("name", body.name).execute()
        operation = "updated"
    else:
        payload["created_at"] = datetime.now(timezone.utc).isoformat()
        db.table("budget_guardrails").insert(payload).execute()
        operation = "created"

    return {
        "status": "success",
        "operation": operation,
        "guardrail": {
            "name":         body.name,
            "period":       body.period,
            "limit_usd":    body.limit_amount,
            "action":       body.action,
            "active":       body.active,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# NEW — DELETE /costs/guardrails/{name}
# Soft delete guardrail by name
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/costs/guardrails/{name}")
async def delete_guardrail(name: str):
    """
    Soft-deletes a budget guardrail by setting active=false.
    """
    db = get_db()

    existing = db.table("budget_guardrails").select("id, name, active").eq("name", name).execute().data or []

    if not existing:
        raise HTTPException(status_code=404, detail=f"Guardrail '{name}' not found")

    db.table("budget_guardrails").update({
        "active":     False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("name", name).execute()

    return {
        "status": "success",
        "message": f"Guardrail '{name}' has been deactivated (soft delete)",
        "name": name,
    }


# ─────────────────────────────────────────────────────────────────────────────
# NEW — GET /costs/roi
# ROI calculator
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/costs/roi")
async def get_roi(
    demos_sent: int = 0,
    close_rate_pct: float = 10.0,
    deal_value: float = 500.0,
):
    """
    ROI calculator. Uses total enrichment spend from api_cost_log as cost basis.
    Returns estimated revenue, ROI %, cost per demo, and break-even demo count.
    """
    if close_rate_pct < 0 or close_rate_pct > 100:
        raise HTTPException(status_code=422, detail="close_rate_pct must be between 0 and 100")

    if deal_value < 0:
        raise HTTPException(status_code=422, detail="deal_value must be >= 0")

    db = get_db()

    all_rows = db.table("api_cost_log").select("cost_per_unit, records_processed").execute().data or []
    total_cost = round(sum(
        _safe_float(r.get("cost_per_unit", 0)) * _safe_int(r.get("records_processed", 1), 1)
        for r in all_rows
    ), 6)

    close_rate = close_rate_pct / 100.0
    estimated_closes = demos_sent * close_rate
    estimated_revenue = round(estimated_closes * deal_value, 2)
    net_profit = round(estimated_revenue - total_cost, 2)
    roi_pct = round((net_profit / total_cost * 100) if total_cost > 0 else 0.0, 2)
    cost_per_demo = round(total_cost / demos_sent, 6) if demos_sent > 0 else 0.0

    # Break-even: how many demos needed so revenue >= total_cost
    # demos * close_rate * deal_value >= total_cost
    # demos >= total_cost / (close_rate * deal_value)
    if close_rate > 0 and deal_value > 0:
        break_even_demos = round(total_cost / (close_rate * deal_value), 1)
    else:
        break_even_demos = None

    return {
        "status": "success",
        "inputs": {
            "demos_sent":      demos_sent,
            "close_rate_pct":  close_rate_pct,
            "deal_value_usd":  deal_value,
        },
        "roi": {
            "total_enrichment_cost_usd": total_cost,
            "estimated_closes":          round(estimated_closes, 2),
            "estimated_revenue_usd":     estimated_revenue,
            "net_profit_usd":            net_profit,
            "estimated_roi_pct":         roi_pct,
            "cost_per_demo_usd":         cost_per_demo,
            "break_even_demos":          break_even_demos,
        },
        "interpretation": (
            f"At a {close_rate_pct}% close rate on {demos_sent} demos worth ${deal_value} each, "
            f"you'd need {break_even_demos} demos to recover ${total_cost} in enrichment costs."
            if break_even_demos is not None else
            "Set close_rate_pct and deal_value > 0 to calculate break-even."
        ),
    }
