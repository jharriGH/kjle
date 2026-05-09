#!/usr/bin/env python3
"""
KJLE — Pain score backfill (cursor-based pagination + retry + checkpoint)
File: scripts/recompute_pain.py

Recomputes pain_score for every active lead that is not yet on the current
formula version. Reuses compute_pain_score_v1 from scripts/ingest.py — single
source of truth for the formula.

Pagination is cursor-based ("WHERE id > last_id ORDER BY id LIMIT N"), which
is constant-time per query regardless of progress. The previous offset-based
implementation hit Postgres error 57014 (statement_timeout) at offset 94000
because OFFSET-based scans are O(N²) on large tables.

Server-side filter:
    is_active = TRUE
    AND (pain_score_version IS NULL OR pain_score_version < 2)

This skips leads already on v2, so:
  - Re-running after a partial backfill is safe and fast (no duplicate work)
  - Adding a future v3 formula re-uses this script with the filter bumping to
    "< 3" automatically once compute_pain_score_v1 returns version=3

Usage:
    # Required env vars:
    export SUPABASE_URL=https://...supabase.co
    export SUPABASE_SERVICE_KEY=eyJ...

    # Smoke test on 5 leads, no DB writes:
    python scripts/recompute_pain.py --dry-run --limit 5

    # Full backfill (auto-resumes from checkpoint if present):
    python scripts/recompute_pain.py

    # Force a fresh start, ignore any checkpoint:
    python scripts/recompute_pain.py --restart

    # Resume from a specific UUID (manual override):
    python scripts/recompute_pain.py --start-after-id "abc-123-..."

    # Run the unit tests for retry + checkpoint helpers (no DB needed):
    python scripts/recompute_pain.py --self-test

After full completion, trigger the classifier to re-bucket HOT/WARM/COLD:
    POST https://kjle-api.onrender.com/kjle/v1/scheduler/run/classify_segments

Behavior matrix:
    --start-after-id UUID  → use it, ignore checkpoint
    --restart              → use None, ignore + clear checkpoint, log warning
    checkpoint file exists → resume from its last_id
    nothing                → start from None (beginning)

On clean completion (zero per-lead failures), the checkpoint is deleted.
On any abort or per-lead failures, the checkpoint stays for the next resume.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

# Make sibling scripts/ingest.py importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

CHECKPOINT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".recompute_pain_checkpoint.json",
)

# Server-side column filter — match leads NOT yet on the current formula.
# Bump the .lt.<n> when shipping a new formula version (compute_pain_score_v1
# already writes pain_score_version=N).
VERSION_FILTER_OR = "pain_score_version.is.null,pain_score_version.lt.2"

# Fields the formula reads. We don't pull the whole row (100+ columns).
SELECT_FIELDS = ", ".join([
    "id", "niche_slug",
    # reputation
    "google_stars", "google_review_count", "g_maps_claimed",
    # seo
    "seo_schema_present", "mobile_friendly",
    "google_analytics", "google_pixel", "google_rank",
    # social
    "facebook_url", "instagram_url",
    "ads_facebook", "ads_adwords", "facebook_stars",
    # website
    "website", "uses_wordpress", "uses_shopify", "domain_expired",
    # bizintel
    "domain_expiring_soon", "email_state",
])

RETRYABLE_PATTERNS = (
    "57014",                # Postgres statement_timeout
    "timeout", "Timeout",
    " 503", " 504",         # PostgREST gateway / upstream errors
    "ConnectionError", "ConnectionResetError",
    "ReadTimeout", "RemoteProtocolError",
    "ServerDisconnectedError",
)

DEFAULT_BATCH_SIZE = 1000
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 2.0   # seconds; doubles each retry → 2, 4, 8

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("recompute_pain")


# ──────────────────────────────────────────────────────────────────────────────
# Retry helper
# ──────────────────────────────────────────────────────────────────────────────

def is_retryable(err: BaseException) -> bool:
    """True if err looks like a transient/retryable failure."""
    s = repr(err)
    return any(p in s for p in RETRYABLE_PATTERNS)


def retry(label: str, fn, *,
          max_attempts: int = DEFAULT_MAX_ATTEMPTS,
          base_delay: float = DEFAULT_BASE_DELAY,
          sleep_fn=time.sleep):
    """
    Run fn(). On retryable errors, sleep with exponential backoff and try
    again. Re-raise on non-retryable errors or after max_attempts exhausted.

    sleep_fn is injectable for testing (defaults to time.sleep).
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if not is_retryable(e):
                raise
            if attempt == max_attempts:
                log.error(f"  [retry EXHAUSTED] {label} after {max_attempts} "
                          f"attempts: {repr(e)[:300]}")
                break
            delay = base_delay * (2 ** (attempt - 1))
            log.warning(f"  [retry {attempt}/{max_attempts}] {label}: "
                        f"{repr(e)[:200]} — sleeping {delay:.1f}s")
            sleep_fn(delay)
    assert last_exc is not None  # for type-checkers; loop guarantees this
    raise last_exc


