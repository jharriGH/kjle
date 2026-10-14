"""
clean_socar_realtors.py — KJLE one-off
Email-clean the SoCal realtor CONTACTS for the REK pilot via TrueList.

Target:
  contacts WHERE niche_slug='realestate'
    AND title ~* '(realtor|real estate agent|listing agent|...)'
    AND state='CA'
    AND city ILIKE ANY (...24 SoCal cities...)
    AND email_cleaned_at IS NULL

Uses the correct TrueList CSV multipart/form-data submit (fixed 2026-10-11).
Matches verdicts back to contacts by contact_id (not raw email equality).
Uses _extract_emails to strip annotations before submit and during ingest.

Usage:
  python3 scripts/clean_socar_realtors.py              # full run
  python3 scripts/clean_socar_realtors.py --dry-run    # count only, no submit
  python3 scripts/clean_socar_realtors.py --poll-only BATCH_ID
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
BATCH_ID_FILE = "/tmp/clean_socar_realtors_batch_id.txt"
LOG_FILE = "/tmp/clean_socar_realtors.log"
REPORT_FILE = "/tmp/clean_socar_realtors_report.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── same annotation-stripping helpers as enrichment_email_clean.py ────────────

_ANNOTATION_RE = re.compile(r'\s*\([^)]*\)\s*$')


def _extract_emails(raw: str) -> list:
    """
    Extract clean email addresses from a raw field that may contain annotations
    or comma-separated multiple values (e.g. "foo@bar.com (accept_all)").
    Returns list of lowercased clean addresses.
    """
    if not raw:
        return []
    results = []
    for part in re.split(r'\s*,\s*', raw.strip()):
        clean = _ANNOTATION_RE.sub('', part).strip()
        if clean and '@' in clean and ' ' not in clean and len(clean) <= 254:
            results.append(clean.lower())
    return results


def _parse_truelist_state(raw_state: Optional[str]) -> tuple:
    """Map TrueList Email State → (email_status, email_valid)."""
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
    """Parse TrueList annotated CSV → {email_lower: {state, sub_state}}."""
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
    try:
        sub_idx = header.index("Email Sub-State")
    except ValueError:
        sub_idx = 4
    for r in rows[1:]:
        if not r or len(r) <= max(em_idx, st_idx):
            continue
        em = (r[em_idx] or "").strip().lower()
        if em:
            out[em] = {
                "state":     (r[st_idx] or "").strip(),
                "sub_state": (r[sub_idx] or "").strip() if sub_idx < len(r) else "",
            }
    return out


# ── target set definition ─────────────────────────────────────────────────────

TARGET_TITLE_PATTERN = (
    r'(realtor|real estate agent|listing agent|sales agent|associate broker'
    r'|broker associate|real estate broker|sales associate)'
)

SOCAR_CITY_PATTERNS = [
    '%long beach%', '%los angeles%', '%anaheim%', '%irvine%', '%santa ana%',
    '%torrance%', '%huntington beach%', '%lakewood%', '%cerritos%', '%carson%',
    '%seal beach%', '%garden grove%', '%fullerton%', '%costa mesa%', '%newport beach%',
    '%downey%', '%whittier%', '%bellflower%', '%norwalk%', '%cypress%',
    '%westminster%', '%fountain valley%', '%san pedro%', '%orange%',
]


def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def _city_clause():
    return " OR ".join(["city ILIKE %s"] * len(SOCAR_CITY_PATTERNS))


def query_target_contacts(conn) -> list:
    """Fetch all SoCal realtor contacts with email_cleaned_at IS NULL."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id::text, primary_email
            FROM contacts
            WHERE niche_slug = %s
              AND title ~* %s
              AND state = %s
              AND ({_city_clause()})
              AND email_cleaned_at IS NULL
              AND primary_email IS NOT NULL
            ORDER BY id
            """,
            ['realestate', TARGET_TITLE_PATTERN, 'CA'] + SOCAR_CITY_PATTERNS,
        )
        rows = cur.fetchall()
    log.info(f"Queried {len(rows)} SoCal realtor contacts with email_cleaned_at IS NULL")
    return rows


def query_totals(conn) -> dict:
    """Count the full SoCal realtor target set (all rows, including already cleaned)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                COUNT(*)                                        AS total,
                COUNT(*) FILTER (WHERE email_cleaned_at IS NULL)  AS uncleaned,
                COUNT(*) FILTER (WHERE email_valid = true)         AS email_valid_count,
                COUNT(*) FILTER (WHERE email_status = 'valid')     AS status_valid,
                COUNT(*) FILTER (WHERE email_status = 'invalid')   AS status_invalid,
                COUNT(*) FILTER (WHERE email_status = 'unknown')   AS status_unknown
            FROM contacts
            WHERE niche_slug = %s
              AND title ~* %s
              AND state = %s
              AND ({_city_clause()})
            """,
            ['realestate', TARGET_TITLE_PATTERN, 'CA'] + SOCAR_CITY_PATTERNS,
        )
        row = cur.fetchone()
    return {
        "total":             row[0],
        "uncleaned":         row[1],
        "email_valid_count": row[2],
        "status_valid":      row[3],
        "status_invalid":    row[4],
        "status_unknown":    row[5],
    }


# ── submit ────────────────────────────────────────────────────────────────────

def submit_to_truelist(contacts: list, api_key: str, batch_name: str) -> tuple:
    """
    Build a CSV of unique extracted emails and POST to TrueList as
    multipart/form-data (CSV file upload — required since 2026-10-11).
    Returns (batch_id, unique_email_count).
    """
    seen_emails: set = set()
    csv_emails: list = []

    for contact_id, primary_email in contacts:
        extracted = _extract_emails(primary_email or "")
        if not extracted:
            log.warning(f"  contact {contact_id}: no extractable email from {primary_email!r:.80s}")
            continue
        for em in extracted:
            if em not in seen_emails:
                seen_emails.add(em)
                csv_emails.append(em)

    if not csv_emails:
        raise ValueError("No extractable emails from target set")

    log.info(f"  Building CSV: {len(csv_emails)} unique emails from {len(contacts)} contacts")

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["email"])
    for em in csv_emails:
        writer.writerow([em])
    csv_bytes = csv_buf.getvalue().encode("utf-8")

    log.info(f"  POSTing CSV to TrueList (name={batch_name!r})...")
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
            log.warning(f"  409 duplicate — adopting existing batch {existing_id}")
            return existing_id, len(csv_emails)
        raise RuntimeError(f"TrueList 409 without existing_batch id: {r.text[:300]}")

    if r.status_code not in (200, 201):
        raise RuntimeError(f"TrueList submit HTTP {r.status_code}: {r.text[:300]}")

    body = r.json()
    batch_id = body.get("id")
    if not batch_id:
        raise RuntimeError(f"TrueList response missing id: {body}")

    log.info(
        f"  Batch {batch_id} submitted "
        f"(state={body.get('batch_state')}, email_count={body.get('email_count')})"
    )
    return batch_id, len(csv_emails)


# ── poll ──────────────────────────────────────────────────────────────────────

def poll_until_complete(batch_id: str, api_key: str) -> str:
    """Poll TrueList every 60s until batch_state='completed'. Returns annotated_csv_url."""
    MAX_WAIT = 90 * 60      # 90 min — TrueList can take 60-90 min for ~2K emails
    POLL_INTERVAL = 60
    elapsed = 0
    state = "pending"
    csv_url = None

    log.info(f"  Polling {batch_id} (up to {MAX_WAIT // 60}min, every {POLL_INTERVAL}s)...")

    while state != "completed" and elapsed < MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.get(
                    f"{TRUELIST_BATCHES_URL}/{batch_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if r.status_code == 429:
                log.warning("  Rate-limited — extra 120s")
                time.sleep(120)
                elapsed += 120
                continue
            if r.status_code != 200:
                log.warning(f"  Poll HTTP {r.status_code} — retrying")
                continue
            raw = r.json()
            state = raw.get("batch_state", "unknown")
            csv_url = raw.get("annotated_csv_url")
            log.info(f"  Poll at {elapsed}s: state={state}")
        except Exception as e:
            log.warning(f"  Poll error: {e}")

    if state != "completed":
        with open(BATCH_ID_FILE, "w") as f:
            f.write(batch_id)
        raise TimeoutError(
            f"Batch {batch_id} not completed after {elapsed}s (state={state}). "
            f"Batch ID saved to {BATCH_ID_FILE}. "
            f"Re-run: python3 scripts/clean_socar_realtors.py --poll-only {batch_id}"
        )

    if not csv_url:
        raise RuntimeError(f"Batch {batch_id} completed but annotated_csv_url is empty")

    log.info(f"  Batch {batch_id} completed — CSV URL obtained")
    return csv_url


# ── ingest ────────────────────────────────────────────────────────────────────

def ingest_verdicts(conn, contacts: list, email_map: dict) -> dict:
    """
    Write TrueList verdicts to the contacts table by contact_id.
    For each contact: extract clean emails → look up in email_map →
    apply best-verdict (valid > unknown > invalid) → UPDATE by UUID.
    No-match contacts (email not in CSV) are marked 'unknown' + email_cleaned_at.
    """
    _VERDICT_ORDER = {"valid": 0, "unknown": 1, "invalid": 2}

    verdict_to_ids: dict = {}   # (status, valid) → [contact_id, ...]
    no_match_ids: list = []
    no_email_ids: list = []

    for contact_id, primary_email in contacts:
        clean_emails = _extract_emails(primary_email or "")
        if not clean_emails:
            no_email_ids.append(contact_id)
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
            continue

        verdict_to_ids.setdefault(best, []).append(contact_id)

    now_ts = datetime.now(timezone.utc).isoformat()
    counts = {
        "valid":    0,
        "invalid":  0,
        "unknown":  0,
        "no_match": len(no_match_ids),
        "no_email": len(no_email_ids),
    }

    CHUNK = 200
    for (status, valid), contact_ids in verdict_to_ids.items():
        for i in range(0, len(contact_ids), CHUNK):
            chunk = contact_ids[i:i + CHUNK]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE contacts
                    SET email_status     = %s,
                        email_valid      = %s,
                        email_cleaned_at = %s,
                        email_trust = CASE
                            WHEN %s = 'valid'   THEN 'valid'
                            WHEN %s = 'invalid' THEN 'unconfirmable'
                            ELSE email_trust
                        END
                    WHERE id = ANY(%s::uuid[])
                    """,
                    (status, valid, now_ts, status, status, chunk),
                )
                counts[status] = counts.get(status, 0) + cur.rowcount

    # Mark no-CSV-match contacts as 'unknown' + set email_cleaned_at so they don't remain NULL.
    if no_match_ids:
        log.info(f"  {len(no_match_ids)} contacts had no CSV match — marking email_status='unknown'")
        for i in range(0, len(no_match_ids), CHUNK):
            chunk = no_match_ids[i:i + CHUNK]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE contacts
                    SET email_status = 'unknown',
                        email_cleaned_at = %s
                    WHERE id = ANY(%s::uuid[])
                    """,
                    (now_ts, chunk),
                )

    if no_email_ids:
        log.warning(f"  {len(no_email_ids)} contacts had no extractable email — left uncleaned")

    total = counts["valid"] + counts["invalid"] + counts["unknown"] + counts["no_match"]
    log.info(f"  Ingest complete: total_written={total} counts={counts}")
    return counts


# ── audit table helpers ───────────────────────────────────────────────────────

def record_batch_audit(conn, batch_id: str, email_count: int, contact_count: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO truelist_batches
                (id, email_count, status, submitted_at, submitted_by, notes)
            VALUES (%s, %s, 'pending', NOW(), 'clean_socar_realtors',
                    'REK pilot: SoCal realtors — ' || %s || ' contacts, '
                    || %s || ' unique emails')
            ON CONFLICT (id) DO UPDATE
                SET notes = truelist_batches.notes || ' | re-submitted'
            """,
            (batch_id, email_count, str(contact_count), str(email_count)),
        )
    log.info(f"  Audit row written for batch {batch_id}")


