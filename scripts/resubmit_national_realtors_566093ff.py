"""
resubmit_national_realtors_566093ff.py

One-off: clear the 20,000 contacts stuck in failed TrueList batch
566093ff-9379-4341-ad92-6be4d249a3ed and re-submit them as a fresh batch.

Background: batch 566093ff hit a TrueList 503 on 2026-10-16 and never completed
(status='unknown', no annotated_csv_url). TrueList has no retrievable result for
that batch_id; contacts remain mislabeled catch_all/pending_batch. This script
resets those rows and re-submits so the fixed poll->ingest cron can assign real verdicts.

Steps executed:
  1. Fetch the exact 20,000 contact ids + emails from the failed batch.
  2. Clear their stale verdict fields (NULL them out so they look un-cleaned).
  3. POST the same emails to TrueList as a new batch.
  4. Record the new batch in truelist_batches.
  5. Stamp contacts with the new batch_id (email_status='pending_batch').
  6. Mark old batch as 'superseded' with a note.
"""

import csv
import io
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv("/opt/kjle/.env")

DATABASE_URL = os.environ["DATABASE_URL"]
TRUELIST_BATCHES_URL = "https://api.truelist.io/api/v1/batches"

BAD_BATCH_ID = "566093ff-9379-4341-ad92-6be4d249a3ed"
SUBMITTED_BY = "clean_national_realtors"
STAMP_CHUNK = 200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── email helpers (identical to clean_national_realtors.py) ──────────────────

_ANNOTATION_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _extract_emails(raw: str) -> list:
    if not raw:
        return []
    results = []
    for part in re.split(r"\s*,\s*", raw.strip()):
        clean = _ANNOTATION_RE.sub("", part).strip()
        if clean and "@" in clean and " " not in clean and len(clean) <= 254:
            results.append(clean.lower())
    return results


# ── db ────────────────────────────────────────────────────────────────────────

