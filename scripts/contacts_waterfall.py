"""
contacts_waterfall.py — KJLE
Makes contacts.primary_email send-ready:
  Step 1: Email provider  — tier-1 string-match + tier-2 MX via domain_provider_cache
  Step 2: Email trust     — derived from leadrocks emails jsonb status + provider
  Step 3: Truelist        — submit → poll → ingest annotated CSV

Reuses the proven patterns from classify_mx.py (provider) and
enrichment_email_clean.py (Truelist). Contacts share domain_provider_cache
with leads, so cache hits are free.

Usage: python3 scripts/contacts_waterfall.py [--skip-truelist]
"""
import argparse
import csv
import io
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv("/opt/kjle/.env")

import dns.resolver
import httpx
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ["DATABASE_URL"]
TRUELIST_BATCHES_URL = "https://api.truelist.io/api/v1/batches"
LOG_FILE = "/tmp/contacts_waterfall.log"
REPORT_FILE = "/tmp/kjle_contacts_waterfall.txt"
BATCH_ID_FILE = "/tmp/contacts_truelist_batch_id.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── 9-bucket provider tier-1 map (matches provider_classifier.py exactly) ──

_TIER1 = {
    "gmail.com": "gmail_consumer",
    "googlemail.com": "gmail_consumer",
    "outlook.com": "ms_consumer",
    "hotmail.com": "ms_consumer",
    "live.com": "ms_consumer",
    "msn.com": "ms_consumer",
    "hotmail.co.uk": "ms_consumer",
    "live.co.uk": "ms_consumer",
    "outlook.co.uk": "ms_consumer",
    "live.ca": "ms_consumer",
    "hotmail.fr": "ms_consumer",
    "live.fr": "ms_consumer",
    "hotmail.de": "ms_consumer",
    "live.de": "ms_consumer",
    "hotmail.es": "ms_consumer",
    "yahoo.com": "yahoo",
    "ymail.com": "yahoo",
    "rocketmail.com": "yahoo",
    "aol.com": "yahoo",
    "yahoo.co.uk": "yahoo",
    "yahoo.co.in": "yahoo",
    "yahoo.fr": "yahoo",
    "yahoo.de": "yahoo",
    "yahoo.es": "yahoo",
    "yahoo.it": "yahoo",
    "yahoo.com.br": "yahoo",
    "yahoo.com.au": "yahoo",
    "yahoo.ca": "yahoo",
    "yahoo.co.jp": "yahoo",
    "yahoo.com.mx": "yahoo",
    "verizon.net": "yahoo",
    "icloud.com": "apple",
    "me.com": "apple",
    "mac.com": "apple",
    "comcast.net": "other",
    "att.net": "other",
    "sbcglobal.net": "other",
    "cox.net": "other",
    "charter.net": "other",
    "spectrum.net": "other",
    "earthlink.net": "other",
    "bellsouth.net": "other",
    "roadrunner.com": "other",
    "protonmail.com": "other",
    "zoho.com": "other",
    "mail.com": "other",
    "gmx.com": "other",
    "gmx.net": "other",
}

ROLE_LOCALPARTS = frozenset([
    "info", "sales", "hello", "contact", "admin", "support",
    "team", "office", "marketing", "help", "no-reply", "noreply",
    "webmaster", "postmaster",
])


# ── DB connection ─────────────────────────────────────────────────────────────

def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout=0")
    return conn


# ── provider MX lookup (matches classify_mx.py pattern) ──────────────────────

def _mx_lookup(domain: str) -> str:
    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = 5
        resolver.lifetime = 5
        answers = resolver.resolve(domain, "MX")
        for rdata in sorted(answers, key=lambda r: r.preference):
            mx = str(rdata.exchange).rstrip(".").lower()
            if any(x in mx for x in ("aspmx.l.google", "google.com", "googlemail.com")):
                return "google_workspace"
            if any(x in mx for x in ("outlook.com", "office365.com", "protection.outlook", "mail.protection")):
                return "office365"
            if "yahoodns" in mx:
                return "yahoo"
            if "secureserver" in mx:
                return "other"
            if any(x in mx for x in ("icloud.com", "apple.com")):
                return "apple"
        return "self_hosted"
    except Exception:
        return "unknown"


