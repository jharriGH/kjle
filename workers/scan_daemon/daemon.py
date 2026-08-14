"""
KJLE — WebSignalz Scan Daemon (Phase 3)
File: workers/scan_daemon/daemon.py

Polls scan_jobs for queued URLs, runs headless Chromium + vendored axe-core 4.10.2,
and writes append-only rows to scan_results. Direct Supabase access via service_role key.

Required env vars:
  SUPABASE_URL           Supabase project URL
  SUPABASE_SERVICE_KEY   service_role key (never anon key)
  SCAN_CONCURRENCY       max parallel browser contexts (default: 4, hard cap: 16)
  POLL_INTERVAL_SEC      seconds between polls when queue is empty (default: 15)
  SCAN_BATCH_SIZE        jobs claimed per poll cycle (default: SCAN_CONCURRENCY)
  WORKER_ID              label in metadata.worker_id (default: scan-daemon)
  LOG_LEVEL              default: INFO
  SCAN_NAV_TIMEOUT_MS    per-navigation Playwright timeout ms (default: 30000)
  SCAN_TOTAL_TIMEOUT_S   per-lead overall scan ceiling seconds (default: 60)
  STALL_THRESHOLD_S      idle-with-queue threshold before self-restart (default: 300)
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from supabase import create_client, Client

from url_guard import is_url_safe, check_redirect_chain, UNREACHABLE_REASONS

# ── Stealth browser constants (headless-detection hardening) ─────────────────
# Desktop Windows Chrome UA — avoids "HeadlessChrome" / "Linux" UA signals that
# Wordfence and Cloudflare use to identify headless scanners.
_STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Headers a real Chrome desktop sends; absence of these is a headless tell.
_STEALTH_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}
# IIFE executed before any page JS: masks navigator.webdriver and other
# headless tells that Wordfence/bot-detection scripts check.
_STEALTH_INIT = """(() => {
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true});
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const a = [
                {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'Portable Document Format'},
                {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:''},
                {name:'Native Client',filename:'internal-nacl-plugin',description:''},
            ];
            a.__proto__ = PluginArray.prototype;
            return a;
        },
        configurable: true,
    });
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en'], configurable: true});
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
})();"""

# ── Configuration ────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
# Hard cap at 4 — each Chromium instance uses ~350-500 MB system RAM.
SCAN_CONCURRENCY = min(int(os.environ.get("SCAN_CONCURRENCY", "4")), 16)
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "15"))
SCAN_BATCH_SIZE = int(os.environ.get("SCAN_BATCH_SIZE", str(SCAN_CONCURRENCY)))
WORKER_ID = os.environ.get("WORKER_ID", "scan-daemon").strip()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Per-navigation hard cap — bounds page.goto and EVERY Playwright op on the page
# (set via set_default_timeout after page creation).
SCAN_NAV_TIMEOUT_MS = int(os.environ.get("SCAN_NAV_TIMEOUT_MS", "30000"))
# Per-lead total ceiling — thread-level; frees the worker slot even if the
# browser thread is still winding down after a pathological page.
SCAN_TOTAL_TIMEOUT_S = int(os.environ.get("SCAN_TOTAL_TIMEOUT_S", "60"))
# Stall threshold: if no jobs complete for this many seconds while the queue is
# non-empty, the watchdog exits with code 1 so systemd restarts the daemon fresh.
STALL_THRESHOLD_S = int(os.environ.get("STALL_THRESHOLD_S", "300"))

AXE_VERSION = "4.10.2"
AXE_PATH = Path(__file__).parent / "axe.min.js"

# ── Stall watchdog state (module-level, GIL-safe for scalar writes) ──────────
_last_job_completed_at: float = 0.0    # monotonic; updated on every terminal job state
_queue_had_jobs: bool = False           # True if last poll returned queued jobs
_daemon_start_time: float = 0.0        # set in main() before watchdog starts
_at_least_one_completion: bool = False  # prevents stall-detect before first job done
_WATCHDOG_CHECK_S = 60
_STARTUP_GRACE_S = 120

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
# K=60 validated against 149 real small-business lead scans (2026-07-15): score
# distribution spans 0-100, mean ~54, mode 60-70, no pile-up at 0 or ceiling.
# Any future change to K or the weights MUST bump SCORE_FORMULA_VERSION — changing
# the formula invalidates stored trend history.
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

    # ── Layer 1: pre-browser preflight redirect-chain check ───────────────────
    # Follow the server-side redirect chain with Python http.client (no browser)
    # and check every hop with is_url_safe() BEFORE the browser launches.
    # This guarantees the browser never connects to an internal host even when
    # the initial URL is an external/safe server that 302-redirects internally
    # (the proven Slice 14 attack path: ctx.route does NOT fire for navigation
    # redirect hops, so the route handler alone cannot catch this).
    chain_safe, chain_reason = check_redirect_chain(url)
    if not chain_safe:
        _log("ssrf_blocked", level=logging.WARNING,
             url=url, reason=chain_reason, layer="preflight_redirect")
        return {
            "status": "blocked",
            "error": f"ssrf_blocked: {chain_reason}",
            "accessibility_score": None,
            "critical_count": 0,
            "serious_count": 0,
            "moderate_count": 0,
            "minor_count": 0,
            "violations": [],
            "incomplete_count": 0,
            "incomplete": [],
            "score_formula_version": SCORE_FORMULA_VERSION,
        }

    # Per-scan SSRF intercept cache: (scheme, host, port) -> (safe, reason).
    # Shared between the route handler and request-event monitor.
    _ssrf_cache: dict[tuple, tuple] = {}
    _blocked: list[str] = [""]  # written by both route handler and request monitor
    _page_ref: list[Any] = [None]  # set after page creation for the request monitor

    # ── Layer 2a: ctx.route handler (Slice 11, kept) ──────────────────────────
    # Intercepts subresource requests and the FIRST navigation hop. Does NOT
    # fire for navigation redirect hops (proven Slice 14 investigation finding).
    def _route_handler(route: Any) -> None:
        # Fail CLOSED: any exception MUST abort; never let it become an open door.
        try:
            req_url = route.request.url
            parsed = urllib.parse.urlparse(req_url)
            if parsed.scheme not in ("http", "https"):
                route.continue_()
                return
            key = (parsed.scheme, parsed.hostname or "", parsed.port or 0)
            if key not in _ssrf_cache:
                _ssrf_cache[key] = is_url_safe(req_url)
            safe, reason = _ssrf_cache[key]
            if safe:
                route.continue_()
            elif reason in UNREACHABLE_REASONS:
                # Dead/unreachable domain (dns_resolution_failed) — NOT an SSRF threat.
                # Let the browser handle it naturally; killing the scan is wrong here.
                route.continue_()
            else:
                _blocked[0] = reason
                route.abort("blockedbyclient")
        except Exception:
            route.abort("blockedbyclient")

    # ── Layer 2b: page.on("request") kill-switch (new, Slice 14) ─────────────
    # page.on("request") fires for EVERY request the page makes, including
    # navigation redirect hops that ctx.route misses. When an unsafe URL is
    # detected, a daemon thread closes the page (spawned outside the event loop
    # to avoid deadlocking the sync Playwright dispatcher).
    def _on_request_monitor(request: Any) -> None:
        if _blocked[0]:
            return  # already aborting
        try:
            req_url = request.url
            parsed = urllib.parse.urlparse(req_url)
            if parsed.scheme not in ("http", "https"):
                return
            key = (parsed.scheme, parsed.hostname or "", parsed.port or 0)
            if key not in _ssrf_cache:
                _ssrf_cache[key] = is_url_safe(req_url)
            safe, reason = _ssrf_cache[key]
            if not safe:
                # Dead/unreachable domains (dns_resolution_failed) are not SSRF
                # threats — skip close-page so a dead analytics pixel doesn't kill the scan.
                if reason in UNREACHABLE_REASONS:
                    return
                _blocked[0] = reason
                page = _page_ref[0]
                if page is not None:
                    # Close the page from a separate thread: calling page.close()
                    # directly inside a Playwright event callback deadlocks the
                    # sync dispatcher. The thread exits the event loop context
                    # first, then issues the close through the dispatcher queue.
                    def _close_page() -> None:
                        try:
                            page.close()
                        except Exception:
                            pass
                    threading.Thread(target=_close_page, daemon=True).start()
        except Exception:
            pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent=_STEALTH_UA,
                    java_script_enabled=True,
                    extra_http_headers=_STEALTH_HEADERS,
                )
                # Mask navigator.webdriver and other headless tells before any page JS runs.
                ctx.add_init_script(_STEALTH_INIT)
                ctx.route("**/*", _route_handler)
                page = ctx.new_page()
                _page_ref[0] = page
                # Bound ALL Playwright operations on this page — goto, wait_for,
                # evaluate, etc. — so no single op can hang a worker indefinitely.
                page.set_default_timeout(SCAN_NAV_TIMEOUT_MS)
                page.set_default_navigation_timeout(SCAN_NAV_TIMEOUT_MS)
                page.on("request", _on_request_monitor)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=SCAN_NAV_TIMEOUT_MS)
                except Exception as _nav_err:
                    _nav_str = str(_nav_err)
                    _is_ssrf = (
                        _blocked[0]
                        or "ERR_BLOCKED_BY_CLIENT" in _nav_str
                        or "Target page, context or browser has been closed" in _nav_str
                        or "Page.goto: Target closed" in _nav_str
                    )
                    if _is_ssrf:
                        _reason = _blocked[0] or "ssrf_intercepted"
                        _log("ssrf_blocked", level=logging.WARNING,
                             url=url, reason=f"redirect_to_{_reason}",
                             layer="browser_intercept")
                        return {
                            "status": "blocked",
                            "error": f"ssrf_blocked: redirect_to_{_reason}",
                            "accessibility_score": None,
                            "critical_count": 0,
                            "serious_count": 0,
                            "moderate_count": 0,
                            "minor_count": 0,
                            "violations": [],
                            "incomplete_count": 0,
                            "incomplete": [],
                            "score_formula_version": SCORE_FORMULA_VERSION,
                        }
                    raise
                # ── Layer 3: post-nav final-URL check ─────────────────────────
                # Last-resort backstop: if the page landed on an internal URL
                # despite layers 1-2 (e.g., JS-driven redirect), block now.
                final_url = page.url
                if final_url and final_url not in ("about:blank", ""):
                    _final_safe, _final_reason = is_url_safe(final_url)
                    if not _final_safe:
                        _log("ssrf_blocked", level=logging.WARNING,
                             url=url, final_url=final_url, reason=_final_reason,
                             layer="post_nav_url_check")
                        return {
                            "status": "blocked",
                            "error": f"ssrf_blocked: post_nav_{_final_reason}",
                            "accessibility_score": None,
                            "critical_count": 0,
                            "serious_count": 0,
                            "moderate_count": 0,
                            "minor_count": 0,
                            "violations": [],
                            "incomplete_count": 0,
                            "incomplete": [],
                            "score_formula_version": SCORE_FORMULA_VERSION,
                        }
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


def _update_lead_summary(db: Client, job: dict, scan: dict) -> None:
    """Write accessibility summary back to leads. Isolated — never fails the scan job."""
    lead_id = job.get("lead_id")
    if not lead_id:
        return
    update: dict[str, Any] = {"accessibility_scanned_at": _now_iso()}
    if scan.get("status") == "ok":
        update["accessibility_score"] = scan.get("accessibility_score")
        update["accessibility_violations"] = len(scan.get("violations", []))
        update["accessibility_critical"] = scan.get("critical_count", 0)
    try:
        db.table("leads").update(update).eq("id", lead_id).execute()
    except Exception as e:
        _log("lead_summary_update_error", level=logging.WARNING,
             lead_id=lead_id, error=str(e))


# ── Watchdog helpers ──────────────────────────────────────────────────────────
def _mark_job_completed() -> None:
    """Update the stall-watchdog heartbeat. Call at every terminal job state."""
    global _last_job_completed_at, _at_least_one_completion
    _last_job_completed_at = time.monotonic()
    _at_least_one_completion = True


def _watchdog_loop() -> None:
    """
    Wakes every 60 s. If no jobs have completed for > STALL_THRESHOLD_S while the
    queue is non-empty, exits with code 1 so systemd (Restart=on-failure) restarts
    the daemon fresh — the proven recovery for all-workers-wedged deadlock.

    Conservative guards prevent false positives:
      - startup grace: skip first _STARTUP_GRACE_S seconds
      - at-least-one-completion: skip until the daemon has finished at least one job
      - queue guard: empty queue = idle (normal), not stalled
    """
    while not _shutdown:
        time.sleep(_WATCHDOG_CHECK_S)
        if _shutdown:
            break
        if time.monotonic() - _daemon_start_time < _STARTUP_GRACE_S:
            continue
        if not _at_least_one_completion:
            continue
        if not _queue_had_jobs:
            continue
        seconds_idle = time.monotonic() - _last_job_completed_at
        if seconds_idle > STALL_THRESHOLD_S:
            _log(
                "daemon_stall_detected",
                level=logging.CRITICAL,
                seconds_since_last_completion=int(seconds_idle),
                stall_threshold_s=STALL_THRESHOLD_S,
            )
            # Hard exit — sys.exit raises SystemExit in this thread only; os._exit
            # terminates the whole process so systemd actually restarts it.
            os._exit(1)


# ── Process one claimed job ───────────────────────────────────────────────────
def _process_job(job: dict, axe_js: str, db: Client) -> None:
    job_id = job["id"]
    url = job["url"]
    _log("job_start", job_id=job_id, url=url)

    # SSRF guard: reject private/loopback/cloud-metadata targets before browser launch
    _pre_safe, _pre_reason = is_url_safe(url)
    if not _pre_safe:
        _pre_scan: dict[str, Any]
        if _pre_reason in UNREACHABLE_REASONS:
            # Dead domain / DNS failure — not a security threat; label correctly.
            _log("unreachable", level=logging.WARNING, url=url, reason=_pre_reason)
            _pre_scan = {
                "status": "unreachable",
                "error": f"unreachable: {_pre_reason}",
                "accessibility_score": None,
                "critical_count": 0,
                "serious_count": 0,
                "moderate_count": 0,
                "minor_count": 0,
                "violations": [],
                "incomplete_count": 0,
                "incomplete": [],
                "score_formula_version": SCORE_FORMULA_VERSION,
            }
        else:
            # Genuine security block: loopback, private IP, cloud metadata, etc.
            _log("ssrf_blocked", level=logging.WARNING, url=url, reason=_pre_reason)
            _pre_scan = {
                "status": "blocked",
                "error": f"ssrf_blocked: {_pre_reason}",
                "accessibility_score": None,
                "critical_count": 0,
                "serious_count": 0,
                "moderate_count": 0,
                "minor_count": 0,
                "violations": [],
                "incomplete_count": 0,
                "incomplete": [],
                "score_formula_version": SCORE_FORMULA_VERSION,
            }
        result_id = _insert_result(db, job, _pre_scan)
        _finish_job(db, job_id, done=True, result_id=result_id)
        _update_lead_summary(db, job, _pre_scan)
        _mark_job_completed()
        return

    # Per-scan ceiling: run _scan_url in a daemon thread so this worker slot is
    # freed after SCAN_TOTAL_TIMEOUT_S even if the browser is still winding down.
    # page.set_default_timeout (set inside _scan_url) ensures all Playwright ops
    # time out on their own, so the daemon thread terminates and the browser context
    # is closed via the existing finally block — no permanent leak.
    _scan_result: list[dict] = []
    _scan_exc: list[BaseException] = []

    def _run_scan() -> None:
        try:
            _scan_result.append(_scan_url(url, axe_js))
        except BaseException as exc:
            _scan_exc.append(exc)

    _t = threading.Thread(target=_run_scan, daemon=True)
    _t.start()
    _t.join(timeout=SCAN_TOTAL_TIMEOUT_S)

    if _t.is_alive():
        # Ceiling hit: free the worker slot now; browser thread will clean up when
        # Playwright ops time out (within SCAN_NAV_TIMEOUT_MS ms).
        _log("scan_total_timeout", level=logging.WARNING, url=url,
             timeout_s=SCAN_TOTAL_TIMEOUT_S, job_id=job_id)
        _finish_job(db, job_id, done=False, error="scan_timeout")
        _update_lead_summary(db, job, {"status": "error", "error": "scan_timeout"})
        _mark_job_completed()
        return

    if _scan_exc:
        raise _scan_exc[0]

    scan = _scan_result[0] if _scan_result else {
        "status": "error", "error": "scan_no_result", "violations": [],
    }

    if scan["status"] == "blocked":
        # Redirect-to-internal SSRF block; already logged inside _scan_url.
        result_id = _insert_result(db, job, scan)
        _finish_job(db, job_id, done=True, result_id=result_id)
        _update_lead_summary(db, job, scan)
        _mark_job_completed()
        return

    if scan["status"] == "error":
        # Never store a fake passing result — error path only.
        _finish_job(db, job_id, done=False, error=scan.get("error"))
        _log("job_error", job_id=job_id, url=url, error=(scan.get("error") or "")[:200])
        _update_lead_summary(db, job, scan)
        _mark_job_completed()
        return

    result_id = _insert_result(db, job, scan)
    if result_id is None:
        _finish_job(db, job_id, done=False, error="scan_results insert failed")
        _log("job_insert_failed", level=logging.ERROR, job_id=job_id, url=url)
        _mark_job_completed()
        return

    _finish_job(db, job_id, done=True, result_id=result_id)
    _log(
        "job_done", job_id=job_id, url=url,
        score=scan.get("accessibility_score"),
        critical=scan.get("critical_count"),
        result_id=result_id,
    )
    _update_lead_summary(db, job, scan)
    _mark_job_completed()


# ── Main poll loop ────────────────────────────────────────────────────────────
def main() -> int:
    _log(
        "daemon_starting",
        supabase_url=(SUPABASE_URL[:40] + "...") if len(SUPABASE_URL) > 40 else SUPABASE_URL,
        concurrency=SCAN_CONCURRENCY,
        batch_size=SCAN_BATCH_SIZE,
        poll_interval_sec=POLL_INTERVAL_SEC,
        axe_version=AXE_VERSION,
        scan_nav_timeout_ms=SCAN_NAV_TIMEOUT_MS,
        scan_total_timeout_s=SCAN_TOTAL_TIMEOUT_S,
        stall_threshold_s=STALL_THRESHOLD_S,
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

    global _daemon_start_time, _queue_had_jobs
    _daemon_start_time = time.monotonic()
    _watchdog = threading.Thread(target=_watchdog_loop, daemon=True, name="stall-watchdog")
    _watchdog.start()
    _log("watchdog_started", stall_threshold_s=STALL_THRESHOLD_S,
         startup_grace_s=_STARTUP_GRACE_S, check_interval_s=_WATCHDOG_CHECK_S)

    while not _shutdown:
        try:
            jobs = _poll_queued(db, SCAN_BATCH_SIZE)
            _queue_had_jobs = bool(jobs)  # watchdog: non-empty = active work expected

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