# ──────────────────────────────────────────────────────────────────────────────
# Checkpoint helpers
# ──────────────────────────────────────────────────────────────────────────────

def save_checkpoint(last_id: str, total: int, failed: int,
                    path: str = CHECKPOINT_PATH) -> None:
    """Atomic write: tmp + rename. Survives Ctrl+C mid-write."""
    payload = {
        "last_id":         last_id,
        "total_processed": total,
        "total_failed":    failed,
        "saved_at":        datetime.now(timezone.utc).isoformat(),
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def load_checkpoint(path: str = CHECKPOINT_PATH) -> Optional[dict]:
    """Returns the saved payload or None if absent / unreadable."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def clear_checkpoint(path: str = CHECKPOINT_PATH) -> None:
    """Delete the checkpoint file. No-op if absent."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# DB layer (lazy-imported so --self-test runs without supabase-py installed)
# ──────────────────────────────────────────────────────────────────────────────

def _get_supabase():
    """Lazily build the Supabase client. Imports + env-var checks happen here
    so --self-test can run with no DB setup at all."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_KEY env vars are required")
        sys.exit(1)
    from supabase import create_client  # type: ignore
    return create_client(url, key)


def fetch_eligible_count(supabase) -> int:
    """Count of leads that match the version filter (one-shot, fast)."""
    res = (
        supabase.table("leads")
        .select("id", count="exact")
        .eq("is_active", True)
        .or_(VERSION_FILTER_OR)
        .limit(1)
        .execute()
    )
    return res.count or 0


def fetch_batch_after(supabase, last_id: Optional[str], limit: int) -> list[dict]:
    """
    Cursor-based fetch. Server-side filters out leads already on v2 so we
    never re-fetch them. Ordered by id (UUID lexicographic) for stable
    pagination — same UUIDs always come in the same order across runs.
    """
    q = (
        supabase.table("leads")
        .select(SELECT_FIELDS)
        .eq("is_active", True)
        .or_(VERSION_FILTER_OR)
        .order("id")
        .limit(limit)
    )
    if last_id is not None:
        q = q.gt("id", last_id)
    return q.execute().data or []


def update_lead_scores(supabase, lead_id: str, scores: dict) -> None:
    """Write recomputed scores back. Single-row update."""
    payload = {
        "pain_score":             scores["pain_score"],
        "pain_score_website":     scores["pain_score_website"],
        "pain_score_reputation":  scores["pain_score_reputation"],
        "pain_score_seo":         scores["pain_score_seo"],
        "pain_score_social":      scores["pain_score_social"],
        "pain_score_bizintel":    scores["pain_score_bizintel"],
        "pain_score_version":     scores["pain_score_version"],
        "pain_score_computed_at": scores["pain_score_computed_at"],
    }
    supabase.table("leads").update(payload).eq("id", lead_id).execute()


# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Backfill pain_score using current formula.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute but do NOT write to DB")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N leads (smoke testing)")
    ap.add_argument("--start-after-id", default=None,
                    help="Cursor: process only leads with id > this UUID. "
                         "Manual override of any checkpoint.")
    ap.add_argument("--restart", action="store_true",
                    help="Ignore + clear any checkpoint; start from beginning.")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                    help=f"Rows per fetch (default {DEFAULT_BATCH_SIZE})")
    ap.add_argument("--checkpoint-path", default=CHECKPOINT_PATH,
                    help="Override checkpoint file location (testing only)")
    ap.add_argument("--self-test", action="store_true",
                    help="Run unit tests for retry + checkpoint helpers, then exit. "
                         "Does NOT touch the DB.")
    return ap.parse_args()


def resolve_starting_cursor(args) -> Optional[str]:
    """Apply the documented priority order to choose the starting last_id."""
    if args.start_after_id is not None:
        log.info(f"using --start-after-id={args.start_after_id} (ignoring any checkpoint)")
        return args.start_after_id

    if args.restart:
        clear_checkpoint(args.checkpoint_path)
        log.warning("--restart: ignoring + clearing any checkpoint, starting from beginning")
        return None

    cp = load_checkpoint(args.checkpoint_path)
    if cp:
        log.info(f"resuming from checkpoint: last_id={cp.get('last_id')}, "
                 f"prev_total={cp.get('total_processed')}, "
                 f"prev_failed={cp.get('total_failed')}, "
                 f"saved_at={cp.get('saved_at')}")
        return cp.get("last_id")

    log.info("no checkpoint found; starting from beginning")
    return None


def run_backfill(args) -> int:
    # Lazy import — only when we actually need DB
    from ingest import compute_pain_score_v1  # type: ignore
    supabase = _get_supabase()

    # Visible-on-startup filter confirmation
    log.info("Filter active: pain_score_version IS NULL OR pain_score_version < 2")

    # Eligible-count query (fast — index lookup with count='exact')
    try:
        eligible = fetch_eligible_count(supabase)
        log.info(f"Eligible leads to process: {eligible:,}")
    except Exception as e:
        log.warning(f"Eligible-count query failed (continuing): {repr(e)[:200]}")

    last_id = resolve_starting_cursor(args)

    t_start = time.time()
    total_processed = 0
    total_failed = 0
    version_counts: dict[int, int] = {}

    while True:
        if args.limit and total_processed >= args.limit:
            log.info(f"reached --limit {args.limit}, stopping")
            break

        batch_size = args.batch_size
        if args.limit:
            batch_size = min(batch_size, args.limit - total_processed)

        # ── Fetch with retry ──
        try:
            cursor_label = f"None" if last_id is None else last_id
            batch = retry(f"fetch_after_{cursor_label}",
                          lambda: fetch_batch_after(supabase, last_id, batch_size))
        except Exception as e:
            log.error(f"FETCH FAILED PERMANENTLY: {repr(e)[:300]}")
            log.error(f"Checkpoint preserved at last_id={last_id}; "
                      f"re-run (without --restart) to resume.")
            return 1

        if not batch:
            log.info("no more leads — backfill complete")
            break

        # ── Per-lead compute + update with retry ──
        for lead in batch:
            lead_id = lead.get("id")
            try:
                scores = compute_pain_score_v1(lead, lead.get("niche_slug") or "other")
                v = scores["pain_score_version"]
                version_counts[v] = version_counts.get(v, 0) + 1
                if not args.dry_run:
                    retry(f"update_id_{lead_id}",
                          lambda lid=lead_id, sc=scores: update_lead_scores(supabase, lid, sc))
                total_processed += 1
            except Exception as e:
                total_failed += 1
                log.warning(f"FAILED lead id={lead_id}: {repr(e)[:200]}")

        # ── Advance cursor + checkpoint after every successful fetch ──
        last_id = batch[-1]["id"]
        if not args.dry_run:
            try:
                save_checkpoint(last_id, total_processed, total_failed,
                                args.checkpoint_path)
            except Exception as e:
                log.warning(f"checkpoint save failed (continuing): {repr(e)[:200]}")

        elapsed = time.time() - t_start
        rate = total_processed / elapsed if elapsed else 0
        log.info(f"last_id={last_id} total={total_processed} "
                 f"failed={total_failed} rate={rate:.1f}/s elapsed={int(elapsed)}s")

    # ── Summary ──
    elapsed = time.time() - t_start
    log.info("=" * 70)
    log.info(f"DONE. processed={total_processed} failed={total_failed} "
             f"elapsed={int(elapsed)}s")
    log.info(f"version distribution: {version_counts}")

    if args.dry_run:
        log.info("(dry-run mode — no writes performed; checkpoint untouched)")
    elif total_failed == 0:
        clear_checkpoint(args.checkpoint_path)
        log.info("checkpoint cleared (clean run)")
    else:
        log.warning(f"checkpoint preserved due to {total_failed} per-lead failures")

    return 0 if total_failed == 0 else 2


# ──────────────────────────────────────────────────────────────────────────────
# Self-tests for retry + checkpoint (no DB needed)
# ──────────────────────────────────────────────────────────────────────────────

def _self_test() -> int:
    fails = 0

    # ── Retry helper tests ──
    print("=== retry helper unit tests ===")

    # Test 1: function returns immediately, no retries
    n_calls = [0]
    def ok_fn():
        n_calls[0] += 1
        return "ok"
    sleeps = []
    result = retry("test_ok", ok_fn, sleep_fn=lambda s: sleeps.append(s))
    if result == "ok" and n_calls[0] == 1 and sleeps == []:
        print("  [OK]    immediate success: no retries, no sleeps")
    else:
        print(f"  [FAIL]  immediate success: result={result} calls={n_calls[0]} sleeps={sleeps}")
        fails += 1

    # Test 2: retryable error twice, then succeeds on attempt 3
    n_calls = [0]
    sleeps = []
    def flaky_fn():
        n_calls[0] += 1
        if n_calls[0] < 3:
            raise Exception("Postgres error: 57014 statement_timeout")
        return "recovered"
    result = retry("test_flaky", flaky_fn, sleep_fn=lambda s: sleeps.append(s))
    if result == "recovered" and n_calls[0] == 3 and sleeps == [2.0, 4.0]:
        print(f"  [OK]    flaky 57014 -> succeeds on attempt 3, sleeps={sleeps}")
    else:
        print(f"  [FAIL]  flaky: result={result} calls={n_calls[0]} sleeps={sleeps}")
        fails += 1

    # Test 3: always-failing retryable -> exhausts and re-raises
    n_calls = [0]
    sleeps = []
    def always_timeout():
        n_calls[0] += 1
        raise Exception("ReadTimeout occurred")
    raised = False
    try:
        retry("test_exhaust", always_timeout, sleep_fn=lambda s: sleeps.append(s))
    except Exception as e:
        raised = "ReadTimeout" in repr(e)
    if raised and n_calls[0] == 4 and sleeps == [2.0, 4.0, 8.0]:
        print(f"  [OK]    exhausts 4 attempts and re-raises, sleeps={sleeps}")
    else:
        print(f"  [FAIL]  exhaust: raised={raised} calls={n_calls[0]} sleeps={sleeps}")
        fails += 1

    # Test 4: non-retryable error re-raises immediately, no sleeps
    n_calls = [0]
    sleeps = []
    def value_err():
        n_calls[0] += 1
        raise KeyError("nope")
    raised = False
    try:
        retry("test_nonretry", value_err, sleep_fn=lambda s: sleeps.append(s))
    except KeyError:
        raised = True
    if raised and n_calls[0] == 1 and sleeps == []:
        print("  [OK]    non-retryable KeyError: immediate re-raise, no sleeps")
    else:
        print(f"  [FAIL]  non-retryable: raised={raised} calls={n_calls[0]} sleeps={sleeps}")
        fails += 1

    # Test 5: is_retryable() classifier sanity
    print()
    print("=== is_retryable() classifier ===")
    cases = [
        (Exception("Postgres error: 57014 statement_timeout"), True,  "57014"),
        (Exception("HTTP 503 Service Unavailable"),            True,  "503"),
        (Exception("HTTP 504 Gateway Timeout"),                True,  "504"),
        (TimeoutError("read timeout"),                         True,  "TimeoutError"),
        (ConnectionError("dropped"),                           True,  "ConnectionError"),
        (KeyError("nope"),                                     False, "non-retryable"),
        (ValueError("bad input"),                              False, "non-retryable"),
        (Exception("HTTP 401 Unauthorized"),                   False, "auth — never retry"),
        (Exception("HTTP 422 Unprocessable Entity"),           False, "client-side bug — never retry"),
    ]
    for err, expected, desc in cases:
        got = is_retryable(err)
        if got == expected:
            print(f"  [OK]    {repr(err)[:60]:60s} -> {got} ({desc})")
        else:
            print(f"  [FAIL]  {repr(err)[:60]:60s} -> {got} expected {expected} ({desc})")
            fails += 1

    # ── Checkpoint helper tests ──
    print()
    print("=== checkpoint helper unit tests ===")
    test_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".test_recompute_pain_checkpoint.json"
    )

    # Cleanup any stale state from prior test runs
    clear_checkpoint(test_path)

    # Test 6: load when file doesn't exist returns None
    if load_checkpoint(test_path) is None:
        print("  [OK]    load_checkpoint() on missing file -> None")
    else:
        print("  [FAIL]  load on missing file should return None")
        fails += 1

    # Test 7: save then load roundtrip
    save_checkpoint("uuid-aaa-001", total=100, failed=5, path=test_path)
    cp = load_checkpoint(test_path)
    if (cp and cp["last_id"] == "uuid-aaa-001"
            and cp["total_processed"] == 100
            and cp["total_failed"] == 5
            and "saved_at" in cp):
        print(f"  [OK]    save/load roundtrip: {cp}")
    else:
        print(f"  [FAIL]  save/load roundtrip got: {cp}")
        fails += 1

    # Test 8: rapid double-save → second value persists, file is valid JSON
    save_checkpoint("uuid-aaa-002", total=200, failed=10, path=test_path)
    save_checkpoint("uuid-aaa-003", total=300, failed=15, path=test_path)
    cp = load_checkpoint(test_path)
    if cp and cp["last_id"] == "uuid-aaa-003" and cp["total_processed"] == 300:
        print(f"  [OK]    rapid double-save: latest value persists ({cp['last_id']})")
    else:
        print(f"  [FAIL]  rapid double-save: cp={cp}")
        fails += 1

    # Test 9: clear removes the file
    clear_checkpoint(test_path)
    if load_checkpoint(test_path) is None and not os.path.exists(test_path):
        print("  [OK]    clear_checkpoint() removes file")
    else:
        print("  [FAIL]  clear_checkpoint() did not remove file")
        fails += 1

    # Test 10: clear is idempotent on missing file (no exception)
    try:
        clear_checkpoint(test_path)
        clear_checkpoint(test_path)
        print("  [OK]    clear_checkpoint() idempotent on missing file")
    except Exception as e:
        print(f"  [FAIL]  clear_checkpoint() raised on missing file: {e}")
        fails += 1

    # Test 11: corrupt JSON file returns None instead of crashing
    with open(test_path, "w") as f:
        f.write("{ this is not valid json }")
    if load_checkpoint(test_path) is None:
        print("  [OK]    load_checkpoint() on corrupt JSON -> None (no crash)")
    else:
        print("  [FAIL]  corrupt JSON should return None")
        fails += 1
    clear_checkpoint(test_path)

    # ── Summary ──
    print()
    if fails == 0:
        print("=== ALL SELF-TESTS PASS ===")
        return 0
    print(f"=== {fails} FAILURE(S) ===")
    return 1


# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()
    if args.self_test:
        return _self_test()
    return run_backfill(args)


if __name__ == "__main__":
    sys.exit(main())
