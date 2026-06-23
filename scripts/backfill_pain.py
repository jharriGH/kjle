"""
KJLE — Pain score backfill for ~942k default-50 leads
scripts/backfill_pain.py

Targets leads WHERE pain_score = 50 (the CSV-import sentinel).
Recomputes pain using compute_pain_score_v1 (read-only import from ingest.py)
and writes back pain_score ONLY — no other columns are touched.

Run order:
  1. Faithfulness pre-flight: recompute 10 existing non-50 leads and compare
     stored vs formula output.  If any diverge > FAITH_WARN points, abort.
  2. Cursor loop over pain_score=50 leads in id-order chunks of 1000.
  3. Upsert pain_score in sub-batches of UPDATE_BATCH rows (one POST each).

Usage:
    # Dry-run first — no writes:
    python scripts/backfill_pain.py --dry-run --limit 500

    # Live run (approve after inspecting dry-run distribution):
    python scripts/backfill_pain.py --limit 50000

    # Full backfill (~942k rows):
    python scripts/backfill_pain.py

Env (required):
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from typing import Optional

# Make sibling scripts/ingest.py importable when running from any directory.
# Intentionally at module level so it's set before any lazy imports inside run().
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Constants ─────────────────────────────────────────────────────────────────

CHUNK_SIZE   = 1000   # leads fetched per cursor step
UPDATE_BATCH = 200    # rows per upsert call (keeps statements small)
FAITH_SAMPLE = 10     # reference leads for faithfulness pre-flight
FAITH_WARN   = 5.0    # absolute pain-score divergence treated as material

# Only the columns compute_pain_score_v1 reads, plus id/niche_slug/pain_score.
# Keeping the projection narrow avoids pulling 100+ columns per row.
SELECT_FIELDS = ", ".join([
    "id", "niche_slug", "pain_score",
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backfill_pain")


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_supabase():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_KEY env vars are required")
        sys.exit(1)
    from supabase import create_client  # type: ignore
    return create_client(url, key)


def _count_eligible(db) -> int:
    res = db.table("leads").select("id", count="exact").eq("pain_score", 50).execute()
    return res.count or 0


def _fetch_chunk(db, cursor: Optional[str], limit: int) -> list[dict]:
    """SELECT up to `limit` pain_score=50 leads with id > cursor, ordered by id."""
    q = (
        db.table("leads")
        .select(SELECT_FIELDS)
        .eq("pain_score", 50)
        .order("id", desc=False)
        .limit(limit)
    )
    if cursor is not None:
        q = q.gt("id", cursor)
    return q.execute().data or []


def _bulk_upsert_pain(db, scores: dict) -> None:
    """
    Write {lead_id: new_pain_score} back in UPDATE_BATCH-sized upsert calls.
    Only pain_score is included in the payload — no other columns are touched.
    """
    items = list(scores.items())
    for i in range(0, len(items), UPDATE_BATCH):
        batch = items[i : i + UPDATE_BATCH]
        db.table("leads").upsert(
            [{"id": lid, "pain_score": pain} for lid, pain in batch],
            on_conflict="id",
        ).execute()


# ── Faithfulness pre-flight ───────────────────────────────────────────────────

def _faithfulness_check(db, compute_fn) -> bool:
    """
    Fetch FAITH_SAMPLE leads that have real stored scores (pain_score != 50
    and NOT NULL), recompute via the formula, and compare.

    Returns True if all divergences are within FAITH_WARN points.
    Returns False (and logs the details) if any diverge materially — the
    caller should abort to avoid writing wrong scores to the DB.
    """
    log.info("── Faithfulness pre-flight ─────────────────────────────────────")
    rows = (
        db.table("leads")
        .select(SELECT_FIELDS)
        .neq("pain_score", 50)
        .not_.is_("pain_score", "null")
        .order("id")
        .limit(FAITH_SAMPLE)
        .execute()
        .data or []
    )

    if not rows:
        log.warning("No reference leads found (pain_score != 50 and not null) — skipping check.")
        return True

    material = False
    for row in rows:
        stored = row.get("pain_score")
        try:
            result = compute_fn(row, row.get("niche_slug") or "")
            recomputed = result["pain_score"]
        except Exception as exc:
            log.error(f"  compute_pain_score_v1 raised for id={row['id']}: {exc}")
            material = True
            continue

        diff = abs((stored or 0) - recomputed)
        flag = "  ⚠ DIVERGED" if diff > FAITH_WARN else ""
        log.info(
            f"  id={row['id']}  "
            f"stored={stored}  recomputed={recomputed}  diff={diff:.1f}{flag}"
        )
        if diff > FAITH_WARN:
            material = True

    if material:
        log.error(
            f"Faithfulness check FAILED: ≥1 lead diverged by >{FAITH_WARN} pts. "
            "Inspect formula or stored data before proceeding."
        )
    else:
        log.info("Faithfulness check PASSED — formula output matches stored scores.")

    return not material


# ── Main backfill loop ────────────────────────────────────────────────────────

def run(args) -> None:
    # Lazy import: ingest.py has heavy module-level side effects (pandas, supabase
    # create_client, sys.exit on missing creds).  Import only after we've set
    # sys.path and only when we're actually about to run.
    from ingest import compute_pain_score_v1  # type: ignore

    db = _get_supabase()

    # ── Faithfulness gate ─────────────────────────────────────────────────────
    if not args.skip_faithfulness:
        if not _faithfulness_check(db, compute_pain_score_v1):
            sys.exit(1)

    # ── Count and announce ────────────────────────────────────────────────────
    eligible = _count_eligible(db)
    cap = min(args.limit, eligible) if args.limit else eligible
    mode = "DRY-RUN" if args.dry_run else "LIVE"
    log.info(
        f"Target: {eligible:,} leads with pain_score=50  "
        f"(processing up to {cap:,})  mode={mode}  "
        f"chunk={CHUNK_SIZE}  upsert_batch={UPDATE_BATCH}"
    )

    # ── Cursor loop ───────────────────────────────────────────────────────────
    cursor: Optional[str] = None
    total_processed = 0
    total_written = 0
    total_skipped = 0
    dist: Counter = Counter()   # new pain-score distribution (10-point buckets)

    while True:
        remaining = cap - total_processed
        if remaining <= 0:
            break

        chunk = _fetch_chunk(db, cursor, min(CHUNK_SIZE, remaining))
        if not chunk:
            break

        # Recompute pain for every row in the chunk
        scores: dict = {}
        for row in chunk:
            try:
                result = compute_pain_score_v1(row, row.get("niche_slug") or "")
                scores[row["id"]] = result["pain_score"]
            except Exception as exc:
                log.warning(
                    f"compute_pain_score_v1 failed for id={row['id']}: {exc} — row skipped"
                )
                total_skipped += 1

        # Accumulate distribution (for dry-run report and live progress)
        for pain in scores.values():
            dist[(int(pain) // 10) * 10] += 1

        if not args.dry_run:
            try:
                _bulk_upsert_pain(db, scores)
                total_written += len(scores)
            except Exception as exc:
                log.error(f"_bulk_upsert_pain failed at cursor={cursor}: {exc}")
                raise

        total_processed += len(chunk)
        cursor = chunk[-1]["id"]

        if total_processed % 10_000 == 0 or total_processed == cap:
            log.info(
                f"  progress: {total_processed:,} recomputed "
                f"/ {cap:,}  written={total_written:,}"
            )

    # ── Final report ──────────────────────────────────────────────────────────
    log.info("── Results ─────────────────────────────────────────────────────")
    log.info(f"  Mode:            {mode}")
    log.info(f"  Processed:       {total_processed:,}")
    log.info(f"  Written:         {total_written:,}")
    log.info(f"  Skipped (error): {total_skipped:,}")
    log.info("  Recomputed pain_score distribution:")

    max_count = max(dist.values()) if dist else 1
    for bucket in sorted(dist):
        bar = "█" * max(1, dist[bucket] * 40 // max_count)
        log.info(
            f"    {bucket:3d}–{bucket + 9:3d}: {dist[bucket]:6,}  {bar}"
        )

    if dist:
        total_scored = sum(dist.values())
        weighted_sum = sum(b * c for b, c in dist.items())
        approx_mean = round(weighted_sum / total_scored + 5, 1)  # bucket midpoints
        log.info(f"  Approximate mean pain (bucket midpoints): {approx_mean}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Backfill pain_score for default-50 sentinel leads (pain_score ONLY)."
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Recompute and report distribution but do not write to the DB.",
    )
    p.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="Max leads to process (default: 0 = all eligible).",
    )
    p.add_argument(
        "--skip-faithfulness", action="store_true",
        help="Skip the pre-flight faithfulness check (not recommended).",
    )
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
