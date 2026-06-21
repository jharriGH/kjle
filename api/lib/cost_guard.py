"""
KJLE — Hard Budget Guardrails + Cost Logging
File: api/lib/cost_guard.py

Every paid-API call site in the KJLE codebase calls `check_budget()` BEFORE
spending money, and `log_cost()` AFTER a successful response. If `check_budget`
returns False the caller MUST abort — no retry, no queue, no soft-cap.

Caps are read LIVE from admin_settings at each call (no module-load caching),
so admins can tune limits without a redeploy.

Writes to `enrichment_cost_log` (authoritative) and `budget_halt_log`.
Legacy `api_cost_log` inserts elsewhere in the codebase are left untouched —
they coexist during the transition and do no harm.
"""
from __future__ import annotations

import httpx
import json
import logging
import os
from datetime import datetime, timezone, date, timedelta
from typing import Optional

from ..database import get_db

logger = logging.getLogger(__name__)


# ── Pricing constants ────────────────────────────────────────────────────────
# These are the reference rates used by helpers below. Per-call actuals are
# always derived from the response body when available (e.g. Anthropic usage
# tokens) — these constants are the per-unit price lookup table.
OUTSCRAPER_COST_PER_REVIEW   = 0.003   # spec — $0.003 per review returned
FIRECRAWL_COST_PER_SCRAPE    = 0.005   # conservative; tunable via future admin_setting
ANTHROPIC_SONNET4_INPUT_PER_MTOK  = 3.00   # USD per 1M input tokens
ANTHROPIC_SONNET4_OUTPUT_PER_MTOK = 15.00  # USD per 1M output tokens


# ── Admin-settings key mapping ───────────────────────────────────────────────
SERVICE_TO_CAP_KEY = {
    "outscraper":    "daily_outscraper_cap_usd",
    "firecrawl":     "daily_firecrawl_cap_usd",
    "anthropic":     "daily_anthropic_cap_usd",
    "realvalidito":  "daily_realvalidito_cap_usd",
    "website_audit": "daily_audit_cap_usd",
}
TOTAL_CAP_KEY   = "daily_total_api_cap_usd"
MONTHLY_CAP_KEY = "monthly_total_api_cap_usd"


