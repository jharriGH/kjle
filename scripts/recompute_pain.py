#!/usr/bin/env python3
"""
KJLE — One-shot pain_score backfill (v1 -> v2)
File: scripts/recompute_pain.py

Recomputes pain_score for every active lead using the current
compute_pain_score_v1 in scripts/ingest.py. Writes new sub-scores +
pain_score_version + pain_score_computed_at back per row.

Usage:
    # Required env vars (read by Supabase client):
    export SUPABASE_URL=https://...supabase.co
    export SUPABASE_SERVICE_KEY=eyJ...

    # Smoke test on 50 leads, no DB writes:
    python scripts/recompute_pain.py --dry-run --limit 50

    # Real backfill (full table; ~10-30 min for 540K leads):
    python scripts/recompute_pain.py

    # Resume from a specific offset if interrupted:
    python scripts/recompute_pain.py --start-offset 250000

After this completes, trigger the classifier to re-bucket HOT/WARM/COLD:
    POST https://kjle-api.onrender.com/kjle/v1/scheduler/run/classify_segments

This script is one-shot. Future ingests pick up the new formula automatically
because compute_pain_score_v1 is called inline during ingest.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

# Make sibling scripts/ingest.py importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pyright: reportMissingImports=false
from supabase import create_client  # type: ignore

# Reuse the canonical formula. NO duplication of pain logic.
from ingest import compute_pain_score_v1  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("recompute_pain")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    log.error("SUPABASE_URL and SUPABASE_SERVICE_KEY env vars are required")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Fields the formula reads. Don't pull the whole row — it has 100+ columns.
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


def fetch_batch(offset: int, limit: int) -> list[dict]:
    """Pull a page of active leads ordered by id for stable pagination."""
    res = (
        supabase.table("leads")
        .select(SELECT_FIELDS)
        .eq("is_active", True)
        .order("id")
        .range(offset, offset + limit - 1)
        .execute()
    )
    return res.data or []


def update_lead_scores(lead_id: str, scores: dict) -> None:
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill pain_score using current formula.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute but do NOT write to DB")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N leads (testing)")
    ap.add_argument("--start-offset", type=int, default=0,
                    help="Resume at this offset (for restarts)")
    ap.add_argument("--batch-size", type=int, default=1000,
                    help="Rows per fetch (default 1000)")
    args = ap.parse_args()

    t_start = time.time()
    offset = args.start_offset
    total_processed = 0
    total_failed = 0
    version_counts: dict[int, int] = {}

    log.info("Backfill starting (dry_run=%s, limit=%s, start_offset=%d, batch_size=%d)",
             args.dry_run, args.limit, args.start_offset, args.batch_size)

    while True:
        batch_size = args.batch_size
        if args.limit:
            remaining = args.limit - total_processed
            if remaining <= 0:
                break
            batch_size = min(batch_size, remaining)

        try:
            batch = fetch_batch(offset, batch_size)
        except Exception as e:
            log.error("Fetch failed at offset=%d: %s", offset, e)
            break

        if not batch:
            log.info("No more leads at offset=%d. Done.", offset)
            break

        for lead in batch:
            try:
                scores = compute_pain_score_v1(lead, lead.get("niche_slug") or "other")
                v = scores["pain_score_version"]
                version_counts[v] = version_counts.get(v, 0) + 1
                if not args.dry_run:
                    update_lead_scores(lead["id"], scores)
                total_processed += 1
            except Exception as e:
                total_failed += 1
                log.warning("Failed lead id=%s: %s", lead.get("id"), e)

        offset += len(batch)
        elapsed = time.time() - t_start
        rate = total_processed / elapsed if elapsed else 0
        log.info("offset=%d total=%d failed=%d rate=%.1f/s elapsed=%ds",
                 offset, total_processed, total_failed, rate, int(elapsed))

        if args.limit and total_processed >= args.limit:
            break

    elapsed = time.time() - t_start
    log.info("DONE. Processed %d leads (%d failed) in %ds", total_processed, total_failed, int(elapsed))
    log.info("Version distribution: %s", version_counts)
    if args.dry_run:
        log.info("(dry-run — no writes performed)")
    return 0 if total_failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
