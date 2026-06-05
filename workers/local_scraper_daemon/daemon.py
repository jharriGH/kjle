"""
KJLE — Local Scraper worker daemon (Phase 4.2 Dispatch 2)
File: workers/local_scraper_daemon/daemon.py

Architecture: reverse polling. The worker box (Windows Z820 or Linux VPS) runs
this daemon, which polls KJLE every POLL_INTERVAL_SEC for queued scrape jobs,
spawns LocalScraper.exe with the job's CLI args, waits for completion, then
ingests the resulting lead JSON back into KJLE via the existing HMAC-signed
/kjle/v1/ingest/localscraper endpoint.

Workers have NO inbound public URL — everything is worker-initiated.

Required env vars (read at startup, never logged):
  KJLE_API_BASE                  default: https://kjle-api.onrender.com
  KJLE_WORKER_API_KEY            required (sent as X-Worker-API-Key)
  LOCAL_SCRAPER_EXE_PATH         full path to LocalScraper.exe
  LOCAL_SCRAPER_SAVE_DIR         where LS will save scrape JSON files
  LOCAL_SCRAPER_WEBHOOK_SECRET   required (HMAC secret for ingest)
  WORKER_ID                      'z820' or 'vps'
  POLL_INTERVAL_SEC              default: 15
  WEBHOOK_HANDLER_PORT           default: 8765
  LOG_LEVEL                      default: INFO

Stdlib + requests only — keep this trivially deployable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ── Configuration ───────────────────────────────────────────────────────────
KJLE_API_BASE = os.environ.get(
    "KJLE_API_BASE", "https://kjle-api.onrender.com"
).rstrip("/")
KJLE_WORKER_API_KEY = os.environ.get("KJLE_WORKER_API_KEY", "").strip()
LOCAL_SCRAPER_EXE_PATH = os.environ.get("LOCAL_SCRAPER_EXE_PATH", "").strip()
LOCAL_SCRAPER_SAVE_DIR = os.environ.get("LOCAL_SCRAPER_SAVE_DIR", "").strip()
LOCAL_SCRAPER_WEBHOOK_SECRET = os.environ.get(
    "LOCAL_SCRAPER_WEBHOOK_SECRET", ""
).strip()
WORKER_ID = os.environ.get("WORKER_ID", "").strip().lower()
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "15"))
WEBHOOK_HANDLER_PORT = int(os.environ.get("WEBHOOK_HANDLER_PORT", "8765"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Endpoint paths (KJLE base is prefixed at runtime).
EP_POLL = "/kjle/v1/scrape/jobs/poll"
EP_STARTED = "/kjle/v1/scrape/jobs/{job_id}/started"
EP_COMPLETE = "/kjle/v1/scrape/jobs/{job_id}/complete"
EP_INGEST = "/kjle/v1/ingest/localscraper"

WEBHOOK_TIMEOUT_SEC = 60         # seconds to wait for LS webhook marker
WEBHOOK_HANDLER_MAX_MIN = 90     # ls-webhook-handler.py self-timeout
HTTP_TIMEOUT_SEC = 30
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 2.0           # exponential backoff base

# ── Logging (single-line JSON) ──────────────────────────────────────────────
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "worker_id": WORKER_ID or "unknown",
            "event": record.getMessage(),
        }
        extras = getattr(record, "extra_fields", None)
        if isinstance(extras, dict):
            payload.update(extras)
        return json.dumps(payload, default=str)


def _build_logger() -> logging.Logger:
    lg = logging.getLogger("ls_daemon")
    lg.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_JsonFormatter())
    lg.handlers = [h]
    lg.propagate = False
    return lg


log = _build_logger()


def _log(event: str, level: int = logging.INFO, **fields: Any) -> None:
    rec = logging.LogRecord(
        name="ls_daemon", level=level, pathname="", lineno=0,
        msg=event, args=None, exc_info=None,
    )
    rec.extra_fields = fields
    log.handle(rec)


# ── Graceful shutdown ───────────────────────────────────────────────────────
_shutdown_requested = False


def _sigterm_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    _log("sigterm_received", signum=signum)


signal.signal(signal.SIGTERM, _sigterm_handler)
signal.signal(signal.SIGINT, _sigterm_handler)


# ── HTTP helpers ────────────────────────────────────────────────────────────
def _worker_headers() -> dict[str, str]:
    return {
        "X-Worker-API-Key": KJLE_WORKER_API_KEY,
        "Content-Type": "application/json",
    }


def _hmac_sign(body_bytes: bytes) -> str:
    digest = hmac.new(
        LOCAL_SCRAPER_WEBHOOK_SECRET.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def _request_with_retry(
    method: str, url: str, *, headers: dict[str, str] | None = None,
    json_body: dict | None = None, data: bytes | None = None,
    timeout: int = HTTP_TIMEOUT_SEC,
) -> requests.Response | None:
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.request(
                method, url, headers=headers, json=json_body, data=data,
                timeout=timeout,
            )
            if 200 <= resp.status_code < 300:
                return resp
            # 4xx (except 429) — don't retry, return as-is for caller to handle.
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                return resp
            _log(
                "http_retryable_status",
                level=logging.WARNING,
                url=url, status=resp.status_code, attempt=attempt,
            )
        except requests.RequestException as e:
            last_exc = e
            _log(
                "http_request_exception",
                level=logging.WARNING,
                url=url, error=str(e), attempt=attempt,
            )
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    if last_exc is not None:
        _log("http_giving_up", level=logging.ERROR,
             url=url, error=str(last_exc))
    return None


# ── KJLE API wrappers ───────────────────────────────────────────────────────
def poll_for_jobs() -> list[dict]:
    url = f"{KJLE_API_BASE}{EP_POLL}"
    params = {"worker_id": WORKER_ID, "max_jobs": 1}
    try:
        resp = requests.get(
            url, headers=_worker_headers(), params=params,
            timeout=HTTP_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        _log("poll_exception", level=logging.WARNING, error=str(e))
        return []
    if resp.status_code != 200:
        _log(
            "poll_non_200",
            level=logging.WARNING,
            status=resp.status_code,
            body_preview=resp.text[:200],
        )
        return []
    try:
        data = resp.json()
    except ValueError:
        _log("poll_bad_json", level=logging.WARNING,
             body_preview=resp.text[:200])
        return []
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    if not isinstance(jobs, list):
        return []
    return jobs


def post_started(job_id: str) -> bool:
    """Body shape matches ScrapeStartedRequest: {worker_id}."""
    url = f"{KJLE_API_BASE}{EP_STARTED.format(job_id=job_id)}"
    resp = _request_with_retry(
        "POST", url, headers=_worker_headers(),
        json_body={"worker_id": WORKER_ID},
    )
    ok = resp is not None and 200 <= resp.status_code < 300
    _log("job_started_post", job_id=job_id, success=ok,
         status=(resp.status_code if resp is not None else None))
    return ok


def post_complete(job_id: str, *, success: bool,
                  run_id: str | None = None,
                  error: str | None = None,
                  result_counts: dict | None = None) -> bool:
    """
    Body shape matches ScrapeCompleteRequest:
      worker_id, success, result_run_id, result_total_records,
      result_inserted, result_duplicates, result_filtered, error_message
    result_counts uses the ingest response keys (results_count, inserted_count,
    duplicate_count, filtered_count) — translated to schema names here.
    """
    url = f"{KJLE_API_BASE}{EP_COMPLETE.format(job_id=job_id)}"
    body: dict[str, Any] = {
        "worker_id": WORKER_ID,
        "success": success,
    }
    if run_id:
        body["result_run_id"] = run_id
    if error:
        body["error_message"] = error
    if result_counts:
        if "results_count" in result_counts:
            body["result_total_records"] = result_counts["results_count"]
        if "inserted_count" in result_counts:
            body["result_inserted"] = result_counts["inserted_count"]
        if "duplicate_count" in result_counts:
            body["result_duplicates"] = result_counts["duplicate_count"]
        if "filtered_count" in result_counts:
            body["result_filtered"] = result_counts["filtered_count"]
    resp = _request_with_retry(
        "POST", url, headers=_worker_headers(), json_body=body,
    )
    ok = resp is not None and 200 <= resp.status_code < 300
    _log("job_complete_post", job_id=job_id, success_flag=success,
         post_ok=ok,
         status=(resp.status_code if resp is not None else None))
    return ok


def post_ingest(payload: dict) -> tuple[bool, dict | None]:
    url = f"{KJLE_API_BASE}{EP_INGEST}"
    body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": _hmac_sign(body_bytes),
    }
    resp = _request_with_retry(
        "POST", url, headers=headers, data=body_bytes,
    )
    if resp is None:
        return False, None
    ok = 200 <= resp.status_code < 300
    try:
        body = resp.json()
    except ValueError:
        body = None
    _log("ingest_post", run_id=payload.get("run_id"),
         success=ok, status=resp.status_code,
         response_preview=str(body)[:300] if body else None)
    return ok, body


# ── LocalScraper job execution ──────────────────────────────────────────────
def _build_ls_cli_args(
    exe_path: str, job: dict, save_dir: Path, webhook_url: str,
) -> list[str]:
    """Map KJLE job fields → LocalScraper CLI args."""
    args: list[str] = [exe_path]

    def _add(flag: str, value: Any) -> None:
        if value is None:
            return
        s = str(value).strip()
        if s:
            args.extend([flag, s])

    _add("--target", job.get("target"))
    _add("--keyword", job.get("keyword"))
    _add("--location", job.get("location"))
    _add("--keyword-list", job.get("keyword_list"))
    _add("--location-list", job.get("location_list"))
    _add("--custom-url", job.get("custom_url"))

    settings_file = job.get("settings_file")
    if settings_file:
        _add("--settings-file", settings_file)

    args.extend(["--save-location", str(save_dir)])
    args.extend(["--webhook-url", webhook_url])
    args.append("--webhook-include-data")
    args.append("--auto-close")
    return args


def _spawn_webhook_handler(
    save_dir: Path, run_id: str, port: int,
) -> subprocess.Popen | None:
    handler_path = Path(__file__).parent / "ls-webhook-handler.py"
    if not handler_path.exists():
        _log("webhook_handler_missing", level=logging.ERROR,
             path=str(handler_path))
        return None
    env = os.environ.copy()
    env["LS_HANDLER_SAVE_DIR"] = str(save_dir)
    env["LS_HANDLER_RUN_ID"] = run_id
    env["LS_HANDLER_PORT"] = str(port)
    env["LS_HANDLER_MAX_MINUTES"] = str(WEBHOOK_HANDLER_MAX_MIN)
    try:
        # Use the same interpreter that runs the daemon — works on Windows + Linux.
        return subprocess.Popen(
            [sys.executable, str(handler_path)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        _log("webhook_handler_spawn_failed", level=logging.ERROR,
             error=str(e))
        return None


def _wait_for_marker(marker_path: Path, timeout_sec: int) -> dict | None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if marker_path.exists():
            try:
                with open(marker_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError) as e:
                _log("marker_read_failed", level=logging.WARNING,
                     path=str(marker_path), error=str(e))
                return None
        if _shutdown_requested:
            return None
        time.sleep(0.5)
    return None


def _load_saved_lead_files(save_dir: Path) -> list[dict]:
    """Read every .json file LocalScraper dropped in save_dir, return merged list."""
    out: list[dict] = []
    if not save_dir.exists():
        return out
    for child in sorted(save_dir.glob("**/*.json")):
        # Skip the webhook marker itself (it lives in the same dir).
        if child.name.endswith("_webhook.json"):
            continue
        try:
            with open(child, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            _log("lead_file_read_failed", level=logging.WARNING,
                 path=str(child), error=str(e))
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    out.append(item)
        elif isinstance(data, dict):
            # Some LS outputs nest results under a key.
            inner = data.get("results") or data.get("leads") or data.get("data")
            if isinstance(inner, list):
                for item in inner:
                    if isinstance(item, dict):
                        out.append(item)
            else:
                out.append(data)
    return out


def _settings_from_job(job: dict) -> dict:
    """Echo back the job parameters as the 'settings' block on ingest.
    Keys mirror api/routes/scrape_jobs.py::ScrapeStartRequest."""
    keys = (
        "target", "keyword", "location", "keyword_list", "location_list",
        "custom_url", "max_listings", "find_emails", "correlation_id",
    )
    return {k: job[k] for k in keys if k in job and job[k] is not None}


def process_job(job: dict) -> None:
    job_id = str(job.get("id") or job.get("job_id") or "")
    if not job_id:
        _log("job_missing_id", level=logging.ERROR, raw=str(job)[:300])
        return

    run_id = str(job.get("run_id") or uuid.uuid4())
    save_root = Path(LOCAL_SCRAPER_SAVE_DIR)
    save_dir = save_root / f"job_{job_id}"
    save_dir.mkdir(parents=True, exist_ok=True)
    marker_path = save_dir / f"{run_id}_webhook.json"
    webhook_url = f"http://127.0.0.1:{WEBHOOK_HANDLER_PORT}/ls"

    _log("job_picked_up", job_id=job_id, run_id=run_id,
         save_dir=str(save_dir))

    post_started(job_id)

    if not LOCAL_SCRAPER_EXE_PATH or not Path(LOCAL_SCRAPER_EXE_PATH).exists():
        msg = "LocalScraper.exe missing at configured path"
        _log("ls_exe_missing", level=logging.ERROR, job_id=job_id,
             configured_path=LOCAL_SCRAPER_EXE_PATH)
        post_complete(job_id, success=False, run_id=run_id, error=msg)
        _cleanup_save_dir(save_dir)
        return

    handler_proc = _spawn_webhook_handler(
        save_dir, run_id, WEBHOOK_HANDLER_PORT,
    )
    if handler_proc is None:
        msg = "webhook handler failed to spawn"
        post_complete(job_id, success=False, run_id=run_id, error=msg)
        _cleanup_save_dir(save_dir)
        return

    ls_args = _build_ls_cli_args(
        LOCAL_SCRAPER_EXE_PATH, job, save_dir, webhook_url,
    )
    _log("ls_spawning", job_id=job_id, exe=LOCAL_SCRAPER_EXE_PATH,
         arg_count=len(ls_args))

    ls_proc_rc: int | None = None
    try:
        ls_proc = subprocess.run(ls_args, capture_output=True, text=True)
        ls_proc_rc = ls_proc.returncode
    except OSError as e:
        _log("ls_spawn_failed", level=logging.ERROR, job_id=job_id,
             error=str(e))
        _terminate_handler(handler_proc)
        post_complete(job_id, success=False, run_id=run_id,
                      error=f"ls_spawn_failed: {e}")
        _cleanup_save_dir(save_dir)
        return

    _log("ls_exited", job_id=job_id, returncode=ls_proc_rc)

    marker = _wait_for_marker(marker_path, WEBHOOK_TIMEOUT_SEC)
    _terminate_handler(handler_proc)

    if marker is None:
        msg = (f"no webhook marker after LS exit "
               f"(rc={ls_proc_rc}, timeout={WEBHOOK_TIMEOUT_SEC}s)")
        _log("marker_timeout", level=logging.WARNING, job_id=job_id)
        post_complete(job_id, success=False, run_id=run_id, error=msg)
        _cleanup_save_dir(save_dir)
        return

    leads = _load_saved_lead_files(save_dir)
    _log("leads_loaded", job_id=job_id, lead_count=len(leads))

    payload = {
        "event": "scrape_complete",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "worker_id": WORKER_ID,
        "scraper_id": "local_scraper",
        "run_id": run_id,
        "settings": _settings_from_job(job),
        "results": leads,
    }

    ingest_ok, ingest_resp = post_ingest(payload)
    if not ingest_ok:
        post_complete(job_id, success=False, run_id=run_id,
                      error="ingest_post_failed")
        _cleanup_save_dir(save_dir)
        return

    result_counts: dict[str, Any] = {"results_count": len(leads)}
    if isinstance(ingest_resp, dict):
        for k in ("inserted_count", "duplicate_count", "filtered_count",
                  "results_count"):
            if k in ingest_resp:
                result_counts[k] = ingest_resp[k]

    post_complete(job_id, success=True, run_id=run_id,
                  result_counts=result_counts)
    _cleanup_save_dir(save_dir)
    _log("job_done", job_id=job_id, result_counts=result_counts)


def _terminate_handler(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except OSError as e:
        _log("handler_terminate_error", level=logging.WARNING, error=str(e))


def _cleanup_save_dir(save_dir: Path) -> None:
    try:
        shutil.rmtree(save_dir, ignore_errors=True)
    except OSError as e:
        _log("cleanup_failed", level=logging.WARNING,
             path=str(save_dir), error=str(e))


# ── Startup validation ─────────────────────────────────────────────────────
def _validate_config() -> list[str]:
    errors: list[str] = []
    if not KJLE_WORKER_API_KEY:
        errors.append("KJLE_WORKER_API_KEY not set")
    if not LOCAL_SCRAPER_WEBHOOK_SECRET:
        errors.append("LOCAL_SCRAPER_WEBHOOK_SECRET not set")
    if WORKER_ID not in ("z820", "vps"):
        errors.append(f"WORKER_ID must be 'z820' or 'vps' (got '{WORKER_ID}')")
    if not LOCAL_SCRAPER_SAVE_DIR:
        errors.append("LOCAL_SCRAPER_SAVE_DIR not set")
    if not LOCAL_SCRAPER_EXE_PATH:
        # Soft: we still run (VPS may not have LS installed yet — daemon will
        # error-out per-job cleanly rather than refuse to start).
        _log("ls_exe_path_unset_soft",
             level=logging.WARNING,
             note="daemon will start but jobs will fail until LS is installed")
    return errors


def main() -> int:
    _log("daemon_starting",
         kjle_api_base=KJLE_API_BASE,
         worker_id=WORKER_ID,
         poll_interval_sec=POLL_INTERVAL_SEC,
         save_dir=LOCAL_SCRAPER_SAVE_DIR,
         webhook_port=WEBHOOK_HANDLER_PORT)

    errors = _validate_config()
    if errors:
        for e in errors:
            _log("config_error", level=logging.ERROR, detail=e)
        return 2

    save_root = Path(LOCAL_SCRAPER_SAVE_DIR)
    try:
        save_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _log("save_dir_create_failed", level=logging.ERROR, error=str(e))
        return 2

    while not _shutdown_requested:
        try:
            jobs = poll_for_jobs()
            if jobs:
                _log("jobs_received", count=len(jobs))
                for job in jobs:
                    if _shutdown_requested:
                        _log("shutdown_after_job_break")
                        break
                    process_job(job)
            else:
                # No jobs — sleep then continue.
                for _ in range(POLL_INTERVAL_SEC):
                    if _shutdown_requested:
                        break
                    time.sleep(1)
        except Exception as e:
            _log("main_loop_exception", level=logging.ERROR, error=str(e))
            time.sleep(POLL_INTERVAL_SEC)

    _log("daemon_exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
