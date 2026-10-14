"""
clean_national_realtors.py — KJLE recurring drain
Email-clean the national realtor contact backlog via TrueList.

Target (~425k contacts):
  contacts WHERE niche_slug = 'realestate'
    AND title ~* '(realtor|real estate agent|listing agent|sales agent|
                   associate broker|broker associate|real estate broker|sales associate)'
    AND email_cleaned_at IS NULL

Design:
  - Each run: drain any completed in-flight batches first (poll -> ingest).
  - Then, if inflight < MAX_INFLIGHT, submit the next chunk of BATCH_SIZE contacts.
  - Contacts are stamped (email_truelist_batch_id, email_status='pending_batch') on
    submit so subsequent runs skip them; ingest is keyed by batch_id, not email string.
  - contacts.email_truelist_batch_id UUID column is auto-created at startup if absent.
  - Cron: every 3 hours.  20k/batch x ~5-6h TrueList processing -> ~4 ingests/day
    -> ~80k contacts/day -> ~425k / 80k ≈ 5-6 days to drain the full backlog.
  - TrueList UNLIMITED plan cap: ~86k validations/day.

Usage:
  python3 scripts/clean_national_realtors.py              # normal cron run
  python3 scripts/clean_national_realtors.py --dry-run    # count only, no submit
  python3 scripts/clean_national_realtors.py --submit-only  # submit + stamp, skip poll
  python3 scripts/clean_national_realtors.py --ingest BATCH_ID  # force-ingest one batch
"""

import argparse
import csv
import io
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv("/opt/kjle/.env")

DATABASE_URL = os.environ["DATABASE_URL"]
TRUELIST_BATCHES_URL = "https://api.truelist.io/api/v1/batches"
LOG_FILE = "/tmp/clean_national_realtors.log"

SUBMITTED_BY = "clean_national_realtors"
BATCH_SIZE = 20_000
MAX_INFLIGHT = 1      # one batch at a time; TrueList UNLIMITED but ~86k/day cap
STAMP_CHUNK = 200     # contacts stamped per UPDATE (stays under Supabase 8KB URL limit)
INGEST_CHUNK = 200    # contacts updated per UPDATE during ingest
POLL_TIMEOUT = 70 * 60   # 70 min; large batches take 5-6h, so cron drain handles rest
POLL_INTERVAL = 60

TARGET_TITLE_PATTERN = (
    r"(realtor|real estate agent|listing agent|sales agent|associate broker"
    r"|broker associate|real estate broker|sales associate)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── email helpers (same as enrichment_email_clean.py / clean_socar_realtors.py) ─

_ANNOTATION_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _extract_emails(raw: str) -> list:
    """Strip annotations (e.g. '(accept_all)') and return clean lowercase emails."""
    if not raw:
        return []
    results = []
    for part in re.split(r"\s*,\s*", raw.strip()):
        clean = _ANNOTATION_RE.sub("", part).strip()
        if clean and "@" in clean and " " not in clean and len(clean) <= 254:
            results.append(clean.lower())
    return results


def _parse_truelist_state(raw_state: Optional[str]) -> tuple:
    """Map TrueList Email State -> (email_status, email_valid)."""
    if not raw_state:
        return ("unknown", None)
    s = str(raw_state).strip().lower()
    if s.startswith("email_"):
        s = s[len("email_"):]
    if s == "ok":
        return ("valid", True)
    if s == "invalid":
        return ("invalid", False)
    return ("unknown", None)


def _parse_annotated_csv(csv_text: str) -> dict:
    """Parse TrueList annotated CSV -> {email_lower: {'state': ...}}."""
    out = {}
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return out
    header = [h.strip() for h in rows[0]]
    try:
        em_idx = header.index("Email Address")
    except ValueError:
        em_idx = 1
    try:
        st_idx = header.index("Email State")
    except ValueError:
        st_idx = 3
    for r in rows[1:]:
        if not r or len(r) <= max(em_idx, st_idx):
            continue
        em = (r[em_idx] or "").strip().lower()
        if em:
            out[em] = {"state": (r[st_idx] or "").strip()}
    return out


# ── db connection ─────────────────────────────────────────────────────────────

def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


# ── schema auto-migration ─────────────────────────────────────────────────────

def ensure_contacts_batch_column(conn):
    """
    Add email_truelist_batch_id UUID to contacts (and its index) if not present.
    Safe to run on every startup — ADD COLUMN IF NOT EXISTS is idempotent.
    """
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS email_truelist_batch_id UUID
        """)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_contacts_truelist_batch_id
            ON contacts (email_truelist_batch_id)
            WHERE email_truelist_batch_id IS NOT NULL
        """)
    log.info("  contacts.email_truelist_batch_id column + index ensured")


