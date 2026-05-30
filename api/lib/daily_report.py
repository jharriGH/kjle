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


def _format_searchbug_balance_line() -> str:
    """Render the Searchbug balance line with severity indicator."""
    raw = _get_setting("searchbug_balance_last", "")
    if not raw:
        return "Searchbug balance: not yet checked"
    try:
        bal = float(raw)
    except (ValueError, TypeError):
        return f"Searchbug balance: {raw} (unparseable)"

    if bal > 10:
        return f"Searchbug balance: ${bal:.2f} ✅"
    if bal > 3:
        return f"Searchbug balance: ${bal:.2f} ⚠️ LOW — top up soon"
    if bal > 0:
        return f"Searchbug balance: ${bal:.2f} 🔴 CRITICAL — top up immediately"
    return f"Searchbug balance: ${bal:.2f} 🚨 ZERO/NEGATIVE — fail-closed mode active"


def _tcpa_litigator_block(since: datetime) -> str:
    """
    Phase 4 Layer 3 Slice 3A — TCPA LITIGATOR PROTECTION sub-section.

    Appended below the DNC HEALTH block. Three lines when provisioned:
      List size:                N numbers
      Matches blocked (24h):    M
      Last refreshed:           YYYY-MM-DD HH:MM UTC (D days ago)

    When the table is empty (no subscription yet), block collapses to a
    single 'Status: not provisioned (no subscription)' line.

    Graceful degradation: if the migration hasn't run, the block is omitted
    entirely so the rest of the daily report still ships.
    """
    db = get_db()

    try:
        size_res = (
            db.table("tcpa_litigators")
            .select("phone", count="exact")
            .limit(1)
            .execute()
        )
        list_size = size_res.count or 0
    except Exception as e:
        logger.info(f"daily_report: tcpa_litigators unavailable ({e}); skipping TCPA block")
        return ""

    # Slice 3A.1: count rows harvested from Searchbug responses.
    try:
        harvest_res = (
            db.table("tcpa_litigators")
            .select("phone", count="exact")
            .like("source", "searchbug_harvest%")
            .limit(1)
            .execute()
        )
        harvested_count = harvest_res.count or 0
    except Exception:
        harvested_count = 0

    header = (
        "\nTCPA LITIGATOR PROTECTION\n"
        "-----\n"
    )
    footer = "-----\n"

    if list_size == 0:
        return (
            header
            + "Status: not provisioned (no subscription)\n"
            + "Status: not provisioned. Harvest from Searchbug active — table will populate\n"
            + "        organically as DNC checks fire on flagged phones.\n"
            + footer
        )

    # Matches in last 24h
    try:
        rows_24h = (
            db.table("dnc_audit_log")
            .select("result")
            .gte("occurred_at", since.isoformat())
            .eq("result", "tcpa_litigator_match")
            .execute()
            .data or []
        )
        matches_24h = len(rows_24h)
    except Exception as e:
        logger.warning(f"daily_report: tcpa matches 24h fetch failed: {e}")
        matches_24h = 0

    last_refreshed_raw = _get_setting("tcpa_list_last_refresh_at", "")
    # Slice 3A.1: when the refresh job has never populated the setting but the
    # table has rows, attribution is the organic Searchbug harvest.
    if not last_refreshed_raw and harvested_count > 0:
        last_refreshed_line = "organic harvest (Slice 3A.1)"
    else:
        last_refreshed_line = "never (refresh job has not run yet)"
    if last_refreshed_raw:
        try:
            s = last_refreshed_raw
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days_ago = (datetime.now(timezone.utc) - dt).days
            if days_ago == 0:
                ago = "today"
            elif days_ago == 1:
                ago = "1 day ago"
            else:
                ago = f"{days_ago} days ago"
            last_refreshed_line = f"{dt.strftime('%Y-%m-%d %H:%M UTC')} ({ago})"
        except Exception:
            last_refreshed_line = last_refreshed_raw

    return (
        header
        + f"List size:                    {list_size:,} numbers\n"
        + f"  └─ harvested from Searchbug: {harvested_count:,}\n"
        + f"Matches blocked (24h):        {matches_24h:,}\n"
        + f"Last refreshed:               {last_refreshed_line}\n"
        + footer
    )