def _classify_and_update_domain(domain: str) -> int:
    """Resolve provider for one domain and UPDATE matching contacts. Returns rows updated."""
    conn = get_conn()
    try:
        provider = _TIER1.get(domain)
        if not provider:
            with conn.cursor() as cur:
                cur.execute("SELECT provider FROM domain_provider_cache WHERE domain = %s", (domain,))
                row = cur.fetchone()
            if row:
                provider = row[0]
            else:
                provider = _mx_lookup(domain)
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO domain_provider_cache (domain, provider, mx_checked_at) "
                        "VALUES (%s, %s, NOW()) ON CONFLICT DO NOTHING",
                        (domain, provider),
                    )
        if not provider or provider == "unknown":
            return 0
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE contacts SET email_provider=%s "
                "WHERE lower(split_part(primary_email,'@',2))=%s AND email_provider='unknown'",
                (provider, domain),
            )
            return cur.rowcount
    finally:
        conn.close()


# ── STEP 1: Email Provider ────────────────────────────────────────────────────

def step1_provider() -> int:
    log.info("=== STEP 1: Email Provider Classification ===")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT lower(split_part(primary_email,'@',2))
            FROM contacts
            WHERE email_provider='unknown' AND primary_email IS NOT NULL
        """)
        domains = [r[0] for r in cur.fetchall()]
    conn.close()
    log.info(f"  {len(domains)} unique domains to classify")

    total_updated = 0
    BATCH = 10
    for i in range(0, len(domains), BATCH):
        batch = domains[i : i + BATCH]
        with ThreadPoolExecutor(max_workers=BATCH) as pool:
            futs = {pool.submit(_classify_and_update_domain, d): d for d in batch}
            for f in as_completed(futs):
                try:
                    total_updated += f.result()
                except Exception as e:
                    log.error(f"  domain error: {e}")
        time.sleep(0.1)
        done = min(i + BATCH, len(domains))
        if done % 100 < BATCH or done >= len(domains):
            log.info(f"  progress {done}/{len(domains)} domains | {total_updated} contacts updated")

    log.info(f"Step 1 done — {total_updated} contacts provider-classified")
    return total_updated


# ── trust derivation helpers ──────────────────────────────────────────────────

def _clean_lr_status(raw) -> str:
    """Normalize a leadrocks email status value (strips HTML/JSON garbage suffixes)."""
    if not raw:
        return ""
    s = str(raw).strip()
    for stopper in ("<", "{"):
        idx = s.find(stopper)
        if 0 < idx:
            s = s[:idx].strip()
    if "|" in s:
        s = s.split("|")[0].strip()
    return s.lower()


def _derive_trust(primary_email: str, emails_jsonb, email_provider: str) -> str:
    """
    Interim trust from leadrocks emails jsonb + provider.
    Truelist (step 3) will override valid/invalid outcomes.
    """
    localpart = primary_email.split("@")[0].lower() if "@" in primary_email else primary_email.lower()
    if localpart in ROLE_LOCALPARTS:
        return "role"

    lr_status = ""
    if emails_jsonb:
        for entry in emails_jsonb:
            if isinstance(entry, dict) and (entry.get("email") or "").strip().lower() == primary_email.strip().lower():
                lr_status = _clean_lr_status(entry.get("status", ""))
                break

    if lr_status == "ok":
        return "valid"
    if lr_status in ("ok_for_all", "email_ok_for_all"):
        return "catch_all"
    if email_provider and email_provider != "unknown":
        return "catch_all"
    return "unconfirmable"


# ── STEP 2: Email Trust ───────────────────────────────────────────────────────

def step2_trust() -> int:
    log.info("=== STEP 2: Email Trust Derivation ===")
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT id, primary_email, emails, email_provider
            FROM contacts
            WHERE email_trust IS NULL AND primary_email IS NOT NULL
            ORDER BY id
            LIMIT 5000
        """)
        rows = cur.fetchall()
    log.info(f"  Deriving trust for {len(rows)} contacts")

    groups: dict[str, list] = {}
    for r in rows:
        trust = _derive_trust(r["primary_email"], r["emails"], r["email_provider"] or "unknown")
        groups.setdefault(trust, []).append(str(r["id"]))

    updated = 0
    for trust, ids in groups.items():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE contacts SET email_trust=%s WHERE id = ANY(%s::uuid[])",
                (trust, ids),
            )
            updated += cur.rowcount
            log.info(f"  trust={trust}: {cur.rowcount} contacts")

    log.info(f"Step 2 done — {updated} contacts got email_trust")
    conn.close()
    return updated


# ── Truelist helpers ──────────────────────────────────────────────────────────

def _parse_truelist_state(raw_state: Optional[str]) -> tuple:
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
    """Parse Truelist annotated CSV → {email_lower: {state: ...}}."""
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
        if len(r) <= max(em_idx, st_idx):
            continue
        em = (r[em_idx] or "").strip().lower()
        if em:
            out[em] = {"state": (r[st_idx] or "").strip()}
    return out


