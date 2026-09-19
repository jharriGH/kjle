#!/usr/bin/env python3
"""
job_heartbeat.py — KJLE outcome-based job health monitor.

Runs every 15 min via kjle-heartbeat.timer (single-instance via flock).
For each enabled row in job_health:
  - Probes live Supabase metrics (throughput, backlog, last_output_at)
  - Updates status, throughput_window, backlog, last_output_at,
    last_checked_at, detail, updated_at
  - On STALLED/DEGRADED: SMS Jim via Brain /notify, brain_log,
    restart the systemd unit at most once per hour

Alert and restart are both rate-limited to 1/hour/job via
last_alert_at and last_auto_action columns.
"""

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv("/opt/kjle/.env")

DATABASE_URL = os.environ["DATABASE_URL"]
BRAIN_URL    = "https://jim-brain-production.up.railway.app"
BRAIN_KEY    = "jim-brain-kje-2026-kingjames"

LOG_FILE = "/tmp/kjle-heartbeat.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── DB connection ─────────────────────────────────────────────────────────────

def get_conn():
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=15)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0")
    return conn


# ── Time helpers ──────────────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc)


def seconds_since(ts):
    """Seconds elapsed since ts (datetime or ISO string). None if ts is None."""
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now_utc() - ts).total_seconds()


def _iso(dt):
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt


# ── Brain comms ───────────────────────────────────────────────────────────────

