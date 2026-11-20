#!/usr/bin/env python3
"""
KJLE — Paced backfill for public.contacts:
  Phase A: mark email_ambiguous=true for free-mail addresses attached to
           more than one distinct company.
  Phase B: mark email_ambiguous=true for obviously-wrong domains:
           foreign 2-letter country-code TLDs (anything except .us) or .edu.
  Phase C: set county from ca_socal_city_county map (CA contacts only).

Run label: contacts_email_ambiguous_2026_11_05
Checkpoint: /opt/kjle/scripts/.contacts_email_ambiguous_checkpoint.json

Usage:
    python scripts/backfill_contacts_email_ambiguous.py [--dry-run] [--skip-county]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_scripts_dir)
for _p in [_repo_root, _scripts_dir]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv  # type: ignore
load_dotenv(os.path.join(_repo_root, ".env"), override=False)

import psycopg2  # type: ignore
import psycopg2.extras  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill_contacts_email_ambiguous")

RUN_LABEL = "contacts_email_ambiguous_2026_11_05"
CHECKPOINT_PATH = Path(_scripts_dir) / ".contacts_email_ambiguous_checkpoint.json"

# Mirrors the leads free-domain list + web.de / gmx.com (realtor-context additions)
FREE_MAIL_DOMAINS = (
    "gmail.com", "yahoo.com", "aol.com", "outlook.com",
    "hotmail.com", "icloud.com", "live.com", "msn.com",
    "web.de", "gmx.com",
)

CHUNK_SIZE = 2_000
SLEEP_BETWEEN_CHUNKS = 0.4  # seconds

# ── Phase A SQL ───────────────────────────────────────────────────────────────
# Emails that are free-mail AND appear attached to > 1 distinct company.
_PHASE_A_AMBIGUOUS_SQL = """
SELECT lower(primary_email) AS email
FROM contacts
WHERE primary_email IS NOT NULL
  AND lower(split_part(primary_email, '@', 2)) = ANY(%(domains)s)
  AND company IS NOT NULL
GROUP BY lower(primary_email)
HAVING count(DISTINCT lower(company)) > 1
"""

# ── Phase B SQL ───────────────────────────────────────────────────────────────
# Emails whose domain TLD is a foreign 2-letter country code (not .us) or .edu.
# regexp_replace strips everything up through the last dot → leaves the TLD.
_PHASE_B_IDS_SQL = """
SELECT id
FROM contacts
WHERE primary_email IS NOT NULL
  AND primary_email LIKE '%%@%%.%%'
  AND (
        (length(regexp_replace(lower(split_part(primary_email, '@', 2)),
                               '^.*\\.', '')) = 2
         AND regexp_replace(lower(split_part(primary_email, '@', 2)),
                            '^.*\\.', '') <> 'us')
        OR regexp_replace(lower(split_part(primary_email, '@', 2)),
                          '^.*\\.', '') = 'edu'
      )
  AND email_ambiguous IS DISTINCT FROM true
ORDER BY id
"""

# ── Phase C SQL ───────────────────────────────────────────────────────────────
_PHASE_C_FETCH_SQL = """
SELECT id, lower(city) AS city_lc
FROM contacts
WHERE lower(state) = 'ca'
  AND city IS NOT NULL
  AND county IS NULL