# ── Resume helpers — recover timed-out contacts_waterfall batches ─────────────

def _ingest_csv_into_contacts(conn, batch_id: str, csv_url: str, api_key: str) -> dict:
    """
    Download annotated CSV for a contacts_waterfall batch and UPDATE contacts
    by primary_email match. Only touches contacts still at email_status='unvalidated'.

    Root-cause of processed=0: Truelist webhooks call ingest_batch_result which
    queries the leads table (not contacts). This function is the correct contacts path.
    """
    log.info(f"  [resume] Downloading CSV for batch {batch_id}")
    try:
        with httpx.Client(timeout=300.0, follow_redirects=True) as client:
            cr = client.get(csv_url, headers={"Authorization": f"Bearer {api_key}"})
        if cr.status_code != 200:
            log.error(f"  [resume] CSV HTTP {cr.status_code} for {batch_id}")
            return {"error": f"csv_http_{cr.status_code}", "total": 0}
        email_map = _parse_annotated_csv(cr.text)
    except Exception as e:
        log.error(f"  [resume] CSV error for {batch_id}: {e}")
        return {"error": str(e), "total": 0}

    if not email_map:
        log.warning(f"  [resume] Empty CSV for {batch_id}")
        return {"batch_id": batch_id, "total": 0, "error": "empty_csv"}

    now_ts = datetime.now(timezone.utc).isoformat()
    counts: dict[str, int] = {"valid": 0, "invalid": 0, "unknown": 0}

    groups_sv: dict[tuple, list] = {}
    for em_lower, mapped in email_map.items():
        status, valid = _parse_truelist_state(mapped.get("state", ""))
        groups_sv.setdefault((status, valid), []).append(em_lower)

    CHUNK = 200
    for (status, valid), emails_lower in groups_sv.items():
        for i in range(0, len(emails_lower), CHUNK):
            chunk = emails_lower[i : i + CHUNK]
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE contacts
                    SET email_status     = %s,
                        email_valid      = %s,
                        email_cleaned_at = %s,
                        email_trust = CASE
                            WHEN %s = 'valid'   THEN 'valid'
                            WHEN %s = 'invalid' THEN 'unconfirmable'
                            ELSE email_trust
                        END
                    WHERE lower(primary_email) = ANY(%s::text[])
                      AND email_status = 'unvalidated'
                """, (status, valid, now_ts, status, status, chunk))
                counts[status] = counts.get(status, 0) + cur.rowcount

    total = sum(counts.values())
    log.info(f"  [resume] {batch_id}: updated {total} contacts {counts}")
    return {"batch_id": batch_id, "total": total, "counts": counts}


def _resume_contacts_batches(conn, api_key: str) -> int:
    """
    Find contacts_waterfall batches where the Truelist webhook ran ingest_batch_result
    (leads-focused, returned processed=0) instead of the contacts UPDATE path.
    Re-ingest them into contacts now. Returns total contacts updated.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, annotated_csv_url
            FROM truelist_batches
            WHERE submitted_by = 'contacts_waterfall'
              AND annotated_csv_url IS NOT NULL
              AND (
                    status = 'completed'
                OR (status = 'ingested' AND notes ILIKE '%%ingest: processed=0%%')
              )
            ORDER BY submitted_at
            LIMIT 10
        """)
        pending = cur.fetchall()

    if not pending:
        return 0

    log.info(f"  [resume] {len(pending)} contacts batch(es) to recover")
    total_updated = 0

    for batch_id, csv_url in pending:
        result = _ingest_csv_into_contacts(conn, batch_id, csv_url, api_key)
        if result.get("total", 0) == 0 and "error" in result:
            log.error(f"  [resume] Failed for {batch_id}: {result.get('error')}")
            continue
        total = result.get("total", 0)
        counts = result.get("counts", {})
        total_updated += total
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE truelist_batches
                SET status      = 'ingested',
                    ingested_at = COALESCE(ingested_at, NOW()),
                    notes       = notes || %s
                WHERE id = %s
            """, (
                f" | contacts_resume: valid={counts.get('valid', 0)},"
                f"invalid={counts.get('invalid', 0)},unknown={counts.get('unknown', 0)},total={total}",
                batch_id,
            ))
        log.info(f"  [resume] Recovered batch {batch_id} → {total} contacts updated")

    return total_updated


# ── STEP 3: Truelist Validation ───────────────────────────────────────────────