def brain_notify(message: str, severity: str = "warn"):
    try:
        r = httpx.post(
            f"{BRAIN_URL}/notify",
            headers={"x-brain-key": BRAIN_KEY, "Content-Type": "application/json"},
            json={"severity": severity, "message": message[:500], "channel": "sms"},
            timeout=12.0,
        )
        if r.status_code not in (200, 207):
            log.warning(f"brain_notify HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"brain_notify failed: {e}")


def brain_log_entry(message: str, tags: list | None = None):
    try:
        r = httpx.post(
            f"{BRAIN_URL}/log",
            headers={"x-brain-key": BRAIN_KEY, "Content-Type": "application/json"},
            json={"content": message, "tags": tags or ["heartbeat"]},
            timeout=12.0,
        )
        if r.status_code >= 400:
            log.warning(f"brain_log HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"brain_log failed: {e}")


# ── Per-job probes ────────────────────────────────────────────────────────────

def probe_contacts_cleaner(conn, row: dict) -> dict:
    window_min = int(row.get("window_minutes") or 60)
    stall_min  = int(row.get("stall_minutes")  or 120)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
              COUNT(*) FILTER (WHERE email_cleaned_at > now() - (%s * interval '1 minute'))
                AS throughput,
              COUNT(*) FILTER (WHERE email_status = 'unvalidated'
                                 AND (email_trust IS NULL OR email_trust <> 'catch_all'))
                AS backlog,
              MAX(email_cleaned_at) AS last_output_at
            FROM contacts
        """, (window_min,))
        throughput, backlog, last_output_at = cur.fetchone()
    throughput = int(throughput or 0)
    backlog    = int(backlog    or 0)

    secs_idle = seconds_since(last_output_at)
    stalled = throughput == 0 and backlog > 0 and (
        secs_idle is None or secs_idle > stall_min * 60
    )
    status = "stalled" if stalled else ("healthy" if backlog == 0 else "ok")
    return {
        "status":            status,
        "throughput_window": throughput,
        "backlog":           backlog,
        "last_output_at":    _iso(last_output_at),
        "detail":            f"throughput={throughput} last {window_min}m, backlog={backlog}",
    }


def probe_contacts_classify(conn, row: dict) -> dict:
    stall_min   = int(row.get("stall_minutes") or 120)
    prev_backlog = int(row.get("backlog") or 0)
    prev_last_output = row.get("last_output_at")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM contacts WHERE email_trust IS NULL")
        backlog = int(cur.fetchone()[0] or 0)

    throughput_window = max(0, prev_backlog - backlog)

    if throughput_window > 0:
        last_output_at_iso = now_utc().isoformat()
    else:
        last_output_at_iso = _iso(prev_last_output)

    secs_idle = seconds_since(last_output_at_iso)
    stalled = backlog > 0 and throughput_window == 0 and (
        secs_idle is None or secs_idle > stall_min * 60
    )
    status = "stalled" if stalled else ("healthy" if backlog == 0 else "ok")
    return {
        "status":            status,
        "throughput_window": throughput_window,
        "backlog":           backlog,
        "last_output_at":    last_output_at_iso,
        "detail":            f"backlog={backlog}, delta_vs_prev={throughput_window}",
    }


def probe_leads_nightly_clean(conn, row: dict) -> dict:
    window_min   = int(row.get("window_minutes") or 1440)
    min_expected = int(row.get("min_expected")   or 1000)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
              COUNT(*) FILTER (WHERE email_cleaned_at > now() - (%s * interval '1 minute'))
                AS throughput,
              COUNT(*) FILTER (WHERE email_status = 'unvalidated') AS backlog,
              MAX(email_cleaned_at) AS last_output_at
            FROM leads
            WHERE email IS NOT NULL AND email <> ''
        """, (window_min,))
        throughput, backlog, last_output_at = cur.fetchone()
    throughput = int(throughput or 0)
    backlog    = int(backlog    or 0)

    stalled = throughput < min_expected and backlog > 0
    status = "stalled" if stalled else ("healthy" if backlog == 0 else "ok")
    return {
        "status":            status,
        "throughput_window": throughput,
        "backlog":           backlog,
        "last_output_at":    _iso(last_output_at),
        "detail":            f"throughput={throughput} last {window_min}m (min={min_expected}), backlog={backlog}",
    }


def probe_leads_ingest(conn, row: dict) -> dict:
    window_min = int(row.get("window_minutes") or 1440)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id FROM truelist_batches
            WHERE submitted_by = 'nightly_cron'
              AND status = 'ingested'
              AND notes ILIKE '%%processed=0%%'
              AND submitted_at > now() - (%s * interval '1 minute')
        """, (window_min,))
        zero_batches = [r[0] for r in cur.fetchall()]

        cur.execute("""
            SELECT id FROM truelist_batches
            WHERE status IN ('processing', 'pending')
              AND submitted_at < now() - interval '6 hours'
        """)
        stuck_batches = [r[0] for r in cur.fetchall()]

    degraded = bool(zero_batches or stuck_batches)
    offender_parts = []
    if zero_batches:
        offender_parts.append(f"processed=0: {zero_batches[:5]}")
    if stuck_batches:
        offender_parts.append(f"stuck>6h: {stuck_batches[:5]}")

    return {
        "status":            "degraded" if degraded else "ok",
        "throughput_window": None,
        "backlog":           None,
        "last_output_at":    None,
        "detail":            "; ".join(offender_parts) if offender_parts else "all batches healthy",
    }


def probe_chatbot_reaudit(conn, row: dict) -> dict:
    window_min = int(row.get("window_minutes") or 60)
    stall_min  = int(row.get("stall_minutes")  or 120)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
              COUNT(*) FILTER (WHERE status = 'done'
                                 AND finished_at > now() - (%s * interval '1 minute'))
                AS throughput,
              COUNT(*) FILTER (WHERE status = 'queued') AS backlog,
              MAX(finished_at) FILTER (WHERE status = 'done') AS last_output_at
            FROM scan_jobs
        """, (window_min,))
        throughput, backlog, last_output_at = cur.fetchone()
    throughput = int(throughput or 0)
    backlog    = int(backlog    or 0)

    secs_idle = seconds_since(last_output_at)
    stalled = throughput == 0 and backlog > 0 and (
        secs_idle is None or secs_idle > stall_min * 60
    )
    status = "stalled" if stalled else ("healthy" if backlog == 0 else "ok")
    return {
        "status":            status,
        "throughput_window": throughput,
        "backlog":           backlog,
        "last_output_at":    _iso(last_output_at),
        "detail":            f"throughput={throughput} last {window_min}m, queued={backlog}",
    }