def finalize_batch_audit(conn, batch_id: str, csv_url: str, counts: dict):
    summary = (
        f"valid={counts.get('valid', 0)},invalid={counts.get('invalid', 0)},"
        f"unknown={counts.get('unknown', 0)},no_match={counts.get('no_match', 0)},"
        f"no_email={counts.get('no_email', 0)}"
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE truelist_batches
            SET status            = 'ingested',
                annotated_csv_url = COALESCE(annotated_csv_url, %s),
                completed_at      = COALESCE(completed_at, NOW()),
                ingested_at       = NOW(),
                notes             = notes || ' | DONE: ' || %s
            WHERE id = %s
            """,
            (csv_url, summary, batch_id),
        )


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Email-clean SoCal realtor contacts for the REK pilot"
    )
    parser.add_argument("--dry-run", action="store_true", help="Count only, no submit")
    parser.add_argument(
        "--poll-only", metavar="BATCH_ID",
        help="Skip submit — resume polling an existing batch then ingest",
    )
    args = parser.parse_args()

    log.info("=== clean_socar_realtors.py START ===")

    conn = get_conn()

    # Load TrueList API key
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM admin_settings WHERE key='truelist_api_key'")
        row = cur.fetchone()
    if not row or not (row[0] or "").strip():
        log.error("TrueList API key not found in admin_settings")
        sys.exit(1)
    api_key = row[0].strip()
    log.info("  TrueList API key loaded")

    # Pre-run counts
    pre = query_totals(conn)
    log.info(
        f"  Pre-run: total={pre['total']}, uncleaned={pre['uncleaned']}, "
        f"already_valid={pre['email_valid_count']}"
    )

    contacts = query_target_contacts(conn)
    if not contacts:
        log.info("  Nothing to clean — 0 contacts with email_cleaned_at IS NULL in target set")
        conn.close()
        return

    if args.dry_run:
        log.info(
            f"  DRY RUN: {len(contacts)} contacts would be submitted — stopping before submit"
        )
        conn.close()
        return

    # ── SUBMIT (or resume) ──
    if args.poll_only:
        batch_id = args.poll_only
        email_count = 0  # unknown in resume mode
        log.info(f"  --poll-only: resuming batch {batch_id}")
    else:
        log.info(f"=== SUBMIT: {len(contacts)} contacts ===")
        batch_name = (
            f"kjle_socar_realtors_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        )
        batch_id, email_count = submit_to_truelist(contacts, api_key, batch_name)

        # Save for recovery if poll times out
        with open(BATCH_ID_FILE, "w") as f:
            f.write(batch_id)

        record_batch_audit(conn, batch_id, email_count, len(contacts))

    # ── POLL ──
    log.info(f"=== POLL: batch {batch_id} ===")
    try:
        csv_url = poll_until_complete(batch_id, api_key)
    except TimeoutError as e:
        log.error(str(e))
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE truelist_batches SET status='processing' WHERE id=%s",
                (batch_id,),
            )
        conn.close()
        sys.exit(2)

    # ── DOWNLOAD CSV ──
    log.info(f"=== DOWNLOAD CSV: batch {batch_id} ===")
    with httpx.Client(timeout=300.0, follow_redirects=True) as client:
        cr = client.get(csv_url, headers={"Authorization": f"Bearer {api_key}"})
    if cr.status_code != 200:
        log.error(f"  CSV download HTTP {cr.status_code}")
        conn.close()
        sys.exit(3)
    log.info(f"  CSV downloaded ({len(cr.text):,} chars)")

    email_map = _parse_annotated_csv(cr.text)
    log.info(f"  CSV parsed: {len(email_map)} email results")
    if not email_map:
        log.error("  Empty CSV — aborting ingest")
        conn.close()
        sys.exit(3)

    # ── INGEST ──
    log.info(f"=== INGEST verdicts to contacts table ===")
    counts = ingest_verdicts(conn, contacts, email_map)
    finalize_batch_audit(conn, batch_id, csv_url, counts)

    # Post-run verification
    post = query_totals(conn)
    conn.close()

    # ── REPORT ──
    report_lines = [
        "=" * 60,
        "clean_socar_realtors.py — REK PILOT REPORT",
        "=" * 60,
        f"Target set (SoCal realtors, CA):",
        f"  Total contacts:           {pre['total']}",
        f"  Already cleaned pre-run:  {pre['total'] - pre['uncleaned']}",
        f"  Submitted this run:       {len(contacts)}",
        f"  Unique emails in CSV:     {email_count or '(resume mode)'}",
        "",
        "TrueList batch results:",
        f"  Batch ID:   {batch_id}",
        f"  Valid:      {counts['valid']}",
        f"  Invalid:    {counts['invalid']}",
        f"  Unknown:    {counts['unknown']}",
        f"  No CSV match: {counts['no_match']}",
        f"  No email:   {counts['no_email']}",
        "",
        "Post-run state (SoCal realtor target set):",
        f"  email_valid=true (REK-ready):  {post['email_valid_count']}",
        f"  email_cleaned_at IS NULL:      {post['uncleaned']}",
        f"  email_status breakdown:        valid={post['status_valid']}, "
        f"invalid={post['status_invalid']}, unknown={post['status_unknown']}",
        "",
        f"Batch ID: {batch_id}",
        f"Log:      {LOG_FILE}",
        "=" * 60,
    ]
    report = "\n".join(report_lines)
    log.info("\n" + report)
    with open(REPORT_FILE, "w") as f:
        f.write(report)
    print(report)
    log.info("=== clean_socar_realtors.py DONE ===")


if __name__ == "__main__":
    main()