def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def load_api_key(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM admin_settings WHERE key = 'truelist_api_key'")
        row = cur.fetchone()
    if not row or not (row[0] or "").strip():
        log.error("TrueList API key not found in admin_settings")
        sys.exit(1)
    return row[0].strip()


# ── TrueList submit (identical to clean_national_realtors.py) ────────────────

def submit_to_truelist(contacts: list, api_key: str, batch_name: str) -> tuple:
    seen: set = set()
    csv_emails: list = []
    for _contact_id, primary_email in contacts:
        for em in _extract_emails(primary_email or ""):
            if em not in seen:
                seen.add(em)
                csv_emails.append(em)

    if not csv_emails:
        raise ValueError("No extractable emails from selected contacts")

    log.info(f"  Submitting: {len(csv_emails)} unique emails from {len(contacts)} contacts")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["email"])
    for em in csv_emails:
        writer.writerow([em])
    csv_bytes = buf.getvalue().encode("utf-8")

    with httpx.Client(timeout=60.0) as client:
        r = client.post(
            TRUELIST_BATCHES_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("emails.csv", csv_bytes, "text/csv")},
            data={"name": batch_name},
        )

    log.info(f"  TrueList HTTP {r.status_code}: {r.text[:300]}")

    if r.status_code == 409:
        try:
            existing_id = (r.json().get("existing_batch") or {}).get("id")
        except Exception:
            existing_id = None
        if existing_id:
            log.warning(f"  409 duplicate -- adopting existing batch {existing_id}")
            return existing_id, len(csv_emails)
        raise RuntimeError(f"TrueList 409 without existing_batch id: {r.text[:300]}")

    if r.status_code not in (200, 201):
        raise RuntimeError(f"TrueList submit HTTP {r.status_code}: {r.text[:300]}")

    body = r.json()
    batch_id = body.get("id")
    if not batch_id:
        raise RuntimeError(f"TrueList response missing id: {body}")

    log.info(
        f"  Batch {batch_id} created "
        f"(state={body.get('batch_state')}, email_count={body.get('email_count')})"
    )
    return batch_id, len(csv_emails)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== resubmit_national_realtors_566093ff.py START ===")
    conn = get_conn()
    api_key = load_api_key(conn)

    # ── Step 1: capture the exact set up front ──
    log.info(f"  Fetching contacts for failed batch {BAD_BATCH_ID}")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, primary_email
            FROM contacts
            WHERE email_truelist_batch_id = %s::uuid
            ORDER BY id
            """,
            (BAD_BATCH_ID,),
        )
        contacts = cur.fetchall()

    if not contacts:
        log.error("  0 contacts found for this batch_id -- nothing to do")
        conn.close()
        sys.exit(1)

    contact_ids = [r[0] for r in contacts]
    log.info(f"  Captured {len(contacts)} contacts")

    # ── Step 2: clear stale verdict fields on exactly those rows ──
    log.info("  Clearing stale verdict fields ...")
    cleared = 0
    for i in range(0, len(contact_ids), STAMP_CHUNK):
        chunk = contact_ids[i : i + STAMP_CHUNK]
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE contacts
                SET email_trust              = NULL,
                    email_valid              = NULL,
                    email_status             = NULL,
                    email_truelist_batch_id  = NULL,
                    email_cleaned_at         = NULL
                WHERE id = ANY(%s::uuid[])
                """,
                (chunk,),
            )
            cleared += cur.rowcount
    log.info(f"  Cleared {cleared} contacts (expected {len(contacts)})")
    if cleared != len(contacts):
        log.error(f"  MISMATCH: cleared {cleared} but expected {len(contacts)} -- aborting")
        conn.close()
        sys.exit(1)

    # ── Step 3: submit fresh TrueList batch ──
    batch_name = f"national realtors RESUBMIT of 566093ff -- {len(contacts)} contacts"
    log.info(f"  Submitting to TrueList: name='{batch_name}'")
    try:
        new_batch_id, email_count = submit_to_truelist(contacts, api_key, batch_name)
    except Exception as e:
        log.error(f"  TrueList submit FAILED: {e}")
        log.error("  Contacts have been cleared but NOT re-stamped. Re-run this script to retry.")
        conn.close()
        sys.exit(1)

    # ── Step 4: record new batch in truelist_batches ──
    log.info(f"  Recording new batch {new_batch_id} in truelist_batches ...")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO truelist_batches
                (id, email_count, status, submitted_at, submitted_by, notes)
            VALUES (%s::uuid, %s, 'pending', NOW(), %s, %s)
            ON CONFLICT (id) DO UPDATE
                SET notes = truelist_batches.notes || ' | re-submitted'
            """,
            (
                new_batch_id,
                email_count,
                SUBMITTED_BY,
                (
                    f"national realtors RESUBMIT of {BAD_BATCH_ID} on 2026-10-17 "
                    f"-- {len(contacts)} contacts, {email_count} unique emails"
                ),
            ),
        )
    log.info(f"  New batch row recorded: id={new_batch_id}")

    # ── Step 5: stamp contacts with new batch_id ──
    log.info(f"  Stamping {len(contact_ids)} contacts with new batch_id={new_batch_id}")
    stamped = 0
    for i in range(0, len(contact_ids), STAMP_CHUNK):
        chunk = contact_ids[i : i + STAMP_CHUNK]
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE contacts
                SET email_truelist_batch_id = %s::uuid,
                    email_status            = 'pending_batch'
                WHERE id = ANY(%s::uuid[])
                """,
                (new_batch_id, chunk),
            )
            stamped += cur.rowcount
    log.info(f"  Stamped {stamped} contacts")

    # ── Step 6: mark old batch superseded ──
    log.info(f"  Marking old batch {BAD_BATCH_ID} as superseded ...")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE truelist_batches
            SET status = 'superseded',
                notes  = notes || %s
            WHERE id = %s::uuid
            """,
            (
                f" | resubmitted as {new_batch_id} on 2026-10-17",
                BAD_BATCH_ID,
            ),
        )
    log.info(f"  Old batch marked superseded")

    conn.close()

    print("\n========== RESUBMIT COMPLETE ==========")
    print(f"  Old batch (superseded):  {BAD_BATCH_ID}")
    print(f"  New batch id:            {new_batch_id}")
    print(f"  Contacts cleared+stamped: {stamped}")
    print(f"  Unique emails submitted:  {email_count}")
    print(f"  New batch status:         pending -> poll cron will ingest verdicts")
    print("=========================================")
    log.info("=== resubmit_national_realtors_566093ff.py DONE ===")


if __name__ == "__main__":
    main()
