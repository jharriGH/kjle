"""
KJLE — Searchbug balance threshold monitor
File: api/lib/searchbug_balance_monitor.py

Fires graduated email alerts when the Searchbug prepaid balance crosses
configured thresholds. Anti-spam: each (threshold, UTC date) pair alerts
at most once.

Thresholds (highest → lowest):
    $10.00  — LOW       (top up soon)
    $ 3.00  — CRITICAL  (top up immediately)
    $ 0.50  — EMERGENCY (effectively zero; fail-closed mode imminent)

If balance jumps down past multiple thresholds in one lookup (e.g., $15 → $0.40
on a single call), every applicable threshold that hasn't already alerted
today fires its own alert email — so the operator can't miss the severity.

Triggers:
  1. searchbug_provider.py — after every successful fresh lookup (real-time)
  2. scheduler.job_daily_cost_report — daily check (catches days with zero
     fresh lookups but a previously-recorded low balance; belt-and-suspenders)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# (threshold_usd, severity_label) — ordered highest to lowest
THRESHOLDS: list[tuple[float, str]] = [
    (10.00, "LOW"),
    (3.00,  "CRITICAL"),
    (0.50,  "EMERGENCY"),
]

# Email recipient/sender fall back to the daily_cost_report defaults when their
# admin_settings rows are unset. Same operator inbox; same KJLE branding.
_DEFAULT_RECIPIENT = "sales@mobilewebmds.com"
_DEFAULT_SENDER    = "KJLE Reports <kjle@kjreportz.com>"

_TOPUP_URL = "https://www.searchbug.com/services/payment-plans-discounts.aspx"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _flag_key(threshold: float, today: str) -> str:
    return f"searchbug_alert_sent_{threshold:.2f}_{today}"


def _read_admin_setting(key: str, default: str = "") -> str:
    try:
        from ..database import get_db
        db = get_db()
        res = db.table("admin_settings").select("value").eq("key", key).execute()
        if res.data and res.data[0].get("value") is not None:
            return str(res.data[0]["value"])
    except Exception as e:
        logger.warning(f"balance_monitor: admin_settings read failed for {key}: {e}")
    return default


def _upsert_admin_setting(key: str, value: str) -> None:
    try:
        from ..database import get_db
        db = get_db()
        db.table("admin_settings").upsert(
            {"key": key, "value": value, "updated_at": _now_iso()},
            on_conflict="key",
        ).execute()
    except Exception as e:
        logger.warning(f"balance_monitor: admin_settings upsert failed for {key}: {e}")


def _audit_alert(threshold: float, severity: str, balance: float) -> None:
    """Write a balance_alert row to dnc_audit_log for traceability."""
    try:
        from ..database import get_db
        db = get_db()
        db.table("dnc_audit_log").insert({
            "phone":       None,
            "source":      "balance_monitor",
            "result":      "balance_alert",
            "is_dnc":      None,
            "cost_usd":    0.0,
            "metadata": {
                "threshold": threshold,
                "balance":   balance,
                "severity":  severity,
            },
            "occurred_at": _now_iso(),
        }).execute()
    except Exception as e:
        logger.warning(f"balance_monitor: audit insert failed: {e}")


def _build_alert_email(threshold: float, severity: str, balance: float) -> tuple[str, str]:
    """Return (subject, body_text) for a single threshold alert."""
    icon = {"LOW": "🟡", "CRITICAL": "🔴", "EMERGENCY": "🚨"}.get(severity, "⚠️")
    subject = f"{icon} KJLE Alert: Searchbug balance ${balance:.2f} — {severity}"

    impact = (
        "If balance hits zero, all DNC checks fail-closed and Telehealth bridge "
        "submissions will be rejected (no contact attempts). Apps calling "
        "/kjle/v1/dnc/check will return is_dnc=true with error='provider_error' "
        "until the balance is restored."
    )

    lines = [
        f"Searchbug prepaid balance has dropped below the {severity} threshold (${threshold:.2f}).",
        "",
        f"Current balance: ${balance:.2f}",
        f"Threshold:       ${threshold:.2f} ({severity})",
        f"Account:         12729833",
        "",
        "ACTION REQUIRED:",
        f"  Top up at: {_TOPUP_URL}",
        "",
        "IMPACT:",
        f"  {impact}",
        "",
        "This alert fires at most once per threshold per UTC day.",
        "Lower-threshold alerts (CRITICAL/EMERGENCY) will follow if balance keeps falling.",
    ]
    return subject, "\n".join(lines)


async def check_and_alert_balance(balance: Optional[float] = None) -> dict:
    """
    Check the current Searchbug balance against configured thresholds and fire
    one email alert per threshold tripped that hasn't already alerted today.

    Args:
        balance: explicit balance (typically from a fresh lookup response).
                 If None, reads admin_settings.searchbug_balance_last.

    Returns:
        dict with keys:
            balance         (float|None) — value used for the check
            alerts_sent     (int)        — number of threshold emails sent
            thresholds_fired (list[float]) — which thresholds tripped
            reason          (str|None)   — set when alerts_sent=0 and there's a
                                           non-trivial reason (e.g., 'no_balance',
                                           'email_not_configured', 'all_above')
    """
    # Resolve balance — caller-provided or persisted
    if balance is None:
        bal_str = _read_admin_setting("searchbug_balance_last", "")
        if not bal_str:
            return {
                "balance": None, "alerts_sent": 0, "thresholds_fired": [],
                "reason": "no_balance_recorded",
            }
        try:
            balance = float(bal_str)
        except (ValueError, TypeError):
            return {
                "balance": None, "alerts_sent": 0, "thresholds_fired": [],
                "reason": "balance_unparseable",
            }

    # Lazy-import email_sender so this module has no hard dep on httpx at import
    from .email_sender import is_configured, send_email

    if not is_configured():
        logger.info("balance_monitor: RESEND_API_KEY not set — alerts skipped")
        return {
            "balance": balance, "alerts_sent": 0, "thresholds_fired": [],
            "reason": "email_not_configured",
        }

    today = _today_utc()
    recipient = (_read_admin_setting("daily_cost_report_email", "") or _DEFAULT_RECIPIENT).strip()
    sender    = (_read_admin_setting("daily_cost_report_sender", "") or _DEFAULT_SENDER).strip()

    alerts_sent = 0
    thresholds_fired: list[float] = []

    # Iterate highest → lowest. Fire each tripped threshold that hasn't alerted today.
    for threshold, severity in THRESHOLDS:
        if balance > threshold:
            continue
        key = _flag_key(threshold, today)
        if _read_admin_setting(key, ""):
            continue   # already alerted today at this threshold

        subject, body = _build_alert_email(threshold, severity, balance)

        try:
            result = await send_email(to=recipient, subject=subject, body_text=body, from_addr=sender)
        except Exception as e:
            logger.error(f"balance_monitor: send_email raised for threshold {threshold}: {e}")
            continue

        if not result.get("ok"):
            logger.error(
                f"balance_monitor: send failed for threshold {threshold}: {result.get('error')}"
            )
            continue

        # Success — set the daily flag, audit, count
        _upsert_admin_setting(key, "sent")
        _audit_alert(threshold, severity, balance)
        alerts_sent += 1
        thresholds_fired.append(threshold)
        logger.info(
            f"balance_monitor: sent {severity} alert (threshold ${threshold:.2f}, "
            f"balance ${balance:.2f}) email_id={result.get('id')}"
        )

    if alerts_sent == 0 and not thresholds_fired:
        reason = "all_above" if balance > THRESHOLDS[0][0] else "all_already_alerted_today"
    else:
        reason = None

    return {
        "balance":          balance,
        "alerts_sent":      alerts_sent,
        "thresholds_fired": thresholds_fired,
        "reason":           reason,
    }
