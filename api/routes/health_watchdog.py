"""
KJLE — Pipeline Watchdog
File: api/routes/health_watchdog.py

GET  /kjle/v1/health/overview      — public, no auth (CORS handled by app-level middleware)
POST /kjle/v1/health/run-watchdog  — x-api-key protected
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from ..database import get_db
from ..lib.email_sender import send_email

logger = logging.getLogger(__name__)

router = APIRouter()

SCHEDULER_LOG_TABLE = "scheduler_log"
ALERT_RECIPIENT     = "jharricts@gmail.com"
API_SECRET_KEY      = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ─────────────────────────────────────────────────────────────────────────────
# Expected Jobs Registry
# campaign_sync_hourly and enrich_stage1 intentionally excluded (disabled).
# ─────────────────────────────────────────────────────────────────────────────

EXPECTED_JOBS = {
    "classify_segments":         {"label": "Classify Segments",           "max_age_hours": 7,   "allow_skipped": False},
    "email_clean_nightly":       {"label": "Email Clean (Nightly)",       "max_age_hours": 26,  "allow_skipped": False},
    "email_clean_poll_batches":  {"label": "Email Clean (Poll Batches)",  "max_age_hours": 2,   "allow_skipped": False},
    "stale_cleanup":             {"label": "Stale Cleanup",               "max_age_hours": 26,  "allow_skipped": False},
    "cost_digest":               {"label": "Cost Digest",                 "max_age_hours": 26,  "allow_skipped": False},
    "daily_cost_report":         {"label": "Daily Cost Report",           "max_age_hours": 26,  "allow_skipped": False},
    "website_audit_nightly":     {"label": "Website Audit (Nightly)",     "max_age_hours": 26,  "allow_skipped": False},
    "pagespeed_nightly":         {"label": "PageSpeed (Nightly)",         "max_age_hours": 26,  "allow_skipped": False},
    "axe_scan_nightly":          {"label": "Axe Scan (Nightly)",          "max_age_hours": 26,  "allow_skipped": False},
    "provider_classify_nightly": {"label": "Provider Classify (Nightly)", "max_age_hours": 26,  "allow_skipped": False},
    "rdap_domain_check":         {"label": "RDAP Domain Check",           "max_age_hours": 26,  "allow_skipped": False},
    "fed_dnc_refresh_monthly":   {"label": "FED DNC Refresh (Monthly)",   "max_age_hours": 744, "allow_skipped": True},
    "nanpa_refresh_monthly":     {"label": "NANPA Refresh (Monthly)",     "max_age_hours": 744, "allow_skipped": True},
    "tcpa_refresh_weekly":       {"label": "TCPA Refresh (Weekly)",       "max_age_hours": 192, "allow_skipped": True},
}


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_ts(val) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def _hours_ago(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


# ─────────────────────────────────────────────────────────────────────────────
# Health computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_health() -> dict:
    db    = get_db()
    items = []

    # Per-job checks — reads from the same scheduler_log source as /scheduler/status
    for job_name, cfg in EXPECTED_JOBS.items():
        label      = cfg["label"]
        max_age_h  = cfg["max_age_hours"]
        allow_skip = cfg["allow_skipped"]

        try:
            rows = (
                db.table(SCHEDULER_LOG_TABLE)
                .select("ran_at, status, duration_seconds, notes")
                .eq("job_name", job_name)
                .order("ran_at", desc=True)
                .limit(1)
                .execute()
                .data or []
            )
        except Exception as e:
            logger.warning(f"[watchdog] scheduler_log query failed for {job_name}: {e}")
            rows = []

        last        = rows[0] if rows else {}
        last_ran    = last.get("ran_at")
        last_status = last.get("status")
        last_dt     = _parse_ts(last_ran)
        age_h       = _hours_ago(last_dt)

        if not last:
            color, detail = "red", "no run found"
        elif last_status == "failed":
            color, detail = "red", "last_status=failed"
        elif age_h is not None and age_h > max_age_h:
            color  = "red"
            detail = f"stale: {age_h:.1f}h ago (max {max_age_h}h)"
        elif last_status == "partial":
            color, detail = "yellow", "last_status=partial"
        elif last_status == "skipped" and not allow_skip:
            color, detail = "yellow", "skipped (allow_skipped=false)"
        else:
            color  = "green"
            detail = f"ok ({age_h:.1f}h ago)" if age_h is not None else "ok"

        items.append({
            "name":        job_name,
            "label":       label,
            "color":       color,
            "last_ran":    last_ran,
            "last_status": last_status,
            "detail":      detail,
        })

    # Stage health via pipeline_stage_health() RPC
    stage: dict = {}
    try:
        raw = db.rpc("pipeline_stage_health", {}).execute().data
        if isinstance(raw, list) and raw:
            stage = raw[0] or {}
        elif isinstance(raw, dict):
            stage = raw
    except Exception as e:
        logger.warning(f"[watchdog] pipeline_stage_health RPC failed: {e}")

    ec_at   = _parse_ts(stage.get("last_email_clean_at"))
    enr_at  = _parse_ts(stage.get("last_enriched_at"))
    cls_at  = _parse_ts(stage.get("last_classified_at"))
    backlog = int(stage.get("backlog_unenriched") or 0)
    uc      = int(stage.get("backlog_uncleaned")  or 0)
    pending = int(stage.get("pending_batch")      or 0)
    s_ready = int(stage.get("send_ready")         or 0)

    ec_h  = _hours_ago(ec_at)
    enr_h = _hours_ago(enr_at)
    cls_h = _hours_ago(cls_at)

    # email_clean stage: RED if null or older than 26h
    if ec_at is None or (ec_h is not None and ec_h > 26):
        ec_color  = "red"
        ec_detail = (
            f"last_email_clean_at {ec_h:.1f}h ago (max 26h)" if ec_h is not None
            else "last_email_clean_at null"
        )
    else:
        ec_color, ec_detail = "green", f"ok ({ec_h:.1f}h ago)"

    # enrichment stage: RED only if backlog > 0 AND last_enriched_at older than 6h (daemon stalled)
    if backlog > 0 and (enr_at is None or (enr_h is not None and enr_h > 6)):
        enr_color  = "red"
        enr_detail = (
            f"daemon stalled: backlog={backlog}, last_enriched {enr_h:.1f}h ago"
            if enr_h is not None else f"daemon stalled: backlog={backlog}, no enrichment run"
        )
    else:
        enr_color, enr_detail = "green", f"ok (backlog={backlog})"

    # classify stage: RED if null or older than 12h
    if cls_at is None or (cls_h is not None and cls_h > 12):
        cls_color  = "red"
        cls_detail = (
            f"last_classified_at {cls_h:.1f}h ago (max 12h)" if cls_h is not None
            else "last_classified_at null"
        )
    else:
        cls_color, cls_detail = "green", f"ok ({cls_h:.1f}h ago)"

    items.extend([
        {
            "name":        "stage_email_clean",
            "label":       "Stage: Email Clean",
            "color":       ec_color,
            "last_ran":    stage.get("last_email_clean_at"),
            "last_status": None,
            "detail":      ec_detail,
        },
        {
            "name":        "stage_enrichment",
            "label":       "Stage: Enrichment",
            "color":       enr_color,
            "last_ran":    stage.get("last_enriched_at"),
            "last_status": None,
            "detail":      enr_detail,
        },
        {
            "name":        "stage_classify",
            "label":       "Stage: Classify",
            "color":       cls_color,
            "last_ran":    stage.get("last_classified_at"),
            "last_status": None,
            "detail":      cls_detail,
        },
    ])

    red_count    = sum(1 for i in items if i["color"] == "red")
    yellow_count = sum(1 for i in items if i["color"] == "yellow")
    overall      = "red" if red_count else ("yellow" if yellow_count else "green")

    return {
        "overall":       overall,
        "red_count":     red_count,
        "yellow_count":  yellow_count,
        "items":         items,
        "stage_numbers": {
            "backlog_unenriched": backlog,
            "backlog_uncleaned":  uc,
            "pending_batch":      pending,
            "send_ready":         s_ready,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /kjle/v1/health/overview — public, no auth
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health/overview")
async def health_overview():
    """Live pipeline status panel feed. Public. No secrets in payload."""
    return compute_health()


# ─────────────────────────────────────────────────────────────────────────────
# POST /kjle/v1/health/run-watchdog — x-api-key protected
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/health/run-watchdog")
async def run_watchdog(
    digest: bool = Query(False, description="If true, always send a full digest email"),
    _auth: str = Depends(verify_api_key),
):
    db = get_db()

    result       = compute_health()
    overall      = result["overall"]
    red_count    = result["red_count"]
    yellow_count = result["yellow_count"]
    items        = result["items"]

    # Read most-recent prior snapshot for alert-transition detection
    try:
        prior_rows = (
            db.table("pipeline_health_snapshots")
            .select("red_count")
            .order("captured_at", desc=True)
            .limit(1)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"[watchdog] prior snapshot query failed: {e}")
        prior_rows = []

    prior_red = int((prior_rows[0].get("red_count") or 0)) if prior_rows else 0

    # Insert new snapshot row; capture id so we can flip alerted=True if emails go out
    snapshot_id: Optional[str] = None
    try:
        ins = db.table("pipeline_health_snapshots").insert({
            "overall":      overall,
            "red_count":    red_count,
            "yellow_count": yellow_count,
            "details":      result,
            "alerted":      False,
        }).execute()
        snapshot_id = ((ins.data or [{}])[0] or {}).get("id")
    except Exception as e:
        logger.error(f"[watchdog] snapshot insert failed: {e}")

    emails_sent = []

    # ALERT: first-red transition only (prior_red == 0 and now red)
    if overall == "red" and prior_red == 0:
        red_items = [i for i in items if i["color"] == "red"]
        lines = "\n".join(
            f"  - {i['label']}: last_ran={i['last_ran'] or 'never'}, "
            f"last_status={i['last_status'] or 'n/a'} ({i['detail']})"
            for i in red_items
        )
        body = (
            f"KJLE Pipeline ALERT — {red_count} issue(s) detected\n\n"
            f"RED items:\n{lines}\n\n"
            "Check /kjle/v1/health/overview for full details."
        )
        res = await send_email(
            to=ALERT_RECIPIENT,
            subject=f"\U0001f534 KJLE ALERT: {red_count} issue(s) detected",
            body_text=body,
        )
        emails_sent.append({"type": "alert", "ok": res.get("ok"), "id": res.get("id")})

    # DIGEST: always send when digest=True
    if digest:
        stage_n = result["stage_numbers"]
        lines = "\n".join(
            f"  [{i['color'].upper():6}] {i['label']}: "
            f"last_ran={i['last_ran'] or 'never'} — {i['detail']}"
            for i in items
        )
        body = (
            f"KJLE Daily Health Digest — overall: {overall.upper()}\n\n"
            f"Jobs + Stages:\n{lines}\n\n"
            f"Pipeline numbers:\n"
            f"  backlog_unenriched : {stage_n['backlog_unenriched']}\n"
            f"  backlog_uncleaned  : {stage_n['backlog_uncleaned']}\n"
            f"  pending_batch      : {stage_n['pending_batch']}\n"
            f"  send_ready         : {stage_n['send_ready']}\n"
        )
        res = await send_email(
            to=ALERT_RECIPIENT,
            subject=f"KJLE Daily Health — {overall}",
            body_text=body,
        )
        emails_sent.append({"type": "digest", "ok": res.get("ok"), "id": res.get("id")})

    # Mark snapshot alerted=True if any email was sent
    alerted = bool(emails_sent)
    if alerted and snapshot_id:
        try:
            db.table("pipeline_health_snapshots").update({"alerted": True}).eq(
                "id", snapshot_id
            ).execute()
        except Exception as e:
            logger.warning(f"[watchdog] snapshot alerted update failed: {e}")

    return {
        "result":      result,
        "emails_sent": emails_sent,
        "alerted":     alerted,
    }