# ── admin settings ────────────────────────────────────────────────────────────

def load_api_key(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM admin_settings WHERE key = 'truelist_api_key'")
        row = cur.fetchone()
    if not row or not (row[0] or "").strip():
        log.error("TrueList API key not found in admin_settings")
        sys.exit(1)
    return row[0].strip()


# ── target queries ────────────────────────────────────────────────────────────

def query_remaining(conn) -> int:
    """Count contacts still needing cleaning (email_cleaned_at IS NULL)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM contacts
            WHERE niche_slug = 'realestate'
              AND title ~* %s
              AND email_cleaned_at IS NULL
              AND primary_email IS NOT NULL
            """,
            (TARGET_TITLE_PATTERN,),
        )
        return cur.fetchone()[0]


def query_inflight_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM truelist_batches
            WHERE submitted_by = %s
              AND status IN ('pending', 'processing', 'finalizing')
            """,
            (SUBMITTED_BY,),
        )
        return cur.fetchone()[0]


def select_batch(conn) -> list:
    """
    Select up to BATCH_SIZE contacts to submit next.
    Excludes contacts already stamped as pending_batch so we never double-submit.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, primary_email
            FROM contacts
            WHERE niche_slug = 'realestate'
              AND title ~* %s
              AND email_cleaned_at IS NULL
              AND email_status IS DISTINCT FROM 'pending_batch'
              AND primary_email IS NOT NULL
            ORDER BY id
            LIMIT %s
            """,
            (TARGET_TITLE_PATTERN, BATCH_SIZE),
        )
        return cur.fetchall()


# ── db write helpers ──────────────────────────────────────────────────────────

def stamp_contacts(conn, contact_ids: list, batch_id: str):
    """
    Mark submitted contacts so the next cron run won't re-select them.
    Sets email_truelist_batch_id and email_status='pending_batch'.
    """
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
                (batch_id, chunk),
            )
    log.info(f"  Stamped {len(contact_ids)} contacts with batch_id={batch_id}")


def record_batch(conn, batch_id: str, email_count: int, contact_count: int):
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
                batch_id,
                email_count,
                SUBMITTED_BY,
                f"national realtors -- {contact_count} contacts, {email_count} unique emails",
            ),
        )


def update_batch_status(
    conn,
    batch_id: str,
    status: str,
    csv_url: str = None,
    counts: dict = None,
):
    notes_append = ""
    if counts:
        notes_append = (
            f" | DONE: valid={counts.get('valid', 0)},invalid={counts.get('invalid', 0)},"
            f"unknown={counts.get('unknown', 0)},no_match={counts.get('no_match', 0)}"
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE truelist_batches
            SET status            = %s,
                annotated_csv_url = COALESCE(annotated_csv_url, %s),
                completed_at      = CASE
                    WHEN %s IN ('completed', 'ingested')
                    THEN COALESCE(completed_at, NOW())
                    ELSE completed_at
                END,
                ingested_at       = CASE
                    WHEN %s = 'ingested' THEN NOW() ELSE ingested_at
                END,
                notes             = notes || %s
            WHERE id = %s::uuid
            """,
            (status, csv_url, status, status, notes_append, batch_id),
        )


# ── TrueList API ──────────────────────────────────────────────────────────────

