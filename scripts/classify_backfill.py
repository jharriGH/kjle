#!/usr/bin/env python3
"""
KJLE — Paced contactability + segment classification backfill
File: scripts/classify_backfill.py

One-time backfill that applies the existing canonical classifiers to the
~935k active leads that have never been through the backlog scrub
(filter_classified_at IS NULL).

Per-row operations (ALL local, zero external/paid API calls):
  1. Pain score: if pain_score_computed_at IS NULL, compute via
     compute_pain_score_v1 (scripts/ingest.py) — reads only lead-row columns.
     Writes: pain_score, pain_score_version, pain_score_computed_at, sub-scores.
  2. Segment: _classify_lead logic (pain_score → hot/warm/cold).
     Writes: segment_label, segmented_at.
  3. Contactability: classify_lead_quality + classify_phone_quality
     (api/lib/lead_filters.py + api/lib/phone_filters.py).
     Writes: contactable, filter_reasons, filter_classified_at, filter_classified_by.

Scope: is_active = true AND filter_classified_at IS NULL
Skip: rows already stamped — idempotent, safe to re-run (resumes from checkpoint).

Pacing (protects the live REK feed / BizReply DB):
  SELECT 10,000 rows at a time (cursor-paginated by UUID).
  UPDATE in 500-row sub-chunks via psycopg2 execute_values.
  Sleep 0.5s between sub-chunks.

Hard guard: MAX_ITER = 300 outer chunks (3M row cap — well above 935k).

Usage:
    # Dry-run first (no writes):
    python scripts/classify_backfill.py --dry-run

    # Full run:
    python scripts/classify_backfill.py

    # Resume (checkpoint auto-detects; explicit override):
    python scripts/classify_backfill.py --start-after-id "uuid-..."

    # Fresh start (ignore checkpoint):
    python scripts/classify_backfill.py --restart
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

# ── Path setup — make sibling scripts and api/lib importable ─────────────────
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_scripts_dir)
for _p in [_scripts_dir, _repo_root]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv  # type: ignore
load_dotenv(os.path.join(_repo_root, ".env"), override=False)

# ── Import existing canonical classifiers — DO NOT DUPLICATE LOGIC ────────────
from ingest import compute_pain_score_v1  # type: ignore  # scripts/ingest.py
from api.lib.lead_filters import classify_lead_quality    # type: ignore
from api.lib.phone_filters import classify_phone_quality  # type: ignore

import psycopg2  # type: ignore
import psycopg2.extras  # type: ignore

# ── Constants ─────────────────────────────────────────────────────────────────

LABEL = "classify_backfill_2026_10_27"   # filter_classified_by stamp
SELECT_CHUNK = 10_000                     # rows fetched per outer loop
UPDATE_SUB_CHUNK = 500                    # rows per execute_values call
SLEEP_BETWEEN_SUB_CHUNKS = 0.5           # seconds
MAX_ITER = 300                            # hard guard: 300 * 10k = 3M rows max

CHECKPOINT_PATH = os.path.join(_scripts_dir, ".classify_backfill_checkpoint.json")

# Columns the pain formula reads (mirrors recompute_pain.py SELECT_FIELDS)
SELECT_COLS = ", ".join([
    "id",
    "pain_score", "pain_score_computed_at", "niche_slug",
    # contactability classifier (category aliased to match classify_lead_quality's dict key)
    "business_name", "description", "category AS categories", "phone",
    # pain — reputation
    "google_stars", "google_review_count", "g_maps_claimed",
    # pain — seo
    "seo_schema_present", "mobile_friendly",
    "google_analytics", "google_pixel", "google_rank",
    # pain — social
    "facebook_url", "instagram_url",
    "ads_facebook", "ads_adwords", "facebook_stars",
    # pain — website
    "website", "uses_wordpress", "uses_shopify", "domain_expired",
    "is_parked", "website_has_ssl", "website_reachable", "website_status_code",
    # pain — bizintel
    "domain_expiring_soon", "email_state",
])

# ── SQL templates ─────────────────────────────────────────────────────────────

SELECT_SQL_START = f"""
    SELECT {SELECT_COLS}
    FROM leads
    WHERE is_active = true AND filter_classified_at IS NULL
    ORDER BY id
    LIMIT %s
"""

SELECT_SQL_AFTER = f"""
    SELECT {SELECT_COLS}
    FROM leads
    WHERE is_active = true AND filter_classified_at IS NULL AND id > %s
    ORDER BY id
    LIMIT %s
"""

MAIN_UPDATE_SQL = """
UPDATE leads SET
    segment_label        = v.seg,
    segmented_at         = v.seg_at::timestamptz,
    contactable          = v.cont::bool,
    filter_reasons       = v.reasons::jsonb,
    filter_classified_at = v.cls_at::timestamptz,
    filter_classified_by = v.label