def _carrier_pattern_block(since: datetime) -> str:
    """
    Phase 4 Layer 3 Slice 3B — CARRIER-PATTERN PROTECTION sub-section.

    Counts dnc_audit_log rows with result='carrier_pattern_blocked' over the
    24h and 7d windows. Estimates Searchbug cost saved at the published
    per-lookup rate ($0.0214). Pattern blocks are deterministic + free; this
    block makes the cumulative spend savings visible.

    Appended below the TCPA LITIGATOR PROTECTION block. Degrades gracefully:
    if dnc_audit_log can't be read, returns an empty string so the rest of
    the daily report still ships.
    """
    db = get_db()
    try:
        rows_24h = (
            db.table("dnc_audit_log")
            .select("result")
            .gte("occurred_at", since.isoformat())
            .eq("result", "carrier_pattern_blocked")
            .execute()
            .data or []
        )
    except Exception as e:
        logger.info(f"daily_report: carrier-pattern 24h fetch failed: {e}")
        return ""

    # since is the start of the 24h window — subtract 6 more days to get 7d total.
    cutoff_7d = (since - timedelta(hours=6 * 24)).isoformat()
    try:
        rows_7d = (
            db.table("dnc_audit_log")
            .select("result")
            .gte("occurred_at", cutoff_7d)
            .eq("result", "carrier_pattern_blocked")
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"daily_report: carrier-pattern 7d fetch failed: {e}")
        rows_7d = []

    blocks_24h  = len(rows_24h)
    blocks_7d   = len(rows_7d)
    savings_24h = 0.0214 * blocks_24h

    return (
        "\nCARRIER-PATTERN PROTECTION\n"
        "-----\n"
        f"Blocks (24h):  {blocks_24h:,}        (← free, deterministic)\n"
        f"Blocks (7d):   {blocks_7d:,}\n"
        f"Searchbug $ saved (est, 24h):  $0.0214 × {blocks_24h:,} = {_fmt_usd(savings_24h)}\n"
        "-----\n"
    )


def _dnc_section(since: datetime) -> str:
    """
    DNC HEALTH section. Reads from dnc_audit_log + dnc_cache + dnc_suppressions
    + admin_settings.searchbug_balance_last. Degrades gracefully — if any of
    those tables don't exist yet (migration not run), section becomes a single
    'idle/unprovisioned' line so the email still ships.

    Cap-exceeded events ('budget_cap*') are excluded from real-lookup totals
    and surfaced separately as 'halts'.
    """
    db = get_db()
    try:
        rows = (
            db.table("dnc_audit_log")
            .select("result, cost_usd, source, error")
            .gte("occurred_at", since.isoformat())
            .execute()
            .data or []
        )
    except Exception as e:
        logger.info(f"daily_report: dnc_audit_log unavailable ({e}); rendering idle DNC section")
        return "DNC HEALTH\n(table not yet provisioned — run dnc_schema.sql)\n"

    fresh = sum(1 for r in rows if r.get("result") == "fresh_lookup")
    hits  = sum(1 for r in rows if r.get("result") == "cache_hit")
    halts = sum(
        1 for r in rows
        if r.get("result") == "error" and (r.get("error") or "").startswith("budget_cap")
    )

    # Phase 4 Layer 2 Slice 2B — soft-TTL counters (24h)
    stale_hits     = sum(1 for r in rows if r.get("result") == "cache_hit_stale_served")
    bg_success_24h = sum(1 for r in rows if r.get("result") == "background_refresh_success")
    bg_failure_24h = sum(1 for r in rows if r.get("result") == "background_refresh_failure")
    cost = round(
        sum(
            _safe_float(r.get("cost_usd"))
            for r in rows
            if not (
                r.get("result") == "error"
                and (r.get("error") or "").startswith("budget_cap")
            )
        ),
        5,
    )

    try:
        cache_size = (
            db.table("dnc_cache").select("phone", count="exact").limit(1).execute().count or 0
        )
    except Exception:
        cache_size = 0

    src_counts: dict = {}
    for r in rows:
        s = r.get("source") or "unknown"
        src_counts[s] = src_counts.get(s, 0) + 1
    top_sources = sorted(src_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]
    sources_str = ", ".join(f"{s}={n}" for s, n in top_sources) if top_sources else "none"

    balance_line = _format_searchbug_balance_line()
    soft_ttl_lines = _dnc_soft_ttl_lines(
        since=since,
        stale_hits=stale_hits,
        bg_success_24h=bg_success_24h,
        bg_failure_24h=bg_failure_24h,
    )
    by_consumer_block = _dnc_by_consumer_block()
    tcpa_block = _tcpa_litigator_block(since)
    carrier_block = _carrier_pattern_block(since)

    if (fresh + hits + halts + stale_hits + bg_success_24h + bg_failure_24h) == 0:
        return (
            "DNC HEALTH\n"
            "DNC: idle (0 lookups in last 24h)\n"
            f"Cache size: {cache_size:,} phones\n"
            f"{balance_line}\n"
            f"{by_consumer_block}"
            f"{tcpa_block}"
            f"{carrier_block}"
        )

    total = fresh + hits
    return (
        "DNC HEALTH (last 24h)\n"
        f"Lookups: {fresh:,} fresh + {hits:,} cached = {total:,} total"
        + (f" | {halts:,} halts" if halts else "") + "\n"
        f"Cost: {_fmt_usd(cost)}\n"
        f"Cache size: {cache_size:,} phones\n"
        f"Top sources: {sources_str}\n"
        f"{soft_ttl_lines}"
        f"{balance_line}\n"
        f"{by_consumer_block}"
        f"{tcpa_block}"
        f"{carrier_block}"
    )


