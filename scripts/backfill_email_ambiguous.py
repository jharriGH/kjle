#!/usr/bin/env python3
"""
KJLE — Paced backfill: mark leads.email_ambiguous = true for free-mail addresses
attached to more than one distinct business_name among campaign-eligible leads.

Scope: is_active=true AND email_valid=true AND email_status='valid' AND
       domain IN (gmail.com, yahoo.com, aol.com, outlook.com, hotmail.com,
                  icloud.com, live.com, msn.com) AND count(DISTINCT business_name) > 1.

Pacing: UPDATE in 2,000-row chunks with 0.4s sleep between chunks.
Safe to re-run — idempotent (already-true rows are no-ops in the UPDATE).

Usage:
    python scripts/backfill_email_ambiguous.py [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

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
log = logging.getLogger("backfill_email_ambiguous")

FREE_MAIL_DOMAINS = (
    "gmail.com", "yahoo.com", "aol.com", "outlook.com",
    "hotmail.com", "icloud.com", "live.com", "msn.com",
)

CHUNK_SIZE = 2_000
SLEEP_BETWEEN_CHUNKS = 0.4  # seconds

AMBIGUOUS_EMAILS_SQL = """
SELECT lower(email) AS email
FROM leads
WHERE is_active = true
  AND email_valid = true
  AND email_status = 'valid'
  AND lower(split_part(email, '@', 2)) = ANY(%(domains)s)
GROUP BY lower(email)
HAVING count(DISTINCT business_name) > 1
"""

COUNT_SQL = "SELECT count(*) FROM leads WHERE email_ambiguous = true"


def run(dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL not set in environment / .env — abort")
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    try:
        # Step 1: compute ambiguous email set
        log.info("Computing ambiguous free-mail email set …")
        with conn.cursor() as cur:
            cur.execute(AMBIGUOUS_EMAILS_SQL, {"domains": list(FREE_MAIL_DOMAINS)})
            ambiguous_emails = [row[0] for row in cur.fetchall()]

        log.info("Ambiguous email addresses found: %s", f"{len(ambiguous_emails):,}")
        if not ambiguous_emails:
            log.info("Nothing to update — done.")
            return

        # Step 2: fetch IDs of matching leads (email_ambiguous not already true)
        log.info("Fetching lead IDs to update …")
        placeholders = ",".join(["%s"] * len(ambiguous_emails))
        id_sql = f"""
            SELECT id
            FROM leads
            WHERE lower(email) IN ({placeholders})
              AND email_ambiguous IS DISTINCT FROM true
            ORDER BY id
        """
        with conn.cursor() as cur:
            cur.execute(id_sql, ambiguous_emails)
            all_ids = [row[0] for row in cur.fetchall()]

        log.info("Lead rows to mark email_ambiguous=true: %s", f"{len(all_ids):,}")
        if not all_ids:
            log.info("All matching leads already flagged — done.")
            return

        if dry_run:
            log.info("[DRY-RUN] Would UPDATE %s rows — exiting without writing.", f"{len(all_ids):,}")
            return

        # Step 3: paced UPDATE in chunks
        total_written = 0
        n_chunks = (len(all_ids) + CHUNK_SIZE - 1) // CHUNK_SIZE
        for i in range(0, len(all_ids), CHUNK_SIZE):
            chunk = all_ids[i : i + CHUNK_SIZE]
            chunk_num = i // CHUNK_SIZE + 1
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE leads SET email_ambiguous = true WHERE id = ANY(%s::uuid[])",
                    (chunk,),
                )
                written = cur.rowcount
            conn.commit()
            total_written += written
            log.info(
                "[chunk %d/%d] wrote %d rows (running total: %s)",
                chunk_num, n_chunks, written, f"{total_written:,}",
            )
            if i + CHUNK_SIZE < len(all_ids):
                time.sleep(SLEEP_BETWEEN_CHUNKS)

        # Step 4: report final count
        with conn.cursor() as cur:
            cur.execute(COUNT_SQL)
            final_count = cur.fetchone()[0]
        log.info("Backfill complete. UPDATE-written this run: %s", f"{total_written:,}")
        log.info("SELECT count(*) FROM leads WHERE email_ambiguous = true → %s", f"{final_count:,}")

    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill email_ambiguous flag on leads.")
    parser.add_argument("--dry-run", action="store_true", help="Compute scope only, no writes.")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