ORDER BY id
"""


def _load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        try:
            data = json.loads(CHECKPOINT_PATH.read_text())
            if data.get("run_label") == RUN_LABEL:
                return data
        except Exception:
            pass
    return {
        "run_label": RUN_LABEL,
        "phase_a_complete": False,
        "phase_b_complete": False,
        "phase_c_complete": False,
        "rows_flagged_a": 0,
        "rows_flagged_b": 0,
        "rows_county_set": 0,
    }


def _save_checkpoint(cp: dict) -> None:
    CHECKPOINT_PATH.write_text(json.dumps(cp, indent=2))


def _update_in_chunks(conn, ids: list, label: str) -> int:
    """UPDATE contacts SET email_ambiguous=true for each id chunk. Returns rows written."""
    total_written = 0
    n_chunks = (len(ids) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for i in range(0, len(ids), CHUNK_SIZE):
        chunk = ids[i: i + CHUNK_SIZE]
        chunk_num = i // CHUNK_SIZE + 1
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE contacts SET email_ambiguous = true WHERE id = ANY(%s::uuid[])",
                (chunk,),
            )
            written = cur.rowcount
        conn.commit()
        total_written += written
        log.info(
            "[%s chunk %d/%d] wrote %d rows (running total: %s)",
            label, chunk_num, n_chunks, written, f"{total_written:,}",
        )
        if i + CHUNK_SIZE < len(ids):
            time.sleep(SLEEP_BETWEEN_CHUNKS)
    return total_written


def phase_a(conn, dry_run: bool) -> int:
    """Free-mail-on-multiple: flag contacts whose free-mail email maps to >1 company."""
    log.info("=== Phase A: free-mail-on-multiple ===")
    log.info("Computing ambiguous free-mail email set …")
    with conn.cursor() as cur:
        cur.execute(_PHASE_A_AMBIGUOUS_SQL, {"domains": list(FREE_MAIL_DOMAINS)})
        ambiguous_emails = [row[0] for row in cur.fetchall()]

    log.info("Ambiguous free-mail addresses found: %s", f"{len(ambiguous_emails):,}")
    if not ambiguous_emails:
        log.info("Phase A: nothing to flag.")
        return 0

    placeholders = ",".join(["%s"] * len(ambiguous_emails))
    id_sql = f"""
        SELECT id
        FROM contacts
        WHERE lower(primary_email) IN ({placeholders})
          AND email_ambiguous IS DISTINCT FROM true
        ORDER BY id
    """
    with conn.cursor() as cur:
        cur.execute(id_sql, ambiguous_emails)
        all_ids = [row[0] for row in cur.fetchall()]

    log.info("Phase A: contact rows to mark email_ambiguous=true: %s", f"{len(all_ids):,}")
    if not all_ids:
        log.info("Phase A: all matching contacts already flagged.")
        return 0

    if dry_run:
        log.info("[DRY-RUN] Phase A: would UPDATE %s rows.", f"{len(all_ids):,}")
        return 0

    return _update_in_chunks(conn, all_ids, "A")


def phase_b(conn, dry_run: bool) -> int:
    """Foreign TLD / .edu: flag contacts with obviously-wrong domains for US realtors."""
    log.info("=== Phase B: foreign TLD / .edu ===")
    with conn.cursor() as cur:
        cur.execute(_PHASE_B_IDS_SQL)
        all_ids = [row[0] for row in cur.fetchall()]

    log.info("Phase B: contact rows to flag (foreign TLD or .edu): %s", f"{len(all_ids):,}")
    if not all_ids:
        log.info("Phase B: nothing to flag.")
        return 0

    if dry_run:
        log.info("[DRY-RUN] Phase B: would UPDATE %s rows.", f"{len(all_ids):,}")
        return 0

    return _update_in_chunks(conn, all_ids, "B")


def phase_c(conn, dry_run: bool) -> int:
    """County backfill: set contacts.county from ca_socal_city_county map (CA only)."""
    log.info("=== Phase C: county backfill (CA contacts) ===")

    from api.data.ca_socal_city_county import CITY_TO_COUNTY

    with conn.cursor() as cur:
        cur.execute(_PHASE_C_FETCH_SQL)
        rows = cur.fetchall()  # (id, city_lc)

    log.info("Phase C: CA contacts with NULL county: %s", f"{len(rows):,}")
    if not rows:
        log.info("Phase C: nothing to update.")
        return 0

    # Group ids by county
    county_to_ids: dict = {}
    skipped = 0
    for row_id, city_lc in rows:
        county = CITY_TO_COUNTY.get(city_lc.strip()) if city_lc else None
        if county:
            county_to_ids.setdefault(county, []).append(row_id)
        else:
            skipped += 1

    mapped = sum(len(v) for v in county_to_ids.values())
    log.info("Phase C: mappable=%s  unmapped(city not in dict)=%s", f"{mapped:,}", f"{skipped:,}")

    if not mapped:
        log.info("Phase C: no cities matched the map.")
        return 0

    if dry_run:
        log.info("[DRY-RUN] Phase C: would UPDATE %s rows across %d counties.",
                 f"{mapped:,}", len(county_to_ids))
        return 0

    total_written = 0
    for county, ids in sorted(county_to_ids.items()):
        n_chunks = (len(ids) + CHUNK_SIZE - 1) // CHUNK_SIZE
        log.info("Phase C [%s]: %s contacts, %d chunk(s)", county, f"{len(ids):,}", n_chunks)
        for i in range(0, len(ids), CHUNK_SIZE):
            chunk = ids[i: i + CHUNK_SIZE]
            chunk_num = i // CHUNK_SIZE + 1
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE contacts SET county = %s WHERE id = ANY(%s::uuid[])",
                    (county, chunk),
                )
                written = cur.rowcount
            conn.commit()
            total_written += written
            log.info(
                "  [%s chunk %d/%d] wrote %d rows (running total: %s)",
                county, chunk_num, n_chunks, written, f"{total_written:,}",
            )
            if i + CHUNK_SIZE < len(ids):
                time.sleep(SLEEP_BETWEEN_CHUNKS)

    return total_written


def run(dry_run: bool, skip_county: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL not set in environment / .env — abort")
        sys.exit(1)

    cp = _load_checkpoint()
    log.info("Checkpoint loaded: phase_a=%s  phase_b=%s  phase_c=%s",
             cp["phase_a_complete"], cp["phase_b_complete"], cp["phase_c_complete"])

    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    try:
        # ── Phase A ──────────────────────────────────────────────────────────
        if cp["phase_a_complete"]:
            log.info("Phase A already complete (checkpoint) — skipping.")
        else:
            flagged_a = phase_a(conn, dry_run)
            cp["rows_flagged_a"] += flagged_a
            if not dry_run:
                cp["phase_a_complete"] = True
                _save_checkpoint(cp)
            log.info("Phase A done. Flagged this run: %s", f"{flagged_a:,}")

        # ── Phase B ──────────────────────────────────────────────────────────
        if cp["phase_b_complete"]:
            log.info("Phase B already complete (checkpoint) — skipping.")
        else:
            flagged_b = phase_b(conn, dry_run)
            cp["rows_flagged_b"] += flagged_b
            if not dry_run:
                cp["phase_b_complete"] = True
                _save_checkpoint(cp)
            log.info("Phase B done. Flagged this run: %s", f"{flagged_b:,}")

        # ── Phase C ──────────────────────────────────────────────────────────
        if skip_county:
            log.info("Phase C skipped (--skip-county).")
        elif cp["phase_c_complete"]:
            log.info("Phase C already complete (checkpoint) — skipping.")
        else:
            county_set = phase_c(conn, dry_run)
            cp["rows_county_set"] += county_set
            if not dry_run:
                cp["phase_c_complete"] = True
                _save_checkpoint(cp)
            log.info("Phase C done. County rows set this run: %s", f"{county_set:,}")

        # ── Final summary ─────────────────────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM contacts WHERE email_ambiguous = true")
            total_ambiguous = cur.fetchone()[0]

        log.info("─" * 60)
        log.info("BACKFILL COMPLETE")
        log.info("  Rows flagged Phase A (this run): %s", f"{cp['rows_flagged_a']:,}")
        log.info("  Rows flagged Phase B (this run): %s", f"{cp['rows_flagged_b']:,}")
        log.info("  County rows set      (this run): %s", f"{cp['rows_county_set']:,}")
        log.info("  contacts.email_ambiguous=true total: %s", f"{total_ambiguous:,}")

    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill email_ambiguous + county on contacts table."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute scope only, no writes.")
    parser.add_argument("--skip-county", action="store_true",
                        help="Skip Phase C (county backfill).")
    args = parser.parse_args()
    run(dry_run=args.dry_run, skip_county=args.skip_county)
