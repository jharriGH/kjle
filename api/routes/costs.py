"""
KJLE API — Cost Intelligence Routes
GET /kjle/v1/costs/summary        — today/week/month totals
GET /kjle/v1/costs/by-service     — breakdown by API service
GET /kjle/v1/costs/guardrails     — budget guardrail status
"""
from fastapi import APIRouter
from datetime import datetime, timezone, timedelta
from ..database import get_db

router = APIRouter()


@router.get("/costs/summary")
async def costs_summary():
    db = get_db()
    now = datetime.now(timezone.utc)

    today_start   = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    week_start    = (now - timedelta(days=7)).isoformat()
    month_start   = now.replace(day=1, hour=0, minute=0, second=0).isoformat()

    def sum_costs(since: str) -> float:
        result = db.table("api_cost_log").select("total_cost").gte("created_at", since).execute()
        return round(sum(r["total_cost"] for r in result.data if r["total_cost"]), 4)

    # Fixed costs from monthly_fixed_costs table
    fixed = db.table("monthly_fixed_costs").select("monthly_amount").eq("active", True).execute()
    fixed_total = round(sum(r["monthly_amount"] for r in fixed.data), 2)

    return {
        "variable": {
            "today":        sum_costs(today_start),
            "last_7_days":  sum_costs(week_start),
            "this_month":   sum_costs(month_start),
        },
        "fixed_monthly": fixed_total,
        "total_estimated_monthly": round(sum_costs(month_start) + fixed_total, 2),
    }


@router.get("/costs/by-service")
async def costs_by_service():
    db = get_db()
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0).isoformat()

    result = db.table("api_cost_log").select("service, total_cost, records_processed").gte("created_at", month_start).execute()

    breakdown = {}
    for row in result.data:
        svc = row["service"]
        if svc not in breakdown:
            breakdown[svc] = {"service": svc, "total_cost": 0.0, "calls": 0, "records": 0}
        breakdown[svc]["total_cost"]  += row["total_cost"] or 0
        breakdown[svc]["calls"]       += 1
        breakdown[svc]["records"]     += row["records_processed"] or 0

    services = sorted(breakdown.values(), key=lambda x: x["total_cost"], reverse=True)
    for s in services:
        s["total_cost"] = round(s["total_cost"], 4)

    return {"this_month": services}


@router.get("/costs/guardrails")
async def costs_guardrails():
    db = get_db()
    now = datetime.now(timezone.utc)

    today_start  = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start  = now.replace(day=1, hour=0, minute=0, second=0).isoformat()

    today_result = db.table("api_cost_log").select("total_cost").gte("created_at", today_start).execute()
    month_result = db.table("api_cost_log").select("total_cost").gte("created_at", month_start).execute()

    today_spend = round(sum(r["total_cost"] for r in today_result.data if r["total_cost"]), 4)
    month_spend = round(sum(r["total_cost"] for r in month_result.data if r["total_cost"]), 4)

    guardrails = db.table("budget_guardrails").select("*").eq("active", True).execute()

    statuses = []
    for g in guardrails.data:
        spend = today_spend if g["period"] == "daily" else month_spend
        pct   = round((spend / g["limit_amount"]) * 100, 1) if g["limit_amount"] else 0
        statuses.append({
            "name":       g["name"],
            "period":     g["period"],
            "limit":      g["limit_amount"],
            "spent":      spend,
            "pct_used":   pct,
            "status":     "ok" if pct < 75 else "warning" if pct < 90 else "critical",
            "action":     g["action"],
        })

    return {"guardrails": statuses}