PROBES = {
    "contacts_cleaner":    probe_contacts_cleaner,
    "contacts_classify":   probe_contacts_classify,
    "leads_nightly_clean": probe_leads_nightly_clean,
    "leads_ingest":        probe_leads_ingest,
    "chatbot_reaudit":     probe_chatbot_reaudit,
}


# ── Systemd unit restart (rate-limited to 1/hour) ─────────────────────────────

def maybe_restart(row: dict, conn) -> str | None:
    unit = (row.get("systemd_unit") or "").strip()
    if not unit:
        return None

    secs = seconds_since(row.get("last_auto_action"))
    if secs is not None and secs < 3600:
        return f"restart suppressed (last restart {int(secs/60)}m ago, escalating)"

    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", unit],
            capture_output=True, text=True, timeout=30,
        )
        note = f"restarted {unit} (rc={result.returncode})"
        if result.returncode != 0:
            note += f" stderr={result.stderr.strip()[:150]}"
        log.info(f"[heartbeat] {note}")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE job_health SET last_auto_action = %s WHERE job_key = %s",
                (now_utc(), row["job_key"]),
            )
        return note
    except Exception as e:
        log.error(f"[heartbeat] restart {unit} failed: {e}")
        return f"restart_failed: {e}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info(f"=== job_heartbeat start (pid={os.getpid()}) ===")
    conn = get_conn()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM job_health WHERE enabled = TRUE ORDER BY job_key")
        jobs = [dict(r) for r in cur.fetchall()]

    if not jobs:
        log.warning("No enabled rows in job_health — nothing to probe")
        conn.close()
        return

    log.info(f"Probing {len(jobs)} job(s)")
    now = now_utc()
    summary_parts = []

    for row in jobs:
        job_key  = row["job_key"]
        probe_fn = PROBES.get(job_key)
        if probe_fn is None:
            log.warning(f"No probe registered for job_key={job_key!r} — skipping")
            continue

        try:
            metrics = probe_fn(conn, row)
        except Exception as e:
            log.error(f"probe {job_key} raised: {e}", exc_info=True)
            metrics = {
                "status": "unknown", "throughput_window": None, "backlog": None,
                "last_output_at": None, "detail": f"probe_error: {e}",
            }

        status = metrics["status"]
        detail = metrics.get("detail") or ""
        log.info(f"  {job_key}: {status}  {detail}")
        summary_parts.append(f"{job_key}={status}")

        # Build UPDATE — only set last_output_at if probe returned one
        update: dict = {
            "status":            status,
            "throughput_window": metrics.get("throughput_window"),
            "backlog":           metrics.get("backlog"),
            "last_checked_at":   now,
            "detail":            detail,
            "updated_at":        now,
        }
        if metrics.get("last_output_at") is not None:
            update["last_output_at"] = metrics["last_output_at"]

        set_clause = ", ".join(f"{k} = %s" for k in update)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE job_health SET {set_clause} WHERE job_key = %s",
                list(update.values()) + [job_key],
            )

        # Alert + auto-restart on stalled / degraded
        if status in ("stalled", "degraded"):
            secs_since_alert = seconds_since(row.get("last_alert_at"))
            alert_due = secs_since_alert is None or secs_since_alert >= 3600

            if alert_due:
                msg = f"KJLE {job_key} is {status.upper()}: {detail[:220]}"
                brain_notify(msg, severity="warn")
                brain_log_entry(f"heartbeat alert: {msg}", tags=["heartbeat", job_key, status])
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE job_health SET last_alert_at = %s WHERE job_key = %s",
                        (now, job_key),
                    )
                log.info(f"  alert sent for {job_key}")

            if status == "stalled":
                restart_note = maybe_restart(row, conn)
                if restart_note:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE job_health SET detail = %s WHERE job_key = %s",
                            (f"{detail} | {restart_note}", job_key),
                        )

    summary = ", ".join(summary_parts)
    log.info(f"=== job_heartbeat done: {summary} ===")
    brain_log_entry(f"heartbeat run complete: {summary}", tags=["heartbeat"])
    conn.close()


if __name__ == "__main__":
    main()