# Default caps used ONLY if admin_settings lookup fails (DB down etc).
# Intentionally conservative — if the DB can't be reached, be cheap.
_DEFAULT_CAPS = {
    "outscraper":    30.00,
    "firecrawl":     10.00,
    "anthropic":     10.00,
    "realvalidito":   3.00,
    "website_audit":  5.00,
    "_total_":       50.00,
    "_monthly_":    500.00,
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def _today_start_iso() -> str:
    """Start of today in UTC, ISO format."""
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return start.isoformat()


def _month_start_iso() -> str:
    return date.today().replace(day=1).isoformat()


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _get_cap(key: str, fallback: float) -> float:
    """Read a numeric cap from admin_settings. Never raises."""
    try:
        db = get_db()
        res = db.table("admin_settings").select("value").eq("key", key).execute()
        if res.data and res.data[0].get("value") is not None:
            return float(res.data[0]["value"])
    except Exception as e:
        logger.warning(f"cost_guard: failed to read {key} from admin_settings: {e}")
    return fallback


def _get_today_spend(service: Optional[str] = None) -> float:
    """
    Sum enrichment_cost_log.cost_usd for today (UTC calendar day).
    If service is None, returns total across all services.
    """
    try:
        db = get_db()
        q = db.table("enrichment_cost_log").select("cost_usd").gte("occurred_at", _today_start_iso())
        if service:
            q = q.eq("service", service)
        rows = q.execute().data or []
        return round(sum(_safe_float(r.get("cost_usd")) for r in rows), 6)
    except Exception as e:
        logger.critical(
            f"cost_guard: failed to read today's spend (service={service}): {e} "
            "— returning inf to fail-closed (cap treated as exhausted)"
        )
        return float("inf")


def _get_month_spend() -> float:
    try:
        db = get_db()
        rows = (
            db.table("enrichment_cost_log")
            .select("cost_usd")
            .gte("occurred_at", _month_start_iso())
            .execute()
            .data or []
        )
        return round(sum(_safe_float(r.get("cost_usd")) for r in rows), 6)
    except Exception as e:
        logger.critical(
            f"cost_guard: failed to read month spend: {e} "
            "— returning inf to fail-closed (cap treated as exhausted)"
        )
        return float("inf")


# ── Public API ───────────────────────────────────────────────────────────────
async def check_budget(
    service: str,
    estimated_cost_usd: float,
    job_name: str,
    leads_affected: int = 0,
) -> bool:
    """
    Returns True if the caller is safe to make the paid call.
    Returns False (AND writes budget_halt_log) if any cap would be breached.

    Callers MUST honor the return value. No retry, no queue.
    """
    if estimated_cost_usd <= 0:
        return True

    # Per-service daily cap
    svc_cap_key = SERVICE_TO_CAP_KEY.get(service)
    if svc_cap_key:
        svc_cap = _get_cap(svc_cap_key, _DEFAULT_CAPS.get(service, 10.00))
        svc_spent = _get_today_spend(service)
        if svc_spent + estimated_cost_usd > svc_cap:
            await log_halt(
                job_name=job_name,
                service=service,
                requested_operation=f"estimated ${estimated_cost_usd:.5f}",
                current_spend=svc_spent,
                cap_value=svc_cap,
                leads_affected=leads_affected,
                metadata={"breach": "per_service_daily"},
            )
            return False

    # Global daily cap
    total_cap = _get_cap(TOTAL_CAP_KEY, _DEFAULT_CAPS["_total_"])
    total_spent = _get_today_spend(None)
    if total_spent + estimated_cost_usd > total_cap:
        await log_halt(
            job_name=job_name,
            service=service,
            requested_operation=f"estimated ${estimated_cost_usd:.5f}",
            current_spend=total_spent,
            cap_value=total_cap,
            leads_affected=leads_affected,
            metadata={"breach": "total_daily"},
        )
        return False

    # Monthly cap
    monthly_cap = _get_cap(MONTHLY_CAP_KEY, _DEFAULT_CAPS["_monthly_"])
    month_spent = _get_month_spend()
    if month_spent + estimated_cost_usd > monthly_cap:
        await log_halt(
            job_name=job_name,
            service=service,
            requested_operation=f"estimated ${estimated_cost_usd:.5f}",
            current_spend=month_spent,
            cap_value=monthly_cap,
            leads_affected=leads_affected,
            metadata={"breach": "monthly_total"},
        )
        return False

    return True


async def log_cost(
    stage: str,
    service: str,
    cost_usd: float,
    lead_id: Optional[str] = None,       # leads.id is UUID in this repo — keep as TEXT
    items_fetched: Optional[int] = None,
    tokens_used: Optional[int] = None,
    job_run_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Insert one row into enrichment_cost_log. Swallows all exceptions."""
    try:
        db = get_db()
        row = {
            "stage":         stage,
            "service":       service,
            "cost_usd":      round(float(cost_usd), 5),
            "lead_id":       lead_id,
            "items_fetched": items_fetched,
            "tokens_used":   tokens_used,
            "job_run_id":    job_run_id,
            "metadata":      metadata,
            "occurred_at":   datetime.now(timezone.utc).isoformat(),
        }
        db.table("enrichment_cost_log").insert(row).execute()
    except Exception as e:
        logger.critical(
            f"cost_guard.log_cost FAILED — spend row NOT recorded "
            f"(stage={stage}, service={service}, cost=${cost_usd:.5f}): {e}"
        )


async def log_halt(
    job_name: str,
    service: str,
    requested_operation: str,
    current_spend: float,
    cap_value: float,
    leads_affected: int,
    metadata: Optional[dict] = None,
) -> None:
    """Insert one row into budget_halt_log. Swallows all exceptions."""
    _insert_ok = False
    try:
        db = get_db()
        row = {
            "job_name":                 job_name,
            "service":                  service,
            "requested_operation":      requested_operation,
            "current_daily_spend_usd":  round(float(current_spend), 5),
            "cap_value_usd":            round(float(cap_value), 5),
            "leads_affected":           int(leads_affected or 0),
            "metadata":                 metadata,
            "occurred_at":              datetime.now(timezone.utc).isoformat(),
        }
        db.table("budget_halt_log").insert(row).execute()
        logger.warning(
            f"BUDGET HALT [{service}] job={job_name} spent=${current_spend:.4f} cap=${cap_value:.2f}"
        )
        _insert_ok = True
    except Exception as e:
        logger.error(f"cost_guard.log_halt failed (service={service}): {e}")

    if not _insert_ok:
        return

    # ── SMS notification ──────────────────────────────────────────────────────
    try:
        # 1. Throttle: skip if another halt for this service fired in the last 60 min.
        #    The row just inserted is included in the query; len > 1 means a prior halt exists.
        sixty_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        db = get_db()
        prior = (
            db.table("budget_halt_log")
            .select("occurred_at")
            .eq("service", service)
            .gte("occurred_at", sixty_min_ago)
            .limit(2)
            .execute()
            .data or []
        )
        if len(prior) > 1:
            pass  # throttled — halt row still recorded, SMS skipped
        else:
            # 2. Read env vars; skip if any missing.
            sid    = os.environ.get("TWILIO_ACCOUNT_SID", "")
            token  = os.environ.get("TWILIO_AUTH_TOKEN", "")
            sender = os.environ.get("TWILIO_SENDER", "")
            to_num = os.environ.get("HALT_ALERT_SMS_TO", "")
            if not all([sid, token, sender, to_num]):
                logger.warning(
                    f"cost_guard: TWILIO env vars not fully set — skipping halt SMS (service={service})"
                )
            else:
                # 3. Pick the admin_settings lever key from the breach type.
                breach = (metadata or {}).get("breach", "unknown")
                if breach == "per_service_daily":
                    cap_key = SERVICE_TO_CAP_KEY.get(service, TOTAL_CAP_KEY)
                elif breach == "monthly_total":
                    cap_key = MONTHLY_CAP_KEY
                else:
                    cap_key = TOTAL_CAP_KEY

                # 4. Build concise SMS (<300 chars).
                sms_text = (
                    f"KJLE BUDGET HALT [{service}] {breach}: "
                    f"spent ${current_spend:.4f} vs cap ${cap_value:.2f} "
                    f"(job {job_name}). Lever: admin_settings.{cap_key}."
                )

                # 5. Send via Twilio REST. Log status only — never log auth or body.
                resp = httpx.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                    data={"From": sender, "To": to_num, "Body": sms_text},
                    auth=(sid, token),
                    timeout=10.0,
                )
                if resp.status_code // 100 != 2:
                    logger.warning(
                        f"cost_guard: Twilio SMS non-2xx status={resp.status_code} (service={service})"
                    )
                else:
                    logger.info(
                        f"cost_guard: halt SMS sent status={resp.status_code} (service={service})"
                    )
    except Exception as e:
        logger.warning(f"cost_guard: halt SMS notify failed (service={service}): {e}")


# ── Pricing helpers (public — callers compute actuals, then log) ────────────
def anthropic_cost(input_tokens: int, output_tokens: int) -> float:
    """USD cost for a Claude Sonnet 4 call given token counts from response.usage."""
    in_cost  = (max(input_tokens,  0) / 1_000_000) * ANTHROPIC_SONNET4_INPUT_PER_MTOK
    out_cost = (max(output_tokens, 0) / 1_000_000) * ANTHROPIC_SONNET4_OUTPUT_PER_MTOK
    return round(in_cost + out_cost, 6)


def outscraper_cost(review_count: int) -> float:
    """USD cost for one Outscraper call given review count from response."""
    return round(max(review_count or 0, 0) * OUTSCRAPER_COST_PER_REVIEW, 6)
