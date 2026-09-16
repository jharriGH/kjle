"""
KJLE — Scan Progress Notifier
File: scripts/scan_progress_notify.py

Run by kjle-scan-progress.timer every 30 minutes.
Checks scan_jobs for stall (0 completions in 30 min while work is queued)
and notifies Jim via Brain SMS. Single-instance enforced via flock.

Env vars (read from /etc/kjle-scan-daemon.env if sourced, else set directly):
  SUPABASE_URL            Supabase project URL
  SUPABASE_SERVICE_KEY    service_role key
  BRAIN_URL               Jim Brain base URL (default: https://jim-brain-production.up.railway.app)
  BRAIN_KEY               x-brain-key value
  PROGRESS_WINDOW_MIN     minutes to look back for completions (default: 30)
  DAILY_SUMMARY_HOUR      UTC hour to send daily healthy SMS, -1 to disable (default: 14)
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
BRAIN_URL = os.environ.get("BRAIN_URL", "https://jim-brain-production.up.railway.app").rstrip("/")
BRAIN_KEY = os.environ.get("BRAIN_KEY", "jim-brain-kje-2026-kingjames").strip()
PROGRESS_WINDOW_MIN = int(os.environ.get("PROGRESS_WINDOW_MIN", "30"))
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "14"))  # 14 UTC = ~7am PST

LOCK_FILE = "/tmp/kjle-scan-progress-notify.lock"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _brain_notify(message: str, channel: str = "sms") -> bool:
    try:
        payload = json.dumps({"message": message, "channel": channel}).encode()
        req = urllib.request.Request(
            f"{BRAIN_URL}/notify",
            data=payload,
            headers={"Content-Type": "application/json", "x-brain-key": BRAIN_KEY},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        print(f"[notify] sent: {message[:100]}", flush=True)
        return True
    except Exception as e:
        print(f"[notify] ERROR: {e}", flush=True)
        return False


def _supabase_get(path: str) -> dict | list:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Accept": "application/json",
            "Prefer": "count=exact",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _count_completed_since(cutoff_iso: str) -> int:
    """Count scan_jobs finished (done or error) since cutoff."""
    path = (
        f"scan_jobs?select=id"
        f"&status=in.(done,error)"
        f"&finished_at=gte.{urllib.parse.quote(cutoff_iso)}"
        f"&limit=1"
    )
    try:
        import urllib.parse
        url = f"{SUPABASE_URL}/rest/v1/{path}"
        req = urllib.request.Request(
            url,
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Accept": "application/json",
                "Prefer": "count=exact",
                "Range-Unit": "items",
                "Range": "0-0",
            },
            method="HEAD",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            cr = r.headers.get("Content-Range", "")
            # Content-Range: 0-0/1234  → total = 1234
            if "/" in cr:
                total = cr.split("/")[-1].strip()
                if total == "*":
                    # Fall back to GET with limit for small result sets
                    return _count_via_get(path.replace("&limit=1", "&limit=9999"))
                return int(total)
    except Exception as e:
        print(f"[count] HEAD error: {e}", flush=True)
    return -1


def _count_via_get(path: str) -> int:
    try:
        import urllib.parse
        url = f"{SUPABASE_URL}/rest/v1/{path}"
        req = urllib.request.Request(
            url,
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Accept": "application/json",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            return len(data) if isinstance(data, list) else 0
    except Exception as e:
        print(f"[count_get] error: {e}", flush=True)
    return -1


def _count_queued() -> int:
    try:
        import urllib.parse
        url = f"{SUPABASE_URL}/rest/v1/scan_jobs?select=id&status=eq.queued&limit=1"
        req = urllib.request.Request(
            url,
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Accept": "application/json",
                "Prefer": "count=exact",
                "Range-Unit": "items",
                "Range": "0-0",
            },
            method="HEAD",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            cr = r.headers.get("Content-Range", "")
            if "/" in cr:
                total = cr.split("/")[-1].strip()
                if total != "*":
                    return int(total)
    except Exception as e:
        print(f"[queued] error: {e}", flush=True)
    return -1


def _count_done_total() -> int:
    try:
        import urllib.parse
        url = f"{SUPABASE_URL}/rest/v1/scan_jobs?select=id&status=in.(done,error)&limit=1"
        req = urllib.request.Request(
            url,
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Accept": "application/json",
                "Prefer": "count=exact",
                "Range-Unit": "items",
                "Range": "0-0",
            },
            method="HEAD",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            cr = r.headers.get("Content-Range", "")
            if "/" in cr:
                total = cr.split("/")[-1].strip()
                if total != "*":
                    return int(total)
    except Exception as e:
        print(f"[done_total] error: {e}", flush=True)
    return -1


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[error] SUPABASE_URL and SUPABASE_SERVICE_KEY required", flush=True)
        return 2

    # Single-instance guard — timer may overlap on a slow system.
    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[skip] another instance is running", flush=True)
        return 0

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(minutes=PROGRESS_WINDOW_MIN)
    cutoff_iso = cutoff.isoformat()

    print(f"[check] window={PROGRESS_WINDOW_MIN}min, cutoff={cutoff_iso}", flush=True)

    completed_in_window = _count_completed_since(cutoff_iso)
    queued = _count_queued()

    print(f"[stats] completed_in_window={completed_in_window} queued={queued}", flush=True)

    # Stall detection: 0 progress while work remains.
    if completed_in_window == 0 and queued > 0:
        msg = (
            f"KJLE scan daemon STALLED: 0 jobs completed in last {PROGRESS_WINDOW_MIN}min, "
            f"{queued} jobs still queued. Check: systemctl status kjle-scan-daemon"
        )
        print(f"[STALL] {msg}", flush=True)
        _brain_notify(msg, channel="sms")

    # Daily healthy summary — only during the configured UTC hour window.
    elif DAILY_SUMMARY_HOUR >= 0 and now_utc.hour == DAILY_SUMMARY_HOUR:
        done_total = _count_done_total()
        total = (done_total if done_total >= 0 else 0) + (queued if queued >= 0 else 0)
        pct = f"{100*done_total//total}%" if total > 0 else "?"
        msg = (
            f"KJLE scan healthy: {done_total} done / {total} total ({pct}), "
            f"{queued} queued, {completed_in_window} in last {PROGRESS_WINDOW_MIN}min."
        )
        print(f"[healthy] {msg}", flush=True)
        _brain_notify(msg, channel="sms")
    else:
        print(f"[ok] no stall, no daily summary window. done_this_window={completed_in_window}", flush=True)

    fcntl.flock(lock_fh, fcntl.LOCK_UN)
    lock_fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