FROM (VALUES %s) AS v(id, seg, seg_at, cont, reasons, cls_at, label)
WHERE leads.id = v.id::uuid
"""

PAIN_UPDATE_SQL = """
UPDATE leads SET
    pain_score             = v.score::float,
    pain_score_version     = v.ver::int,
    pain_score_computed_at = v.computed_at::timestamptz,
    pain_score_website     = v.ps_web::float,
    pain_score_reputation  = v.ps_rep::float,
    pain_score_seo         = v.ps_seo::float,
    pain_score_social      = v.ps_soc::float,
    pain_score_bizintel    = v.ps_biz::float
FROM (VALUES %s) AS v(id, score, ver, computed_at, ps_web, ps_rep, ps_seo, ps_soc, ps_biz)
WHERE leads.id = v.id::uuid
"""

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("classify_backfill")


# ── Canonical segment classifier (mirrors _classify_lead exactly) ─────────────

def classify_segment(pain_score) -> str:
    """HOT >= 30 | WARM 15-29 | COLD < 15  (v2 thresholds, 2026-05-09)."""
    pain = pain_score or 0
    if pain >= 30:
        return "hot"
    if pain >= 15:
        return "warm"
    return "cold"


# ── Checkpoint helpers ────────────────────────────────────────────────────────

def save_checkpoint(last_id: str, total: int, pain_total: int) -> None:
    payload = {
        "last_id":    last_id,
        "total":      total,
        "pain_total": pain_total,
        "saved_at":   datetime.now(timezone.utc).isoformat(),
    }
    tmp = CHECKPOINT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, CHECKPOINT_PATH)


def load_checkpoint() -> Optional[dict]:
    try:
        with open(CHECKPOINT_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def clear_checkpoint() -> None:
    try:
        os.remove(CHECKPOINT_PATH)
    except FileNotFoundError:
        pass


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        log.error("DATABASE_URL env var is required (load .env first)")
        sys.exit(1)
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    # Auto-parse jsonb columns (categories, filter_reasons, etc.)
    psycopg2.extras.register_default_jsonb(conn, globally=False)
    conn.autocommit = False
    return conn


def count_remaining(conn) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM leads WHERE is_active = true AND filter_classified_at IS NULL"
        )
        remaining = cur.fetchone()["count"]
        cur.execute(
            "SELECT COUNT(*) FROM leads WHERE is_active = true AND filter_classified_at IS NOT NULL"
        )
        done = cur.fetchone()["count"]
    return remaining, done


def segment_distribution(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT segment_label, COUNT(*) AS cnt
            FROM leads
            WHERE is_active = true AND filter_classified_at = %s
            GROUP BY segment_label
        """, (LABEL,))
        return {r["segment_label"]: r["cnt"] for r in cur.fetchall()}


# ── Sub-chunk writer ──────────────────────────────────────────────────────────

def flush_sub_chunk(conn, main_rows: list, pain_rows: list, dry_run: bool) -> int:
    """Write one sub-chunk. Returns number of main rows written."""
    if dry_run or not main_rows:
        return len(main_rows)
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, MAIN_UPDATE_SQL, main_rows)
        if pain_rows:
            psycopg2.extras.execute_values(cur, PAIN_UPDATE_SQL, pain_rows)
    conn.commit()
    return len(main_rows)


# ── Main backfill loop ────────────────────────────────────────────────────────

