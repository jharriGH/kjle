"""
KJLE — WebSignalz Scan Daemon (Phase 3)
File: workers/scan_daemon/daemon.py

Polls scan_jobs for queued URLs, runs headless Chromium + vendored axe-core 4.10.2,
and writes append-only rows to scan_results. Direct Supabase access via service_role key.

Required env vars:
  SUPABASE_URL           Supabase project URL
  SUPABASE_SERVICE_KEY   service_role key (never anon key)
  SCAN_CONCURRENCY       max parallel browser contexts (default: 4, hard cap: 4)
  POLL_INTERVAL_SEC      seconds between polls when queue is empty (default: 15)
  SCAN_BATCH_SIZE        jobs claimed per poll cycle (default: SCAN_CONCURRENCY)
  WORKER_ID              label in metadata.worker_id (default: scan-daemon)
  LOG_LEVEL              default: INFO
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from supabase import create_client, Client

# ── Configuration ────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
# Hard cap at 4 — each Chromium instance uses ~350-500 MB system RAM.
SCAN_CONCURRENCY = min(int(os.environ.get("SCAN_CONCURRENCY", "4")), 4)
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "15"))
SCAN_BATCH_SIZE = int(os.environ.get("SCAN_BATCH_SIZE", str(SCAN_CONCURRENCY)))
WORKER_ID = os.environ.get("WORKER_ID", "scan-daemon").strip()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

AXE_VERSION = "4.10.2"
AXE_PATH = Path(__file__).parent / "axe.min.js"
NAV_TIMEOUT_MS = 30_000  # 30 s, hard limit per navigation

# ── JSON logger (mirrors local_scraper_daemon pattern) ───────────────────────
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "worker_id": WORKER_ID,
            "event": record.getMessage(),
        }
        extras = getattr(record, "extra_fields", None)
        if isinstance(extras, dict):
            payload.update(extras)
        return json.dumps(payload, default=str)


def _build_logger() -> logging.Logger:
    lg = logging.getLogger("scan_daemon")
    lg.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_JsonFormatter())
    lg.handlers = [h]
    lg.propagate = False
    return lg


log = _build_logger()


def _log(event: str, level: int = logging.INFO, **fields: Any) -> None:
    rec = logging.LogRecord(
        name="scan_daemon", level=level, pathname="", lineno=0,
        msg=event, args=None, exc_info=None,
    )
    rec.extra_fields = fields
    log.handle(rec)


# ── Graceful shutdown ────────────────────────────────────────────────────────
_shutdown = False


def _sigterm(signum, frame):
    global _shutdown
    _shutdown = True
    _log("sigterm_received", signum=signum)


signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT, _sigterm)


# ── Supabase client ──────────────────────────────────────────────────────────
def _make_db() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ── axe-core JS (loaded once at startup, not CDN) ───────────────────────────
def _load_axe() -> str:
    if not AXE_PATH.exists():
        raise RuntimeError(f"axe.min.js not found at {AXE_PATH} — copy it from /tmp/wsz_p3_bench/")
    return AXE_PATH.read_text(encoding="utf-8")


# ── Accessibility score formula ──────────────────────────────────────────────
_IMPACT_WEIGHTS = {"critical": 25.0, "serious": 10.0, "moderate": 3.0, "minor": 1.0}
# K is a PROVISIONAL calibration constant. Recalibrate once a real score distribution
# exists from thousands of lead scans -- do not treat this value as empirically derived.
SCORE_DECAY_K = 60.0
# Version stamp: any formula change invalidates trend history; stamp makes it explainable.
SCORE_FORMULA_VERSION = "2.0-expdecay-k60"


def _compute_score(violations: list[dict]) -> float:
    # Automated RISK index (0-100, approaches but never reaches 0).
    # This is a relative, tool-specific score -- NEVER a compliance determination.
    # Raw violation counts are the authoritative fact; 'incomplete' items never affect
    # this score because they are unconfirmed.
    # Formula: W = sum(weight(impact) * (1 + log10(max(1, affected_nodes))))
    #          score = 100 * exp(-W / SCORE_DECAY_K)
    # The 4 *_count columns remain counts of violation TYPES (unchanged semantics).
    W = 0.0
    for v in violations:
        impact = (v.get("impact") or "minor").lower()
        weight = _IMPACT_WEIGHTS.get(impact, 1.0)
        nodes = max(1, v.get("affected_nodes", 1))
        W += weight * (1.0 + math.log10(nodes))
    return 100.0 * math.exp(-W / SCORE_DECAY_K)


# ── Per-job scan (runs inside thread pool) ───────────────────────────────────
def _scan_url(url: str, axe_js: str) -> dict:
    """
    Navigate to URL, inject vendored axe-core, run axe.run(), parse results.
    Returns a dict with status, violation counts, score, violations list.
    NEVER returns a fake passing result on any exception — status='error' always.
    """
    start = time.perf_counter()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    java_script_enabled=True,
                )
                page = ctx.new_page()
                # domcontentloaded is the hard requirement; networkidle is best-effort.
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:
                    pass
                # Inject vendored axe — never CDN-load.
                page.evaluate(axe_js)
                raw = page.evaluate(
                    """() => new Promise((resolve, reject) => {
                        axe.run(document, {}, (err, results) => {
                            if (err) reject(err.toString());
                            else resolve(results);
                        });
                    })"""
                )
            finally:
                browser.close()

        counts: dict[str, int] = {"critical": 0, "serious": 0, "moderate": 0, "minor": 0}
        violations = []
        for v in raw.get("violations", []):
            impact = (v.get("impact") or "minor").lower()
            if impact in counts:
                counts[impact] += 1  # count violation TYPES, not affected nodes
            violations.append({
                "id": v["id"],
                "impact": v.get("impact"),
                "tags": v.get("tags", []),
                "description": v.get("description", ""),
                "affected_nodes": len(v.get("nodes", [])),
            })

        # axe 'incomplete' = checks axe could NOT automatically determine (e.g. color-contrast
        # on image/gradient backgrounds). Valuable to surface for client review; never affects
        # the automated score because these are unconfirmed violations.
        incomplete = []
        for item in raw.get("incomplete", []):
            incomplete.append({
                "id": item["id"],
                "impact": item.get("impact"),
                "tags": item.get("tags", []),
                "description": item.get("description", ""),
                "affected_nodes": len(item.get("nodes", [])),
            })
        incomplete_count = len(incomplete)

        # Score uses violations list (with per-violation affected_nodes) for prevalence weighting.
        score = _compute_score(violations)
        elapsed = time.perf_counter() - start
        _log(
            "scan_ok", url=url,
            critical=counts["critical"], serious=counts["serious"],
            incomplete_count=incomplete_count,
            score=round(score, 2), elapsed_s=round(elapsed, 3),
        )
        return {
            "status": "ok",
            "violations": violations,
            "critical_count": counts["critical"],
            "serious_count": counts["serious"],
            "moderate_count": counts["moderate"],
            "minor_count": counts["minor"],
            "incomplete_count": incomplete_count,
            "incomplete": incomplete,
            "accessibility_score": round(score, 2),
            "score_formula_version": SCORE_FORMULA_VERSION,
            "error": None,
        }

    except PWTimeout as e:
        _log("scan_timeout", level=logging.WARNING, url=url, error=str(e)[:200])
        return {"status": "error", "error": f"timeout: {str(e)[:400]}", "violations": []}
    except Exception as e:
        _log("scan_error", level=logging.WARNING, url=url, error=str(e)[:200])
        return {"status": "error", "error": str(e)[:500], "violations": []}


# ── DB helpers ───────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _poll_queued(db: Client, batch: int) -> list[dict]:
    try:
        resp = (
            db.table("scan_jobs")
            .select("id,url,lead_id,client_id,priority,attempts,metadata")
            .eq("status", "queued")
            .order("priority", desc=True)
            .order("enqueued_at", desc=False)
            .limit(batch)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        _log("poll_error", level=logging.ERROR, error=str(e))
        return []


def _claim_job(db: Client, job_id: int, attempts: int) -> bool:
    """Optimistic claim — only succeeds if status is still 'queued' at update time."""
    try:
        resp = (
            db.table("scan_jobs")
            .update({"status": "running", "started_at": _now_iso(), "attempts": attempts + 1})
            .eq("id", job_id)
            .eq("status", "queued")
            .execute()
        )
        return bool(resp.data)
    except Exception as e:
        _log("claim_error", level=logging.WARNING, job_id=job_id, error=str(e))
        return False


def _insert_result(db: Client, job: dict, scan: dict) -> int | None:
    """Append-only INSERT into scan_results. Never upsert."""
    row = {
        "lead_id": job.get("lead_id"),
        "client_id": job.get("client_id"),
        "url": job["url"],
        "scanned_at": _now_iso(),
        "axe_version": AXE_VERSION,
        "scan_status": scan["status"],
        "error": scan.get("error"),
        "accessibility_score": scan.get("accessibility_score"),
        "critical_count": scan.get("critical_count", 0),
        "serious_count": scan.get("serious_count", 0),
        "moderate_count": scan.get("moderate_count", 0),
        "minor_count": scan.get("minor_count", 0),
        # Pass native list/dict — postgrest serializes these as real jsonb (not string).
        "violations": scan.get("violations", []),
        "incomplete_count": scan.get("incomplete_count", 0),
        "incomplete": scan.get("incomplete", []),
        "score_formula_version": scan.get("score_formula_version"),
        "metadata": {"worker_id": WORKER_ID},
    }
    try:
        resp = db.table("scan_results").insert(row).execute()
        if resp.data:
            return resp.data[0]["id"]
    except Exception as e:
        _log("insert_result_error", level=logging.ERROR,
             url=job["url"], error=str(e))
    return None


def _finish_job(
    db: Client, job_id: int, *, done: bool,
    result_id: int | None = None, error: str | None = None,
) -> None:
    update: dict[str, Any] = {
        "status": "done" if done else "error",
        "finished_at": _now_iso(),
    }
    if result_id is not None:
        update["scan_result_id"] = result_id
    if error:
        update["error"] = error[:1000]
    try:
        db.table("scan_jobs").update(update).eq("id", job_id).execute()
    except Exception as e:
        _log("finish_job_error", level=logging.ERROR, job_id=job_id, error=str(e))


# ── Process one claimed job ───────────────────────────────────────────────────
def _process_job(job: dict, axe_js: str, db: Client) -> None:
    job_id = job["id"]
    url = job["url"]
    _log("job_start", job_id=job_id, url=url)

    scan = _scan_url(url, axe_js)

    if scan["status"] == "error":
        # Never store a fake passing result — error path only.
        _finish_job(db, job_id, done=False, error=scan.get("error"))
        _log("job_error", job_id=job_id, url=url, error=(scan.get("error") or "")[:200])
        return

    result_id = _insert_result(db, job, scan)
    if result_id is None:
        _finish_job(db, job_id, done=False, error="scan_results insert failed")
        _log("job_insert_failed", level=logging.ERROR, job_id=job_id, url=url)
        return

    _finish_job(db, job_id, done=True, result_id=result_id)
    _log(
        "job_done", job_id=job_id, url=url,
        score=scan.get("accessibility_score"),
        critical=scan.get("critical_count"),
        result_id=result_id,
    )


# ── Main poll loop ────────────────────────────────────────────────────────────
def main() -> int:
    _log(
        "daemon_starting",
        supabase_url=(SUPABASE_URL[:40] + "...") if len(SUPABASE_URL) > 40 else SUPABASE_URL,
        concurrency=SCAN_CONCURRENCY,
        batch_size=SCAN_BATCH_SIZE,
        poll_interval_sec=POLL_INTERVAL_SEC,
        axe_version=AXE_VERSION,
    )

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        _log("config_error", level=logging.ERROR,
             detail="SUPABASE_URL and SUPABASE_SERVICE_KEY are required")
        return 2

    if not AXE_PATH.exists():
        _log("config_error", level=logging.ERROR,
             detail=f"axe.min.js not found at {AXE_PATH}")
        return 2

    axe_js = _load_axe()
    _log("axe_loaded", size_bytes=len(axe_js.encode("utf-8")), version=AXE_VERSION)

    db = _make_db()

    while not _shutdown:
        try:
            jobs = _poll_queued(db, SCAN_BATCH_SIZE)

            if not jobs:
                for _ in range(POLL_INTERVAL_SEC):
                    if _shutdown:
                        break
                    time.sleep(1)
                continue

            _log("jobs_fetched", count=len(jobs))

            claimed = []
            for job in jobs:
                if _shutdown:
                    break
                if _claim_job(db, job["id"], job.get("attempts", 0)):
                    claimed.append(job)
                else:
                    _log("claim_skipped", job_id=job["id"])

            if not claimed:
                time.sleep(1)
                continue

            _log("jobs_claimed", count=len(claimed))

            with ThreadPoolExecutor(max_workers=SCAN_CONCURRENCY) as ex:
                futs = {
                    ex.submit(_process_job, job, axe_js, db): job["id"]
                    for job in claimed
                    if not _shutdown
                }
                for fut in as_completed(futs):
                    try:
                        fut.result()
                    except Exception as e:
                        job_id = futs[fut]
                        _log("job_unhandled_exception", level=logging.ERROR,
                             job_id=job_id, error=str(e))
                        _finish_job(db, job_id, done=False,
                                    error=f"unhandled: {str(e)[:400]}")

        except Exception as e:
            _log("main_loop_exception", level=logging.ERROR, error=str(e))
            time.sleep(POLL_INTERVAL_SEC)

    _log("daemon_exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
