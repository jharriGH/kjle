"""
KJLE — Daily Cost Report builder
File: api/lib/daily_report.py

Gathers data for the rolling 24h window ending at `fire_time_utc` and returns
(subject, body_text). Called by scheduler.job_daily_cost_report; no side effects.

Data sources:
  - leads (counts + email coverage)
  - enrichment_cost_log (new hard-cap ledger)
  - scheduler_log (pipeline throughput)
  - budget_halt_log (anomalies)
  - campaign_activity helper (graceful-empty if parallel session not landed)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Tuple

from ..database import get_db
from .cost_guard import (
    MONTHLY_CAP_KEY,
    SERVICE_TO_CAP_KEY,
    TOTAL_CAP_KEY,
)
from .campaign_activity import get_kjle_campaign_activity_summary

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _get_setting(key: str, default: str) -> str:
    try:
        db = get_db()
        res = db.table("admin_settings").select("value").eq("key", key).execute()
        if res.data and res.data[0].get("value") is not None:
            return str(res.data[0]["value"])
    except Exception as e:
        logger.warning(f"daily_report: admin_settings read failed for {key}: {e}")
    return default


def _get_cap(key: str, fallback: float) -> float:
    try:
        return float(_get_setting(key, str(fallback)))
    except ValueError:
        return fallback


def _sum_cost(rows) -> float:
    return round(sum(_safe_float(r.get("cost_usd")) for r in (rows or [])), 5)


def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}" if abs(v) >= 1 else f"${v:.4f}"


def _pct(numer: float, denom: float) -> Optional[float]:
    if not denom:
        return None
    return round(numer / denom * 100, 1)


# ── Section builders ─────────────────────────────────────────────────────────
def _leads_section(now: datetime) -> Tuple[str, int]:
    """Returns (section_text, total_leads) — total used for anomaly checks."""
    db = get_db()
    try:
        all_rows = (
            db.table("leads")
            .select("id, segment_label, email, email_valid, is_active, created_at")
            .eq("is_active", True)
            .limit(200_000)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"daily_report: leads fetch failed: {e}")
        return ("LEADS\n(unavailable)\n", 0)

    total = len(all_rows)
    hot = sum(1 for r in all_rows if (r.get("segment_label") or "").lower() == "hot")
    warm = sum(1 for r in all_rows if (r.get("segment_label") or "").lower() == "warm")
    cold = sum(1 for r in all_rows if (r.get("segment_label") or "").lower() == "cold")
    unclassified = total - hot - warm - cold

    with_email = sum(1 for r in all_rows if (r.get("email") or "").strip())
    email_cov = _pct(with_email, total)

    # Delta vs 24h ago: count rows created in the last 24h.
    cutoff = (now - timedelta(hours=24)).isoformat()
    delta_new = sum(1 for r in all_rows if (r.get("created_at") or "") >= cutoff)

    body = (
        "LEADS\n"
        f"Total: {total:,} (Δ last 24h: +{delta_new:,})\n"
        f"HOT: {hot:,} / WARM: {warm:,} / COLD: {cold:,} / Unclassified: {unclassified:,}\n"
        f"Email coverage: {email_cov if email_cov is not None else 'n/a'}%\n"
    )
    return body, total


def _spend_section(since: datetime) -> Tuple[str, float, dict]:
    """Returns (section_text, total_spent, by_service_dict)."""
    db = get_db()
    try:
        rows = (
            db.table("enrichment_cost_log")
            .select("service, stage, cost_usd, lead_id")
            .gte("occurred_at", since.isoformat())
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"daily_report: enrichment_cost_log fetch failed: {e}")
        return ("SPEND LAST 24H\n(unavailable)\n", 0.0, {})

    by_service: dict = {}
    leads_by_service: dict = {}
    for r in rows:
        svc = r.get("service") or "unknown"
        by_service[svc] = by_service.get(svc, 0.0) + _safe_float(r.get("cost_usd"))
        if r.get("lead_id"):
            leads_by_service.setdefault(svc, set()).add(r["lead_id"])

    total = round(sum(by_service.values()), 5)

    lines = ["SPEND LAST 24H", f"Total: {_fmt_usd(total)}"]
    for svc in ("outscraper", "firecrawl", "anthropic"):
        spent = by_service.get(svc, 0.0)
        lead_n = len(leads_by_service.get(svc, set()))
        if spent > 0 and lead_n > 0:
            cpl = spent / lead_n
            lines.append(f"{svc.capitalize()}: {_fmt_usd(spent)} ({lead_n} leads, {_fmt_usd(cpl)}/lead)")
        else:
            lines.append(f"{svc.capitalize()}: {_fmt_usd(spent)}")
    return "\n".join(lines) + "\n", total, by_service


def _mtd_section(now: datetime) -> Tuple[str, float]:
    db = get_db()
    month_start = date(now.year, now.month, 1).isoformat()
    try:
        rows = (
            db.table("enrichment_cost_log")
            .select("cost_usd")
            .gte("occurred_at", month_start)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"daily_report: MTD fetch failed: {e}")
        return ("MONTH TO DATE\n(unavailable)\n", 0.0)

    mtd = _sum_cost(rows)
    cap = _get_cap(MONTHLY_CAP_KEY, 500.0)
    pct = _pct(mtd, cap)

    # Linear projection to month-end
    days_elapsed = now.day
    days_in_month = (date(now.year + (now.month // 12), (now.month % 12) + 1, 1) - timedelta(days=1)).day
    projected = round((mtd / days_elapsed) * days_in_month, 2) if days_elapsed > 0 else 0

    body = (
        "MONTH TO DATE\n"
        f"{_fmt_usd(mtd)} / {_fmt_usd(cap)} ({pct if pct is not None else 'n/a'}% used)\n"
        f"Projected month-end: {_fmt_usd(projected)}\n"
    )
    return body, mtd


def _pipeline_section(since: datetime) -> str:
    db = get_db()
    try:
        rows = (
            db.table("scheduler_log")
            .select("job_name, leads_processed, ran_at, status")
            .gte("ran_at", since.isoformat())
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"daily_report: scheduler_log fetch failed: {e}")
        return "PIPELINE\n(unavailable)\n"

    totals: dict = {}
    for r in rows:
        j = r.get("job_name") or "unknown"
        totals[j] = totals.get(j, 0) + int(r.get("leads_processed") or 0)

    return (
        "PIPELINE\n"
        f"Stage1 processed: {totals.get('enrich_stage1', 0):,}\n"
        f"Stage3 processed: {totals.get('enrich_stage3_nightly', 0):,}\n"
        f"Stage4 processed: {totals.get('enrich_stage4_nightly', 0):,}\n"
        f"Classifier processed: {totals.get('classify_segments', 0):,}\n"
    )


def _campaigns_section(since: datetime) -> str:
    # Integration shim — returns [] if parallel session's campaign_performance
    # table doesn't exist yet. Neither session blocks the other.
    rows = get_kjle_campaign_activity_summary(since)
    if not rows:
        return "KJLE CAMPAIGNS\n(no campaign data yet)\n"

    lines = ["KJLE CAMPAIGNS (last 24h)"]
    for r in rows[:10]:
        lines.append(
            f"{r.get('campaign_name','?')}: "
            f"sent={r.get('sent',0)} opened={r.get('opened',0)} "
            f"replied={r.get('replied',0)} booked={r.get('booked',0)}"
        )
    return "\n".join(lines) + "\n"


def _anomalies_section(since: datetime, today_by_service: dict) -> str:
    db = get_db()
    lines = ["ANOMALIES"]
    any_flag = False

    # 1. Budget halts in last 24h
    try:
        halts = (
            db.table("budget_halt_log")
            .select("service, occurred_at, leads_affected")
            .gte("occurred_at", since.isoformat())
            .execute()
            .data or []
        )
        if halts:
            any_flag = True
            lines.append(f"- {len(halts)} budget halt(s) in last 24h — check budget_halt_log for details")
    except Exception as e:
        logger.warning(f"daily_report: halt lookup failed: {e}")

    # 2. WoW cost spike (>2x same weekday last week)
    try:
        wk_ago_start = since - timedelta(days=7)
        wk_ago_end   = since - timedelta(days=6)
        wow_rows = (
            db.table("enrichment_cost_log")
            .select("service, cost_usd")
            .gte("occurred_at", wk_ago_start.isoformat())
            .lt("occurred_at",  wk_ago_end.isoformat())
            .execute()
            .data or []
        )
        wow_by_svc: dict = {}
        for r in wow_rows:
            svc = r.get("service") or "unknown"
            wow_by_svc[svc] = wow_by_svc.get(svc, 0.0) + _safe_float(r.get("cost_usd"))
        for svc, today_spent in today_by_service.items():
            last_week = wow_by_svc.get(svc, 0.0)
            if last_week > 0.01 and today_spent > 2 * last_week:
                any_flag = True
                lines.append(
                    f"- {svc}: spend jumped {today_spent / last_week:.1f}x vs same weekday last week "
                    f"({_fmt_usd(last_week)} -> {_fmt_usd(today_spent)})"
                )
    except Exception as e:
        logger.warning(f"daily_report: WoW comparison failed: {e}")

    if not any_flag:
        lines.append("all quiet")
    return "\n".join(lines) + "\n"


# ── Public API ───────────────────────────────────────────────────────────────
def build_daily_report(fire_time_utc: Optional[datetime] = None) -> Tuple[str, str]:
    """
    Build (subject, body_text) for the daily cost report.
    Window: rolling 24h ending at fire_time_utc (default = now).
    """
    now = fire_time_utc or datetime.now(timezone.utc)
    since = now - timedelta(hours=24)

    leads_body, _ = _leads_section(now)
    spend_body, total_spent, by_service = _spend_section(since)
    mtd_body, _ = _mtd_section(now)
    pipeline_body = _pipeline_section(since)
    campaigns_body = _campaigns_section(since)
    anomalies_body = _anomalies_section(since, by_service)

    subject = f"KJLE Daily Report — {now.strftime('%Y-%m-%d')} — {_fmt_usd(total_spent)} spent"

    footer = f"\nCovers last 24h ending {now.strftime('%Y-%m-%d %H:%M UTC')}."

    body = "\n".join([
        leads_body,
        spend_body,
        mtd_body,
        pipeline_body,
        campaigns_body,
        anomalies_body,
        footer,
    ])

    return subject, body