def run_backfill(dry_run: bool, start_after_id: Optional[str] = None) -> None:
    t_start = time.monotonic()
    now_iso = datetime.now(timezone.utc).isoformat()

    conn = get_conn()

    # Before counts
    remaining_before, done_before = count_remaining(conn)
    log.info(f"BEFORE: filter_classified_at IS NOT NULL = {done_before:,}")
    log.info(f"BEFORE: filter_classified_at IS NULL     = {remaining_before:,}  ← backfill target")

    # Checkpoint / resume
    ckpt = load_checkpoint()
    last_id: Optional[str] = start_after_id
    total_classified = 0
    total_pain_scored = 0

    if last_id is not None:
        log.info(f"Starting after id={last_id} (explicit override)")
    elif ckpt:
        last_id = ckpt.get("last_id")
        total_classified = ckpt.get("total", 0)
        total_pain_scored = ckpt.get("pain_total", 0)
        log.info(f"Resuming from checkpoint: last_id={last_id} "
                 f"(already done: {total_classified:,})")
    else:
        log.info("Starting from beginning (no checkpoint found)")

    if dry_run:
        log.info("DRY-RUN mode — no writes to DB")

    iter_count = 0

    while iter_count < MAX_ITER:
        iter_count += 1

        # ── Fetch next chunk ─────────────────────────────────────────────────
        with conn.cursor() as cur:
            if last_id is None:
                cur.execute(SELECT_SQL_START, (SELECT_CHUNK,))
            else:
                cur.execute(SELECT_SQL_AFTER, (last_id, SELECT_CHUNK))
            rows = cur.fetchall()

        if not rows:
            log.info("No more rows — backfill complete.")
            break

        last_id = str(rows[-1]["id"])

        # ── Classify all rows in this chunk ──────────────────────────────────
        main_batch: list = []   # (id, seg, seg_at, cont, reasons, cls_at, label)
        pain_batch: list = []   # (id, score, ver, computed_at, sub-scores...)

        for row in rows:
            row_id = str(row["id"])
            try:
                # Step 1: Pain score (local, no external API)
                if row["pain_score_computed_at"] is None:
                    scores = compute_pain_score_v1(
                        dict(row), row.get("niche_slug") or "other"
                    )
                    effective_pain = scores["pain_score"]
                    pain_batch.append((
                        row_id,
                        scores["pain_score"],
                        scores["pain_score_version"],
                        scores["pain_score_computed_at"],
                        scores["pain_score_website"],
                        scores["pain_score_reputation"],
                        scores["pain_score_seo"],
                        scores["pain_score_social"],
                        scores["pain_score_bizintel"],
                    ))
                else:
                    effective_pain = row.get("pain_score") or 0

                # Step 2: Segment classification (mirrors _classify_lead exactly)
                seg = classify_segment(effective_pain)

                # Step 3: Contactability (local, no external API)
                lead_q = classify_lead_quality(dict(row))
                phone_q = (
                    classify_phone_quality(row.get("phone"))
                    if row.get("phone") else None
                )
                contactable = lead_q.get("contactable", True) and (
                    phone_q.get("contactable", True) if phone_q else True
                )
                reasons = (
                    list(lead_q.get("reasons") or []) +
                    (list(phone_q.get("reasons") or []) if phone_q else [])
                )

                main_batch.append((
                    row_id,
                    seg,
                    now_iso,
                    contactable,
                    json.dumps(reasons),
                    now_iso,
                    LABEL,
                ))

            except Exception as e:
                log.warning(f"Row {row_id} classification failed, skipping: {e}")
                continue

        # ── Write in 500-row sub-chunks ──────────────────────────────────────
        chunk_written = 0
        for i in range(0, len(main_batch), UPDATE_SUB_CHUNK):
            m_slice = main_batch[i : i + UPDATE_SUB_CHUNK]
            # Pain rows only for this sub-chunk's ids
            m_ids = {r[0] for r in m_slice}
            p_slice = [r for r in pain_batch if r[0] in m_ids]

            written = flush_sub_chunk(conn, m_slice, p_slice, dry_run)
            chunk_written += written
            total_classified += written
            total_pain_scored += len(p_slice)

            time.sleep(SLEEP_BETWEEN_SUB_CHUNKS)

        # ── Progress log + checkpoint ────────────────────────────────────────
        elapsed = time.monotonic() - t_start
        log.info(
            f"[chunk {iter_count}] classified={chunk_written:,} | "
            f"total={total_classified:,} | pain_scored={total_pain_scored:,} | "
            f"elapsed={elapsed:.0f}s | last_id={last_id[:8]}..."
        )

        if not dry_run:
            save_checkpoint(last_id, total_classified, total_pain_scored)

    else:
        log.warning(f"Hit MAX_ITER={MAX_ITER} guard — stopping to prevent runaway loop.")

    # ── After counts ─────────────────────────────────────────────────────────
    remaining_after, done_after = count_remaining(conn)
    dist = segment_distribution(conn)
    elapsed_total = time.monotonic() - t_start

    log.info("=" * 60)
    log.info("BACKFILL COMPLETE")
    log.info(f"  Mode:         {'DRY-RUN (no writes)' if dry_run else 'LIVE'}")
    log.info(f"  Runtime:      {elapsed_total:.0f}s ({elapsed_total/60:.1f} min)")
    log.info(f"  Classified:   {total_classified:,} rows")
    log.info(f"  Pain scored:  {total_pain_scored:,} rows (local formula only)")
    log.info(f"  BEFORE: NOT NULL={done_before:,} | NULL={remaining_before:,}")
    log.info(f"  AFTER:  NOT NULL={done_after:,} | NULL={remaining_after:,}")
    log.info(f"  Segment distribution (this backfill's rows, label={LABEL!r}):")
    for label in ("hot", "warm", "cold"):
        log.info(f"    {label}: {dist.get(label, 0):,}")
    log.info(f"  External/paid API calls: NONE (pure local logic)")
    log.info("=" * 60)

    if not dry_run and remaining_after == 0:
        clear_checkpoint()
        log.info("Checkpoint cleared — backfill fully complete.")
    elif not dry_run:
        log.info(f"{remaining_after:,} rows still unclassified — re-run to continue.")

    conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Classify backfill for KJLE leads")
    parser.add_argument("--dry-run", action="store_true",
                        help="Read + classify without writing to DB")
    parser.add_argument("--start-after-id", default=None, metavar="UUID",
                        help="Resume from a specific UUID (ignores checkpoint)")
    parser.add_argument("--restart", action="store_true",
                        help="Clear checkpoint and start from the beginning")
    args = parser.parse_args()

    if args.restart:
        clear_checkpoint()
        log.info("Checkpoint cleared — restarting from beginning.")

    start_id = args.start_after_id if not args.restart else None
    run_backfill(dry_run=args.dry_run, start_after_id=start_id)


if __name__ == "__main__":
    main()
