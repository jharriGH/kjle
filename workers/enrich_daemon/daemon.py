"""
KJLE -- Stage-1 Enrichment Daemon
File: workers/enrich_daemon/daemon.py

Always-on async daemon that concurrently enriches leads where enrichment_stage=0.
Replaces the serial 12h scheduler job (job_enrich_stage1) with bounded-concurrency
asyncio fetches. Processes the ~1M+ lead backlog, then idles until new leads arrive.

Modeled after workers/scan_daemon/daemon.py:
  - Config from env + admin_settings override
  - asyncio.Semaphore for bounded concurrency (ENRICH_CONCURRENCY, default 25)
  - Graceful SIGTERM shutdown
  - Structured JSON logging
  - sd_notify watchdog + startup grace
  - Restart=always / RestartSec=10 in the unit file

Race safety vs old cron:
  Uses enrichment_locked boolean column -- atomically claims a chunk via UPDATE
  WHERE enrichment_locked=false, clears in finally. Old cron is disabled in
  scheduler.py so this is the only writer.

Required env vars (shared with kjle-api, sourced from EnvironmentFile):
  SUPABASE_URL           Supabase project URL
  SUPABASE_SERVICE_KEY   service_role key (never anon key)

Optional env vars:
  ENRICH_CONCURRENCY     max parallel fetches (default: 25)
  ENRICH_CHUNK_SIZE      leads claimed per cycle (default: 100)
  POLL_INTERVAL_SEC      sleep when queue empty, seconds (default: 30)
  WORKER_ID              label in structured logs (default: enrich-daemon)
  LOG_LEVEL              default: INFO
  BRAIN_URL              Brain REST base URL
  BRAIN_KEY              Brain auth key
  HEALTHY_NOTIFY_INTERVAL_S  daily-healthy SMS cadence (default: 86400; 0=off)

Admin settings keys (overridden at runtime without restart):
  enrich_daemon_concurrency   override ENRICH_CONCURRENCY
  enrich_daemon_chunk_size    override ENRICH_CHUNK_SIZE
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Add repo root to sys.path so API utility functions are importable ──────────
# This lets us reuse _fetch_html_free / _parse_signals_full / _extract_schema_types
# from the same modules job_enrich_stage1 uses -- no reimplementation.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from supabase import create_client, Client

try:
    from api.routes.website_audit import (
        _fetch_html_free    as wa_fetch_html_free,
        _parse_signals_full as wa_parse_signals_full,
        _FULL_AUDIT_COLUMNS as WA_FULL_AUDIT_COLUMNS,
    )
    from api.routes.enrichment import _extract_schema_types
except Exception as _ie:
    print(f"FATAL: cannot import API enrichment helpers -- {_ie}", flush=True)
    sys.exit(2)

# ── Configuration ─────────────────────────────────────────────────────────────
SUPABASE_URL         = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
_ENV_CONCURRENCY     = int(os.environ.get("ENRICH_CONCURRENCY", "25"))
CHUNK_SIZE           = int(os.environ.get("ENRICH_CHUNK_SIZE", "100"))   # was 500
POLL_INTERVAL_SEC    = int(os.environ.get("POLL_INTERVAL_SEC", "30"))
WORKER_ID            = "enrich-daemon"
LOG_LEVEL            = os.environ.get("LOG_LEVEL", "INFO").upper()
BRAIN_URL            = os.environ.get(
    "BRAIN_URL", "https://jim-brain-production.up.railway.app"
).rstrip("/")
BRAIN_KEY            = os.environ.get(
    "BRAIN_KEY", "jim-brain-kje-2026-kingjames"
).strip()
_HEALTHY_NOTIFY_INTERVAL_S = int(os.environ.get("HEALTHY_NOTIFY_INTERVAL_S", "86400"))

# All leads with websites are eligible (Stage 1 is free, no pain floor)
_ENRICH_MIN_PAIN = 0

# Per-lead fetch ceiling -- wa_fetch_html_free has its own 15s timeout per attempt
# (2 attempts = up to 32s); this outer ceiling is a belt-and-suspenders backstop.
_FETCH_TIMEOUT_S = 35

# Watchdog -- ping interval must be well under WatchdogSec=120 in the unit file.
# Timer-based: fires from an independent asyncio task, NOT tied to chunk progress.
_WATCHDOG_PING_S   = 30
_STARTUP_GRACE_S   = 120
_WATCHDOG_CHECK_S  = 60
_STALL_THRESHOLD_S = 600   # 10 min idle with backlog = stall

# IO back-off: on Postgres 57014 (statement_timeout), halve effective concurrency
# and sleep before recovering.
_BACKOFF_SLEEP_S = 60
_BACKOFF_MIN     = 2   # never go below 2 concurrent

# Startup stale-lock reclaimer -- batch size kept small to avoid full-table scan
# on unindexed enrichment_locked column (SC adds partial index post-deploy).
_STALE_LOCK_BATCH   = 2000
_STALE_LOCK_SLEEP_S = 0.5

# Write retry: on dropped-connection errors recreate the client and retry up to 3x.
_WRITE_RETRYABLE = (
    "Server disconnected",
    "ConnectionTerminated",
    "RemoteProtocolError",
    "ConnectError",
)

# Admin settings keys that override env vars at runtime
_ADMIN_CONCURRENCY_KEY = "enrich_daemon_concurrency"
_ADMIN_CHUNK_KEY       = "enrich_daemon_chunk_size"


# ── Structured JSON logger (mirrors scan_daemon pattern) ──────────────────────
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts":        datetime.now(timezone.utc).isoformat(),
            "level":     record.levelname,
            "worker_id": WORKER_ID,
            "event":     record.getMessage(),
        }
        extras = getattr(record, "extra_fields", None)
        if isinstance(extras, dict):
            payload.update(extras)
        return json.dumps(payload, default=str)


def _build_logger() -> logging.Logger:
    lg = logging.getLogger("enrich_daemon")
    lg.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_JsonFormatter())
    lg.handlers = [h]
    lg.propagate = False
    return lg


log = _build_logger()


def _log(event: str, level: int = logging.INFO, **fields: Any) -> None:
    rec = logging.LogRecord(
        name="enrich_daemon", level=level, pathname="", lineno=0,
        msg=event, args=None, exc_info=None,
    )
    rec.extra_fields = fields
    log.handle(rec)


# ── sd_notify (stdlib only, zero extra deps) ──────────────────────────────────
def _sd_notify(msg: str) -> None:
    sock_path = os.environ.get("NOTIFY_SOCKET", "")
    if not sock_path:
        return
    try:
        addr = "\0" + sock_path[1:] if sock_path.startswith("@") else sock_path
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except Exception:
        pass


# ── Brain notify (stdlib urllib, zero extra deps) ─────────────────────────────
def _brain_notify(message: str, channel: str = "sms") -> None:
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
        _log("brain_notify_sent", channel=channel, message=message[:120])
    except Exception as e:
        _log("brain_notify_error", level=logging.WARNING, error=str(e)[:200])


# ── Graceful shutdown ─────────────────────────────────────────────────────────
_shutdown = False

# Tracks the IDs locked by the in-flight chunk so the SIGTERM handler can unlock
# them synchronously before the process exits (covers graceful kills; the startup
# stale-lock reclaimer handles SIGKILL where no handler runs).
_current_chunk_ids: list[int] = []
_db_ref: Client | None = None


def _sigterm(signum, frame):
    global _shutdown
    _shutdown = True
    _log("sigterm_received", signum=signum,
         current_chunk_size=len(_current_chunk_ids))
    # Best-effort sync unlock of in-flight chunk on SIGTERM.
    # The async finally block will also fire if the event loop continues;
    # this is an additional safety net for cases where it doesn't.
    if _current_chunk_ids and _db_ref is not None:
        try:
            _unlock_chunk_sync(_db_ref, list(_current_chunk_ids))
        except Exception:
            pass


signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT, _sigterm)


# ── Supabase client ───────────────────────────────────────────────────────────
def _make_db() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ── Timestamp ─────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Stall watchdog state (module-level; scalar writes are GIL-safe) ───────────
_last_lead_completed_at: float = 0.0
_queue_had_leads: bool         = False
_queue_backlog: int            = 0
_daemon_start_time: float      = 0.0
_at_least_one_completion: bool = False


def _mark_lead_completed() -> None:
    global _last_lead_completed_at, _at_least_one_completion
    _last_lead_completed_at = time.monotonic()
    _at_least_one_completion = True


# ── Internal sentinel for Postgres 57014 ─────────────────────────────────────
class _PgTimeoutError(Exception):
    pass


# ── Admin setting overrides ───────────────────────────────────────────────────
def _get_admin_concurrency(db: Client, default: int) -> int:
    """Read enrich_daemon_concurrency from admin_settings. Returns default on error."""
    try:
        res = (
            db.table("admin_settings")
            .select("value")
            .eq("key", _ADMIN_CONCURRENCY_KEY)
            .execute()
        )
        if res.data and res.data[0].get("value") is not None:
            return int(res.data[0]["value"])
    except Exception:
        pass
    return default


def _get_admin_chunk_size(db: Client, default: int) -> int:
    """Read enrich_daemon_chunk_size from admin_settings. Returns default on error."""
    try:
        res = (
            db.table("admin_settings")
            .select("value")
            .eq("key", _ADMIN_CHUNK_KEY)
            .execute()
        )
        if res.data and res.data[0].get("value") is not None:
            return int(res.data[0]["value"])
    except Exception:
        pass
    return default


# ── DB helpers (sync, run via asyncio.to_thread from async context) ───────────

def _reclaim_stale_locks_sync(db: Client) -> int:
    """
    Release orphaned enrichment_locked=true rows left by a prior crash or SIGKILL.
    Called once at daemon startup, before the main loop begins.

    Runs in bounded batches of _STALE_LOCK_BATCH rows to avoid a full-table scan
    on the (initially unindexed) boolean column. A partial index on
    (enrichment_locked) WHERE enrichment_locked=true makes each batch instant;
    even without the index the SELECT + UPDATE on 2000 rows is manageable.
    """
    total = 0
    while True:
        try:
            res = db.rpc("reclaim_stale_enrichment_locks", {"p_batch": _STALE_LOCK_BATCH}).execute()
            n = res.data if isinstance(res.data, int) else (res.data or 0)
            if not n:
                break
            total += n
            _log("stale_lock_batch_reclaimed", batch=n, total_so_far=total)
            time.sleep(_STALE_LOCK_SLEEP_S)
        except Exception as e:
            _log("stale_lock_reclaim_error", level=logging.ERROR, error=str(e)[:300])
            break
    return total


def _claim_chunk_sync(db: Client, chunk_size: int) -> list[dict]:
    """
    Claim up to chunk_size leads by setting enrichment_locked=true.
    SELECT candidates WHERE enrichment_locked=false, then bulk UPDATE guarded on
    enrichment_locked=false -- only returns rows we actually claimed.
    """
    try:
        cands = (
            db.table("leads")
            .select("id, website, business_name, pain_score")
            .eq("is_active", True)
            .eq("enrichment_stage", 0)
            .eq("enrichment_locked", False)
            .gte("pain_score", _ENRICH_MIN_PAIN)
            .not_.is_("website", "null")
            .neq("website", "")
            .order("pain_score", desc=True)
            .limit(chunk_size)
            .execute()
        )
        cands_data = cands.data or []
        if not cands_data:
            return []

        ids = [r["id"] for r in cands_data]

        # Atomic bulk claim: only rows still unlocked are updated
        claimed_resp = (
            db.table("leads")
            .update({"enrichment_locked": True})
            .in_("id", ids)
            .eq("enrichment_locked", False)
            .execute()
        )
        claimed_ids = {r["id"] for r in (claimed_resp.data or [])}

        return [r for r in cands_data if r["id"] in claimed_ids]

    except Exception as e:
        _log("claim_chunk_error", level=logging.ERROR, error=str(e)[:400])
        return []


def _unlock_chunk_sync(db: Client, ids: list[int]) -> None:
    """Release enrichment_locked for a set of lead IDs in a single UPDATE."""
    if not ids:
        return
    try:
        db.table("leads").update({"enrichment_locked": False}).in_("id", ids).execute()
    except Exception as e:
        _log("unlock_error", level=logging.WARNING,
             count=len(ids), error=str(e)[:200])


def _write_result_sync(db: Client, lead_id: int, payload: dict) -> None:
    """
    Write enrichment result for one lead.
    Retries up to 3x (with exponential backoff and a fresh client) on dropped-
    connection errors ("Server disconnected", "ConnectionTerminated", etc.).
    Raises _PgTimeoutError if Postgres returns SQLSTATE 57014.
    """
    for attempt in range(3):
        _db = _make_db() if attempt > 0 else db
        try:
            t0 = time.monotonic()
            _db.table("leads").update(payload).eq("id", lead_id).execute()
            elapsed = time.monotonic() - t0
            if elapsed > 5.0:
                _log("slow_write", level=logging.WARNING,
                     lead_id=lead_id, elapsed_s=round(elapsed, 2))
            return
        except Exception as e:
            err = str(e)
            if "57014" in err:
                _log("pg_statement_timeout", level=logging.WARNING,
                     lead_id=lead_id, error=err[:200])
                raise _PgTimeoutError(err)
            if any(s in err for s in _WRITE_RETRYABLE) and attempt < 2:
                delay = 2 ** attempt   # 1s, 2s
                _log("write_retry", level=logging.WARNING,
                     lead_id=lead_id, attempt=attempt + 1,
                     backoff_s=delay, error=err[:200])
                time.sleep(delay)
                continue
            _log("write_error", level=logging.ERROR, lead_id=lead_id, error=err[:300])
            raise


def _log_cost_zero_sync(db: Client, lead_id: int) -> None:
    try:
        db.table("api_cost_log").insert({
            "lead_id":           lead_id,
            "service":           "stage1_scrape",
            "cost_per_unit":     0.0,
            "records_processed": 1,
            "source_system":     "kjle",
            "created_at":        _now_iso(),
        }).execute()
    except Exception:
        pass


def _advance_stage_sync(db: Client, lead_id: int) -> None:
    """Advance enrichment_stage to 1 on persistent failure (no infinite retry)."""
    try:
        db.table("leads").update({
            "enrichment_stage": 1,
            "enriched_at":      _now_iso(),
        }).eq("id", lead_id).execute()
    except Exception:
        pass


# ── Per-lead enrichment coroutine ─────────────────────────────────────────────

async def _enrich_lead(lead: dict, db: Client, sem: asyncio.Semaphore) -> str:
    """
    Fetch + parse one lead website. Returns 'ok', 'unreachable', or 'error'.
    Raises _PgTimeoutError on Postgres 57014 so the cycle-level handler can back off.
    Applies the exact field writes job_enrich_stage1 uses:
      reachable   -> full signals + website_reachable=true + schema_types + enrichment_stage=1
      unreachable -> is_parked=true, website_has_ssl=false, website_reachable=false
      always      -> last_audited_at
      on success  -> enriched_at + last_enriched_at + enrichment_stage=1
    """
    lead_id = lead["id"]
    website = (lead.get("website") or "").strip()

    async with sem:
        # Fetch -- wa_fetch_html_free is async httpx; bounded by _FETCH_TIMEOUT_S
        try:
            html, final_url = await asyncio.wait_for(
                wa_fetch_html_free(website), timeout=_FETCH_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            _log("fetch_timeout", level=logging.WARNING,
                 lead_id=lead_id, website=website[:80])
            html, final_url = None, None
        except Exception as e:
            _log("fetch_error", level=logging.WARNING,
                 lead_id=lead_id, website=website[:80], error=str(e)[:200])
            html, final_url = None, None

        now = _now_iso()

        try:
            if html is None:
                # Unreachable -- mirror job_enrich_stage1 unreachable path
                payload: dict[str, Any] = {
                    "is_parked":         True,
                    "website_has_ssl":   False,
                    "website_reachable": False,
                    "last_audited_at":   now,
                    "enrichment_stage":  1,
                    "enriched_at":       now,
                    "last_enriched_at":  now,
                }
                await asyncio.to_thread(_write_result_sync, db, lead_id, payload)
                _mark_lead_completed()
                return "unreachable"

            # Reachable -- full signal set
            signals   = wa_parse_signals_full(html, final_url)
            safe_sigs = {k: v for k, v in signals.items() if k in WA_FULL_AUDIT_COLUMNS}
            schema    = _extract_schema_types(html)
            payload = {
                **safe_sigs,
                "website_reachable": True,
                "schema_types":      schema,
                "last_audited_at":   now,
                "enrichment_stage":  1,
                "enriched_at":       now,
                "last_enriched_at":  now,
            }
            await asyncio.to_thread(_write_result_sync, db, lead_id, payload)
            await asyncio.to_thread(_log_cost_zero_sync, db, lead_id)
            _mark_lead_completed()
            return "ok"

        except _PgTimeoutError:
            # Propagate so cycle handler can halve concurrency
            raise

        except Exception as e:
            _log("enrich_exception", level=logging.ERROR,
                 lead_id=lead_id, website=website[:80], error=str(e)[:300])
            # Advance stage so a broken lead doesn't loop forever
            await asyncio.to_thread(_advance_stage_sync, db, lead_id)
            _mark_lead_completed()
            return "error"


# ── Independent systemd watchdog ping (asyncio task) ─────────────────────────

async def _watchdog_ping_task() -> None:
    """
    Send WATCHDOG=1 to systemd every _WATCHDOG_PING_S seconds.
    Runs as an independent asyncio task so the ping continues even while a chunk
    is mid-processing -- decoupled from chunk/loop progress.
    A live event loop is sufficient evidence the process is healthy; genuine stalls
    are detected by the _watchdog_loop thread which exits via os._exit(1).
    """
    while not _shutdown:
        _sd_notify("WATCHDOG=1")
        await asyncio.sleep(_WATCHDOG_PING_S)


# ── Stall watchdog thread ─────────────────────────────────────────────────────

def _watchdog_loop() -> None:
    """
    Checks every 60s whether the daemon is making progress.
    Exits (os._exit(1)) on stall so systemd (Restart=always) restarts clean.
    Also sends a daily "healthy" SMS when the queue is active.
    """
    import threading
    _last_healthy_at = 0.0

    while not _shutdown:
        time.sleep(_WATCHDOG_CHECK_S)
        if _shutdown:
            break

        now = time.monotonic()

        # Daily healthy heartbeat
        if (
            _HEALTHY_NOTIFY_INTERVAL_S > 0
            and _at_least_one_completion
            and _queue_had_leads
            and (now - _last_healthy_at) >= _HEALTHY_NOTIFY_INTERVAL_S
        ):
            _brain_notify(
                f"KJLE enrich daemon healthy: "
                f"{_queue_backlog} leads remaining, running normally."
            )
            _last_healthy_at = now

        if now - _daemon_start_time < _STARTUP_GRACE_S:
            continue
        if not _at_least_one_completion:
            continue
        if not _queue_had_leads:
            continue

        idle = now - _last_lead_completed_at
        if idle > _STALL_THRESHOLD_S:
            _log(
                "daemon_stall_detected",
                level=logging.CRITICAL,
                seconds_idle=int(idle),
                stall_threshold_s=_STALL_THRESHOLD_S,
            )
            _brain_notify(
                f"URGENT: KJLE enrich daemon STALLED -- 0 progress in {int(idle)}s, "
                f"{_queue_backlog} leads remaining. Restarting."
            )
            # Hard exit: SystemExit only kills this thread; os._exit kills the process
            # so systemd (Restart=always) restarts it.
            os._exit(1)


# ── Async main loop ───────────────────────────────────────────────────────────

async def _run(db: Client) -> None:
    import threading

    global _daemon_start_time, _queue_had_leads, _queue_backlog
    global _current_chunk_ids, _db_ref

    _daemon_start_time = time.monotonic()
    _db_ref = db

    # ── Startup: clear orphaned locks from prior crash / SIGKILL ─────────────
    # Batched to avoid a full-table timeout on the unindexed boolean column.
    _log("stale_lock_reclaim_start")
    reclaimed = await asyncio.to_thread(_reclaim_stale_locks_sync, db)
    _log("stale_lock_reclaim_done", reclaimed=reclaimed)

    # ── Stall watchdog (separate thread -- stall detection via os._exit) ─────
    watchdog = threading.Thread(target=_watchdog_loop, daemon=True, name="stall-watchdog")
    watchdog.start()
    _log("watchdog_started",
         stall_threshold_s=_STALL_THRESHOLD_S,
         startup_grace_s=_STARTUP_GRACE_S)

    # Signal systemd: daemon is fully initialized.
    # Type=notify in the unit makes systemd wait for this before marking active.
    _sd_notify("READY=1")
    _log("sd_notify_ready_sent")

    # ── Independent watchdog ping (asyncio task, fires every _WATCHDOG_PING_S) ─
    # Decoupled from chunk progress: a 100-lead chunk of slow sites can take
    # minutes; this task keeps WATCHDOG=1 arriving regardless.
    asyncio.ensure_future(_watchdog_ping_task())
    _log("watchdog_ping_task_started", interval_s=_WATCHDOG_PING_S)

    effective_concurrency = await asyncio.to_thread(
        _get_admin_concurrency, db, _ENV_CONCURRENCY
    )
    effective_chunk_size = await asyncio.to_thread(
        _get_admin_chunk_size, db, CHUNK_SIZE
    )
    _log("daemon_started",
         effective_concurrency=effective_concurrency,
         effective_chunk_size=effective_chunk_size,
         poll_interval_sec=POLL_INTERVAL_SEC)

    cycle = 0
    while not _shutdown:
        cycle += 1

        # Refresh admin overrides every 50 cycles (~25 min at default poll interval)
        if cycle % 50 == 1 and cycle > 1:
            effective_concurrency = await asyncio.to_thread(
                _get_admin_concurrency, db, _ENV_CONCURRENCY
            )
            effective_chunk_size = await asyncio.to_thread(
                _get_admin_chunk_size, db, CHUNK_SIZE
            )

        # Claim next chunk
        leads = await asyncio.to_thread(_claim_chunk_sync, db, effective_chunk_size)

        if not leads:
            _queue_had_leads = False
            _log("queue_empty", poll_interval_sec=POLL_INTERVAL_SEC)
            for _ in range(POLL_INTERVAL_SEC):
                if _shutdown:
                    break
                await asyncio.sleep(1)
            continue

        _queue_had_leads   = True
        _queue_backlog     = len(leads)
        claimed_ids        = [l["id"] for l in leads]
        _current_chunk_ids = claimed_ids   # visible to SIGTERM handler
        _log("chunk_claimed", count=len(leads), concurrency=effective_concurrency,
             chunk_size=effective_chunk_size)

        sem = asyncio.Semaphore(effective_concurrency)
        succeeded = failed = unreachable = 0
        pg_timeout_hit = False

        try:
            tasks = [asyncio.ensure_future(_enrich_lead(lead, db, sem)) for lead in leads]

            for fut in asyncio.as_completed(tasks):
                if _shutdown:
                    # Cancel remaining tasks on shutdown signal
                    for t in tasks:
                        t.cancel()
                    break
                try:
                    result = await fut
                    if result == "ok":
                        succeeded += 1
                    elif result == "unreachable":
                        unreachable += 1
                    else:
                        failed += 1
                except _PgTimeoutError:
                    pg_timeout_hit = True
                    failed += 1
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    _log("task_exception", level=logging.ERROR, error=str(e)[:300])
                    failed += 1

        finally:
            # Always release locks -- even on shutdown or exception.
            # SIGTERM handler also unlocks synchronously for belt-and-suspenders.
            _current_chunk_ids = []
            await asyncio.to_thread(_unlock_chunk_sync, db, claimed_ids)

        _log(
            "chunk_done",
            succeeded=succeeded,
            unreachable=unreachable,
            failed=failed,
            claimed=len(leads),
            pg_timeout=pg_timeout_hit,
        )

        # IO back-off: on Postgres 57014, halve concurrency and rest
        if pg_timeout_hit:
            new_conc = max(_BACKOFF_MIN, effective_concurrency // 2)
            _log(
                "io_backoff",
                level=logging.WARNING,
                old_concurrency=effective_concurrency,
                new_concurrency=new_conc,
                reason="pg_statement_timeout_57014",
            )
            effective_concurrency = new_conc
            await asyncio.sleep(_BACKOFF_SLEEP_S)
        elif succeeded + unreachable > 0 and effective_concurrency < _ENV_CONCURRENCY:
            # Recover one step after a clean cycle (doubling back to cap)
            effective_concurrency = min(_ENV_CONCURRENCY, effective_concurrency * 2)
            _log("io_recover", effective_concurrency=effective_concurrency)


def main() -> int:
    _log(
        "daemon_starting",
        supabase_url=(SUPABASE_URL[:40] + "...") if len(SUPABASE_URL) > 40 else SUPABASE_URL,
        env_concurrency=_ENV_CONCURRENCY,
        chunk_size=CHUNK_SIZE,
        poll_interval_sec=POLL_INTERVAL_SEC,
        fetch_timeout_s=_FETCH_TIMEOUT_S,
        watchdog_ping_s=_WATCHDOG_PING_S,
    )

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        _log(
            "config_error",
            level=logging.ERROR,
            detail="SUPABASE_URL and SUPABASE_SERVICE_KEY are required",
        )
        return 2

    db = _make_db()

    try:
        asyncio.run(_run(db))
    except KeyboardInterrupt:
        _log("keyboard_interrupt")
    except Exception as e:
        _log("fatal_exception", level=logging.CRITICAL, error=str(e)[:400])
        return 1

    _log("daemon_exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