def _dnc_soft_ttl_lines(
    *,
    since: datetime,
    stale_hits: int,
    bg_success_24h: int,
    bg_failure_24h: int,
) -> str:
    """
    Phase 4 Layer 2 Slice 2B — soft-TTL counters injected into DNC HEALTH.

    Three lines appended below 'Top sources':
      Stale hits (24h):           N
      Background refreshes (24h): X successes / Y failures
      Refresh failure rate (7d):  Z%

    The 7d failure rate requires a separate query over the 7-day window.
    Degrades to '0%' if the window has zero attempts.
    """
    db = get_db()
    cutoff_7d = (since - timedelta(hours=6 * 24)).isoformat()  # since is 24h ago -> 7d total
    try:
        rows_7d = (
            db.table("dnc_audit_log")
            .select("result")
            .gte("occurred_at", cutoff_7d)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"daily_report: 7d refresh-failure fetch failed: {e}")
        rows_7d = []

    succ_7d = sum(1 for r in rows_7d if r.get("result") == "background_refresh_success")
    fail_7d = sum(1 for r in rows_7d if r.get("result") == "background_refresh_failure")
    denom_7d = succ_7d + fail_7d
    fail_rate = round(fail_7d / denom_7d * 100, 1) if denom_7d else 0.0

    return (
        f"Stale hits (24h): {stale_hits:,}\n"
        f"Background refreshes (24h): {bg_success_24h:,} successes / {bg_failure_24h:,} failures\n"
        f"Refresh failure rate (7d): {fail_rate}%\n"
    )


def _dnc_by_consumer_block() -> str:
    """
    Phase 4 Layer 2 Slice 2A — per-consumer DNC breakdown.
    Shares its SQL with /kjle/v1/dnc/stats/by-consumer via
    api.routes.dnc.per_consumer_breakdown (no duplicated query).
    """
    try:
        # Lazy import to avoid a circular module dependency at startup
        # (routes/dnc.py imports from api.lib; this is the reverse edge).
        from ..routes.dnc import per_consumer_breakdown
        snapshot = per_consumer_breakdown(period_hours=24)
    except Exception as e:
        logger.warning(f"daily_report: per_consumer_breakdown failed: {e}")
        return ""

    consumers = snapshot.get("consumers") or []
    totals = snapshot.get("totals") or {}

    if not consumers or (totals.get("calls") or 0) == 0:
        return "DNC by consumer: no activity in window.\n"

    name_w = max(13, max((len(c["consumer_app"]) for c in consumers), default=13))
    header = (
        f"{'consumer_app':<{name_w}}  {'calls':>6}  {'hit_rate':>8}  {'cost':>8}"
    )
    rows = [
        f"{c['consumer_app']:<{name_w}}  {c['calls']:>6,}  "
        f"{int(round(c['hit_rate_pct'])):>7}%  {_fmt_usd(c['cost_usd']):>8}"
        for c in consumers
    ]
    total_row = (
        f"{'total':<{name_w}}  {totals['calls']:>6,}  "
        f"{int(round(totals['hit_rate_pct'])):>7}%  {_fmt_usd(totals['cost_usd']):>8}"
    )
    sep = "-----"

    return (
        "\nDNC BY CONSUMER (last 24h)\n"
        f"{sep}\n"
        f"{header}\n"
        + "\n".join(rows) + "\n"
        f"{sep}\n"
        f"{total_row}\n"
    )