def submit_to_truelist(contacts: list, api_key: str, batch_name: str) -> tuple:
    """
    Deduplicate emails via _extract_emails, build CSV, POST to TrueList.
    Returns (batch_id: str, unique_email_count: int).
    """
    seen: set = set()
    csv_emails: list = []
    for _contact_id, primary_email in contacts:
        for em in _extract_emails(primary_email or ""):
            if em not in seen:
                seen.add(em)
                csv_emails.append(em)

    if not csv_emails:
        raise ValueError("No extractable emails from selected contacts")

    log.info(
        f"  Submitting: {len(csv_emails)} unique emails from {len(contacts)} contacts"
    )

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


def poll_once(batch_id: str, api_key: str) -> tuple:
    """Single TrueList GET. Returns (state: str, csv_url: str|None)."""
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(
                f"{TRUELIST_BATCHES_URL}/{batch_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if r.status_code == 429:
            log.warning(f"  [{batch_id[:8]}] rate-limited -- extra 120s back-off")
            time.sleep(120)
            return "unknown", None
        if r.status_code != 200:
            log.warning(f"  [{batch_id[:8]}] poll HTTP {r.status_code} -- retrying next cycle")
            return "unknown", None
        raw = r.json()
        return raw.get("batch_state", "unknown"), raw.get("annotated_csv_url")
    except Exception as e:
        log.warning(f"  [{batch_id[:8]}] poll error: {e}")
        return "unknown", None


def download_csv(csv_url: str, api_key: str) -> dict:
    """Download TrueList annotated CSV -> email_map dict."""
    with httpx.Client(timeout=300.0, follow_redirects=True) as client:
        cr = client.get(csv_url, headers={"Authorization": f"Bearer {api_key}"})
    if cr.status_code != 200:
        raise RuntimeError(f"CSV download HTTP {cr.status_code}")
    em = _parse_annotated_csv(cr.text)
    log.info(f"  CSV downloaded ({len(cr.text):,} chars) -- {len(em)} email results")
    return em


# ── ingest ────────────────────────────────────────────────────────────────────

def ingest_verdicts_for_batch(conn, batch_id: str, email_map: dict) -> dict:
    """
    Load contacts stamped with batch_id, apply TrueList verdicts.
    Best-verdict logic: valid > unknown > invalid.
    No-match contacts (email not in CSV) get email_status='unknown' + email_cleaned_at.
    Returns counts dict.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, primary_email
            FROM contacts
            WHERE email_truelist_batch_id = %s::uuid
              AND email_cleaned_at IS NULL
            """,
            (batch_id,),
        )
        contacts = cur.fetchall()

    if not contacts:
        log.warning(f"  [{batch_id[:8]}] ingest: 0 contacts found for this batch_id")
        return {"valid": 0, "invalid": 0, "unknown": 0, "no_match": 0}

    log.info(f"  [{batch_id[:8]}] ingest: {len(contacts)} contacts to process")

    _VERDICT_ORDER = {"valid": 0, "unknown": 1, "invalid": 2}
    verdict_to_ids: dict = {}
    no_match_ids: list = []

    for contact_id, primary_email in contacts:
        clean_emails = _extract_emails(primary_email or "")
        if not clean_emails:
            no_match_ids.append(contact_id)
            continue
        best = None
        for em in clean_emails:
            entry = email_map.get(em)
            if entry is None:
                continue
            sv = _parse_truelist_state(entry["state"])
            if best is None or _VERDICT_ORDER.get(sv[0], 99) < _VERDICT_ORDER.get(best[0], 99):
                best = sv
        if best is None:
            no_match_ids.append(contact_id)
        else:
            verdict_to_ids.setdefault(best, []).append(contact_id)

    now_ts = datetime.now(timezone.utc).isoformat()
    counts = {"valid": 0, "invalid": 0, "unknown": 0, "no_match": len(no_match_ids)}

    for (status, valid), ids in verdict_to_ids.items():
        for i in range(0, len(ids), INGEST_CHUNK):
            chunk = ids[i : i + INGEST_CHUNK]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE contacts
                    SET email_status     = %s,
                        email_valid      = %s,
                        email_cleaned_at = %s,
                        email_trust      = CASE
                            WHEN %s = 'valid'   THEN 'valid'
                            WHEN %s = 'invalid' THEN 'unconfirmable'
                            ELSE email_trust
                        END
                    WHERE id = ANY(%s::uuid[])
                    """,
                    (status, valid, now_ts, status, status, chunk),
                )
                counts[status] = counts.get(status, 0) + cur.rowcount

    if no_match_ids:
        log.info(
            f"  [{batch_id[:8]}] {len(no_match_ids)} no-CSV-match contacts "
            f"-- marking email_status='unknown'"
        )
        for i in range(0, len(no_match_ids), INGEST_CHUNK):
            chunk = no_match_ids[i : i + INGEST_CHUNK]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE contacts
                    SET email_status     = 'unknown',
                        email_cleaned_at = %s
                    WHERE id = ANY(%s::uuid[])
                    """,
                    (now_ts, chunk),
                )

    total = sum(counts.values())
    log.info(
        f"  [{batch_id[:8]}] ingest complete: {total} contacts updated -- {counts}"
    )
    return counts