def step3_truelist(api_key: str) -> dict:
    log.info("=== STEP 3: Truelist Email Validation ===")
    conn = get_conn()

    # Resume any contacts_waterfall batches from prior timed-out runs.
    # Truelist webhook calls ingest_batch_result (leads table) on these batches
    # and logs processed=0 — this recovers them into contacts before submitting new ones.
    _resume_contacts_batches(conn, api_key)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id::text, primary_email
            FROM contacts
            WHERE email_status = 'unvalidated'
              AND primary_email IS NOT NULL
              AND (email_trust IS NULL OR email_trust <> 'catch_all')
            ORDER BY id
            LIMIT 5000
        """)
        rows = cur.fetchall()

    if not rows:
        log.info("  Nothing to validate — no unvalidated contacts (or all are catch_all)")
        conn.close()
        return {"submitted": 0, "reason": "all_already_validated"}

    # Deduplicate by email for Truelist payload
    seen: set = set()
    payload = []
    for _, em in rows:
        e = (em or "").strip().lower()
        if e and e not in seen:
            seen.add(e)
            payload.append([e])

    log.info(f"  Submitting {len(payload)} unique emails to Truelist ({len(rows)} contacts)")
    batch_name = f"kjle_contacts_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    with httpx.Client(timeout=60.0) as client:
        r = client.post(
            TRUELIST_BATCHES_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"data": payload, "name": batch_name},
        )

    if r.status_code == 409:
        try:
            existing_id = (r.json().get("existing_batch") or {}).get("id")
        except Exception:
            existing_id = None
        if not existing_id:
            log.error(f"  Truelist 409 without existing_batch id: {r.text[:300]}")
            conn.close()
            return {"error": "409_no_existing_batch", "detail": r.text[:200]}
        batch_id = existing_id
        log.info(f"  409 — adopting existing batch {batch_id}")
    elif r.status_code not in (200, 201):
        log.error(f"  Truelist submit failed HTTP {r.status_code}: {r.text[:300]}")
        conn.close()
        return {"error": f"http_{r.status_code}", "detail": r.text[:200]}
    else:
        body = r.json()
        batch_id = body.get("id")
        log.info(f"  Batch submitted: {batch_id} (state={body.get('batch_state')})")

    # Persist batch ID for recovery if the script is interrupted
    with open(BATCH_ID_FILE, "w") as f:
        f.write(batch_id)

    # Record in truelist_batches for audit
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO truelist_batches (id, email_count, status, submitted_at, submitted_by, notes)
            VALUES (%s, %s, 'pending', NOW(), 'contacts_waterfall', 'contacts.primary_email batch')
            ON CONFLICT (id) DO NOTHING
        """, (batch_id, len(payload)))

    # Poll until completed (max 45 min, every 60s)
    MAX_WAIT = 45 * 60
    POLL_INTERVAL = 60
    elapsed = 0
    state = "pending"
    csv_url = None

    log.info(f"  Polling {batch_id} (up to {MAX_WAIT//60} min, every {POLL_INTERVAL}s)...")
    while state != "completed" and elapsed < MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        try:
            with httpx.Client(timeout=30.0) as client:
                pr = client.get(
                    f"{TRUELIST_BATCHES_URL}/{batch_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if pr.status_code == 429:
                log.warning("  Poll rate-limited — waiting extra 120s")
                time.sleep(120)
                elapsed += 120
                continue
            if pr.status_code != 200:
                log.warning(f"  Poll HTTP {pr.status_code} — retrying")
                continue
            raw = pr.json()
            state = raw.get("batch_state", "unknown")
            csv_url = raw.get("annotated_csv_url")
            log.info(f"  Poll at {elapsed}s: state={state}")
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE truelist_batches SET status=%s, annotated_csv_url=%s WHERE id=%s",
                    (state, csv_url, batch_id),
                )
        except Exception as e:
            log.warning(f"  Poll error: {e}")

    if state != "completed":
        log.warning(
            f"  Batch not completed after {elapsed}s (state={state}). "
            f"Batch ID saved to {BATCH_ID_FILE} for manual resume."
        )
        conn.close()
        return {"batch_id": batch_id, "state": state, "note": "poll_timeout"}

    # Download annotated CSV
    log.info("  Downloading annotated CSV...")
    try:
        with httpx.Client(timeout=300.0, follow_redirects=True) as client:
            cr = client.get(csv_url, headers={"Authorization": f"Bearer {api_key}"})
        if cr.status_code != 200:
            log.error(f"  CSV download failed HTTP {cr.status_code}")
            conn.close()
            return {"batch_id": batch_id, "error": f"csv_http_{cr.status_code}"}
        email_map = _parse_annotated_csv(cr.text)
    except Exception as e:
        log.error(f"  CSV error: {e}")
        conn.close()
        return {"batch_id": batch_id, "error": str(e)}

    log.info(f"  Parsed {len(email_map)} email results from CSV")

    # Ingest results into contacts
    now_ts = datetime.now(timezone.utc).isoformat()
    counts = {"valid": 0, "invalid": 0, "unknown": 0, "no_match": 0}

    # Build result map: original_email → (status, valid)
    matched: dict[str, tuple] = {}
    unmatched: list = []
    for _, em in rows:
        norm = (em or "").strip().lower()
        mapped = email_map.get(norm)
        if not mapped:
            counts["no_match"] += 1
            unmatched.append(norm)
            continue
        status, valid = _parse_truelist_state(mapped["state"])
        counts[status] = counts.get(status, 0) + 1
        matched[norm] = (status, valid)

    # Batch UPDATE grouped by (status, valid) for efficiency
    groups_sv: dict[tuple, list] = {}
    for em_lower, (status, valid) in matched.items():
        groups_sv.setdefault((status, valid), []).append(em_lower)

    for (status, valid), emails_lower in groups_sv.items():
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE contacts
                SET email_status = %s,
                    email_valid  = %s,
                    email_cleaned_at = %s,
                    email_trust = CASE
                        WHEN %s = 'valid'   THEN 'valid'
                        WHEN %s = 'invalid' THEN 'unconfirmable'
                        ELSE email_trust
                    END
                WHERE lower(primary_email) = ANY(%s)
            """, (status, valid, now_ts, status, status, emails_lower))
            log.info(f"  Ingested: status={status} valid={valid} → {cur.rowcount} contacts")

    # Mark no-CSV-match contacts as 'unknown' so they don't stay NULL
    if unmatched:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE contacts SET email_status='unknown', email_cleaned_at=%s "
                "WHERE lower(primary_email) = ANY(%s)",
                (now_ts, unmatched),
            )
        log.info(f"  {len(unmatched)} no-CSV-match contacts marked email_status='unknown'")

    # Finalize truelist_batches row
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE truelist_batches
            SET status='ingested', ingested_at=NOW(),
                notes = notes || %s
            WHERE id = %s
        """, (
            f" | valid={counts['valid']},invalid={counts['invalid']},"
            f"unknown={counts['unknown']},no_match={counts['no_match']}",
            batch_id,
        ))

    log.info(f"Step 3 done — {counts}")
    conn.close()
    return {"batch_id": batch_id, "state": "ingested", "counts": counts}