def _suppressions_section(since: datetime) -> str:
    """
    SUPPRESSIONS section. Reads dnc_suppressions + email_suppressions for the
    rolling 24h window, plus current totals from both tables.

    Graceful degradation: if email_suppressions table doesn't exist (Phase 3
    migration not run), the email line shows '(table not yet provisioned)'
    and the section still renders the phone half.

    Source breakdown caps at top 5; remainder shown as '...+ N more'.
    """
    db = get_db()

    # ── Phone suppressions ───────────────────────────────────────────────────
    try:
        phone_24h_res = (
            db.table("dnc_suppressions")
            .select("phone, source", count="exact")
            .gte("suppressed_at", since.isoformat())
            .execute()
        )
        phone_24h_rows  = phone_24h_res.data or []
        phone_24h_count = phone_24h_res.count if phone_24h_res.count is not None else len(phone_24h_rows)
    except Exception as e:
        logger.info(f"daily_report: phone suppressions 24h fetch failed: {e}")
        phone_24h_rows  = []
        phone_24h_count = 0

    try:
        phone_total = (
            db.table("dnc_suppressions").select("phone", count="exact").limit(1).execute().count or 0
        )
    except Exception:
        phone_total = 0

    # ── Email suppressions (Phase 3 — graceful degradation) ─────────────────
    email_table_missing = False
    try:
        email_24h_res = (
            db.table("email_suppressions")
            .select("email, source", count="exact")
            .gte("suppressed_at", since.isoformat())
            .execute()
        )
        email_24h_rows  = email_24h_res.data or []
        email_24h_count = email_24h_res.count if email_24h_res.count is not None else len(email_24h_rows)
    except Exception as e:
        logger.info(f"daily_report: email_suppressions unavailable ({e}); rendering partial section")
        email_24h_rows  = []
        email_24h_count = 0
        email_table_missing = True

    if email_table_missing:
        email_total = 0
    else:
        try:
            email_total = (
                db.table("email_suppressions").select("email", count="exact").limit(1).execute().count or 0
            )
        except Exception:
            email_total = 0

    # ── Source breakdown across both tables (24h window) ────────────────────
    src_counts: dict = {}
    for r in (phone_24h_rows + email_24h_rows):
        s = r.get("source") or "unknown"
        src_counts[s] = src_counts.get(s, 0) + 1
    sorted_sources = sorted(src_counts.items(), key=lambda kv: kv[1], reverse=True)
    top_5 = sorted_sources[:5]
    extra = len(sorted_sources) - 5
    sources_str = ", ".join(f"{s}={n}" for s, n in top_5) if top_5 else "none"
    if extra > 0:
        sources_str += f" ...+ {extra} more"

    lines = ["SUPPRESSIONS (last 24h)"]
    lines.append(f"Phone suppressions added: {phone_24h_count:,}")
    if email_table_missing:
        lines.append("Email suppressions: (table not yet provisioned — run email_suppressions.sql)")
    else:
        lines.append(f"Email suppressions added: {email_24h_count:,}")
    lines.append(f"By source: {sources_str}")
    lines.append(f"Total suppression list: {phone_total:,} phones, {email_total:,} emails")
    return "\n".join(lines) + "\n"


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
    dnc_body = _dnc_section(since)
    suppressions_body = _suppressions_section(since)
    anomalies_body = _anomalies_section(since, by_service)

    subject = f"KJLE Daily Report — {now.strftime('%Y-%m-%d')} — {_fmt_usd(total_spent)} spent"

    footer = f"\nCovers last 24h ending {now.strftime('%Y-%m-%d %H:%M UTC')}."

    body = "\n".join([
        leads_body,
        spend_body,
        mtd_body,
        pipeline_body,
        campaigns_body,
        dnc_body,
        suppressions_body,
        anomalies_body,
        footer,
    ])

    return subject, body