# ── drain pass ────────────────────────────────────────────────────────────────

def drain_completed_batches(conn, api_key: str) -> int:
    """
    Poll every pending/processing batch from this job.
    Ingest those that have completed.  Returns total contacts updated.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text FROM truelist_batches
            WHERE submitted_by = %s
              AND status IN ('pending', 'processing', 'finalizing')
            ORDER BY submitted_at
            """,
            (SUBMITTED_BY,),
        )
        pending = [r[0] for r in cur.fetchall()]

    if not pending:
        log.info("  Drain: no in-flight batches")
        return 0

    log.info(f"  Drain: checking {len(pending)} in-flight batch(es)")
    total_updated = 0

    for batch_id in pending:
        state, csv_url = poll_once(batch_id, api_key)
        log.info(f"  [{batch_id[:8]}] drain poll -> state={state}")
        update_batch_status(conn, batch_id, state, csv_url)

        if state != "completed":
            continue

        try:
            email_map = download_csv(csv_url, api_key)
            counts = ingest_verdicts_for_batch(conn, batch_id, email_map)
            update_batch_status(conn, batch_id, "ingested", csv_url, counts)
            total_updated += sum(counts.values())
            log.info(f"  [{batch_id[:8]}] drain ingested: {counts}")
        except Exception as e:
            log.error(f"  [{batch_id[:8]}] drain ingest error: {e}")

    return total_updated


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Recurring drain: email-clean national realtor contacts via TrueList"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Count remaining contacts only; no submit")
    parser.add_argument("--submit-only", action="store_true",
                        help="Submit + stamp next batch; skip blocking poll (let cron drain)")
    parser.add_argument("--ingest", metavar="BATCH_ID",
                        help="Force-poll and ingest a specific batch by ID")
    args = parser.parse_args()

    log.info("=== clean_national_realtors.py START ===")
    conn = get_conn()
    ensure_contacts_batch_column(conn)
    api_key = load_api_key(conn)

    remaining = query_remaining(conn)
    log.info(f"  Remaining (email_cleaned_at IS NULL): {remaining:,}")

    # ── DRY RUN ──
    if args.dry_run:
        print(f"DRY RUN: {remaining:,} national realtor contacts need cleaning")
        conn.close()
        return

    # ── FORCE INGEST ──
    if args.ingest:
        batch_id = args.ingest
        log.info(f"  --ingest: force-polling batch {batch_id}")
        state, csv_url = poll_once(batch_id, api_key)
        log.info(f"  Batch {batch_id} state={state} csv_url={csv_url}")
        if state != "completed" or not csv_url:
            log.error(f"  Batch not completed (state={state}) -- cannot ingest yet")
            conn.close()
            sys.exit(1)
        email_map = download_csv(csv_url, api_key)
        counts = ingest_verdicts_for_batch(conn, batch_id, email_map)
        update_batch_status(conn, batch_id, "ingested", csv_url, counts)
        conn.close()
        print(f"Force-ingested batch {batch_id}: {counts}")
        return

    # ── DRAIN PASS (every normal run) ──
    log.info("=== DRAIN PASS ===")
    drain_updated = drain_completed_batches(conn, api_key)
    log.info(f"  Drain pass: {drain_updated} contacts updated")

    # Recount after drain (some may now be cleaned)
    remaining = query_remaining(conn)
    inflight = query_inflight_count(conn)
    log.info(f"  After drain: remaining={remaining:,}, inflight={inflight}")

    if remaining == 0:
        log.info("  BACKLOG CLEAR -- all national realtor contacts are cleaned!")
        conn.close()
        print("DONE: backlog is 0. National realtor drain complete.")
        return

    if inflight >= MAX_INFLIGHT:
        log.info(
            f"  Inflight cap ({inflight}/{MAX_INFLIGHT}) -- skipping submit this run"
        )
        conn.close()
        print(
            f"INFLIGHT CAP: {inflight}/{MAX_INFLIGHT} batch(es) in flight. "
            f"Remaining: {remaining:,}. Will drain next run."
        )
        return

    # ── SUBMIT PASS ──
    log.info(f"=== SUBMIT: selecting up to {BATCH_SIZE:,} contacts ===")
    contacts = select_batch(conn)
    if not contacts:
        log.info("  Nothing selected (all may be pending_batch or cleaned)")
        conn.close()
        print(f"NOTHING TO SUBMIT. Remaining: {remaining:,} (all may be stamped)")
        return

    batch_name = (
        f"kjle_natl_realtors_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    )

    try:
        batch_id, email_count = submit_to_truelist(contacts, api_key, batch_name)
    except Exception as e:
        log.error(f"  Submit failed: {e}")
        conn.close()
        sys.exit(1)

    contact_ids = [r[0] for r in contacts]
    stamp_contacts(conn, contact_ids, batch_id)
    record_batch(conn, batch_id, email_count, len(contacts))

    log.info(
        f"  SUBMITTED: batch_id={batch_id}, contacts={len(contacts):,}, "
        f"emails={email_count:,}"
    )

    if args.submit_only:
        remaining_after = query_remaining(conn)
        conn.close()
        print(
            f"SUBMITTED: batch_id={batch_id}\n"
            f"  contacts_submitted={len(contacts):,}\n"
            f"  unique_emails={email_count:,}\n"
            f"  remaining_after_stamp={remaining_after:,}\n"
            f"  Cron will drain (poll + ingest) on next run."
        )
        log.info("=== clean_national_realtors.py DONE (submit-only) ===")
        return

    # ── POLL PASS (blocking, up to POLL_TIMEOUT) ──
    log.info(f"=== POLL: {batch_id} (up to {POLL_TIMEOUT // 60}min) ===")
    elapsed = 0
    state = "pending"
    csv_url = None

    while state != "completed" and elapsed < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        state, csv_url = poll_once(batch_id, api_key)
        update_batch_status(conn, batch_id, state, csv_url)
        log.info(f"  Poll at {elapsed}s: state={state}")

    if state == "completed" and csv_url:
        log.info("  Batch completed within poll window -- ingesting now")
        email_map = download_csv(csv_url, api_key)
        counts = ingest_verdicts_for_batch(conn, batch_id, email_map)
        update_batch_status(conn, batch_id, "ingested", csv_url, counts)
        remaining_after = query_remaining(conn)
        conn.close()
        print(
            f"BATCH COMPLETE AND INGESTED:\n"
            f"  batch_id={batch_id}\n"
            f"  submitted={len(contacts):,}\n"
            f"  unique_emails={email_count:,}\n"
            f"  ingested={counts}\n"
            f"  remaining={remaining_after:,}"
        )
    else:
        remaining_after = query_remaining(conn)
        conn.close()
        log.info(
            f"  Poll window ended ({elapsed}s, state={state}) -- "
            f"batch in-flight; next cron run will drain it."
        )
        print(
            f"SUBMITTED (poll timed out -- cron will ingest on next run):\n"
            f"  batch_id={batch_id}\n"
            f"  contacts_submitted={len(contacts):,}\n"
            f"  unique_emails={email_count:,}\n"
            f"  remaining_after_stamp={remaining_after:,}\n"
            f"  batch_state={state}"
        )

    log.info("=== clean_national_realtors.py DONE ===")


if __name__ == "__main__":
    main()