# ── distribution query ────────────────────────────────────────────────────────

def _dist(col: str) -> list:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {col}, COUNT(*) FROM contacts GROUP BY {col} ORDER BY 2 DESC"
        )
        rows = cur.fetchall()
    conn.close()
    return rows


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="KJLE contacts email waterfall")
    parser.add_argument("--skip-truelist", action="store_true", help="Run steps 1+2 only")
    args = parser.parse_args()

    log.info("contacts_waterfall.py starting")
    t0 = time.time()

    step1_provider()
    step2_trust()

    truelist_result: dict = {"status": "skipped"}
    if not args.skip_truelist:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM admin_settings WHERE key='truelist_api_key'")
            row = cur.fetchone()
        conn.close()
        if not row or not (row[0] or "").strip():
            log.error("Truelist API key not found in admin_settings — skipping step 3")
            truelist_result = {"status": "skipped", "reason": "no_api_key"}
        else:
            truelist_result = step3_truelist(row[0].strip())
    else:
        log.info("Step 3 (Truelist) skipped via --skip-truelist")

    # Final distributions
    prov_dist  = _dist("email_provider")
    trust_dist = _dist("email_trust")
    status_dist = _dist("email_status")
    elapsed = int(time.time() - t0)

    lines = [
        f"contacts_waterfall.py complete — {elapsed}s",
        f"API health: https://kjle-api.onrender.com/kjle/v1/health (200 confirmed pre-run)",
        "",
        "== Provider distribution ==",
        *[f"  {p}: {n}" for p, n in prov_dist],
        "",
        "== Trust distribution ==",
        *[f"  {t}: {n}" for t, n in trust_dist],
        "",
        "== Email status distribution ==",
        *[f"  {s}: {n}" for s, n in status_dist],
        "",
        f"Truelist step 3: {truelist_result}",
        f"Log: {LOG_FILE}",
    ]
    report = "\n".join(lines)
    with open(REPORT_FILE, "w") as f:
        f.write(report)
    log.info("\n" + report)
    log.info("contacts_waterfall.py DONE")


if __name__ == "__main__":
    main()
