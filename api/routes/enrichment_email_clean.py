"""
KJLE — Email Cleaning via Truelist Bulk Batches API
Route prefix handled in main.py: /kjle/v1/enrichment

V2 architecture (Session 2B, 2026-05-10) — bulk POST /api/v1/batches.

Replaces the v1 verify_inline pipeline which was rate-limited to ~1 RPS
and capped at 2,500 emails/night. V2 submits 25K-email batches to
Truelist and ingests the annotated CSV per batch on completion. ~19
batches clears the 469K backlog.

Truelist API:
  Submit: POST https://api.truelist.io/api/v1/batches
          body: multipart/form-data — field "file" is a CSV with header "email",
                one address per row. Optional "name" field in the same form.
          response: {"id": <uuid>, "batch_state": "pending", "email_count": N, ...}
  NOTE (2026-10-11): TrueList changed from accepting JSON {"data":[...]} to
  requiring a CSV file upload. The 422 "No email column found. Please check
  your CSV!" is returned when the old JSON body is sent.
  Poll:   GET  https://api.truelist.io/api/v1/batches/{uuid}
          response: includes batch_state and (on completion) 4 CSV URLs
          states observed: pending → processing → completed
  CSV:    GET annotated_csv_url
          columns: <leading-blank>, Email Address, Did you mean, Email State, Email Sub-State
          values:  Email State ∈ {ok, invalid, risky, unknown}
  Auth:   Authorization: Bearer <TRUELIST_API_KEY>

Mapping from Truelist `Email State` → KJLE columns:
  ok       → email_status='valid',         email_valid=True,  email_trust='valid'
  invalid  → email_status='invalid',       email_valid=False, email_trust='invalid'
  risky    → email_status='unknown',       email_valid=False, email_trust='catch_all'
  unknown  → email_status='unconfirmable', email_valid=False, email_trust='unconfirmable'

  email_status uses the legacy 4-value vocab (valid/invalid/unknown/unconfirmable) so
  existing filters on that column keep working. email_trust is set directly from TrueList
  at ingest time, bypassing the Tier-1 heuristic classifier for these leads.

⚠️ CAMPAIGN-ELIGIBLE FILTER — POSITIVE WHITELIST ONLY (per Session 2B plan)
  When selecting leads for outbound email campaigns, callers MUST use:
      email_status IN ('valid')              [positive whitelist]
      OR email_state IN ('ok')               [if filtering on raw Truelist column]
  NEVER use:
      email_status != 'invalid'              [includes unknown — would email risky/role/accept-all]
      email_state  != 'invalid'              [same problem]
  The negative form silently includes the 23K+ unknown bucket which contains role
  emails (info@, sales@) and accept-all domains. Sending to them = bounce/spam risk.
  See test fixtures: scripts/test_email_clean_parser.py::test_campaign_eligible_*.

KNOWN HISTORY:
  v1 parser (now removed) checked for state=='bad' but Truelist returns
  'invalid'. This silently bucketed all invalid emails as 'unknown' —
  ~5K-ish hidden invalids in the existing 87,701 cleaned leads. The 19,937
  rows currently labeled 'unknown' will be re-verified in Session 2C.
"""

from __future__ import annotations

import csv
import asyncio
import io
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db
from ..config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


class TruelistTransientError(Exception):
    """Raised when TrueList returns a transient 5xx or network error after all retries.

    Callers should log a WARNING, leave the batch status unchanged, and continue
    to the next batch — the cron will retry on the next 30-min cycle.
    """


TRUELIST_BATCHES_URL  = "https://api.truelist.io/api/v1/batches"
TRUELIST_VERIFY_URL   = "https://api.truelist.io/api/v1/verify_inline"  # retained for single endpoint

INGEST_CHUNK_SIZE     = 500   # lead-id chunks for CSV ingestion SELECT paging
EMAIL_INGEST_CHUNK    = 300   # email-list chunks for lower(email)=ANY() UPDATE
STAMP_CHUNK_SIZE      = 100   # lead-id chunks for UPDATE .in_() calls
                              # 100 UUIDs × 37 chars = ~3.7KB URL, safe under Supabase's
                              # 8KB PATCH URL limit (500 × 37 = 18KB → hit 414 silently)

# Matches trailing provider annotations like " (accept_all)" or " (risky)" in multi-email fields.
# These annotations are appended by LeadRocks/LeadSays ingest and cause TrueList 422 when included
# in the CSV (the field is not a valid email address with them present).
_ANNOTATION_RE = re.compile(r'\s*\([^)]*\)\s*$')


def _extract_emails(raw: str) -> list[str]:
    """
    Extract individual valid-looking email addresses from an email field that may contain:
      - A single clean address:          "foo@bar.com"
      - An annotated address:            "foo@bar.com (accept_all)"
      - Multiple annotated addresses:    "a@b.com (accept_all) , c@d.com (accept_all)"
      - Multiple addresses, no annot:    "a@b.com, c@d.com"

    Splits on commas, strips surrounding whitespace, removes trailing "(…)" annotations,
    then validates each candidate (has @, no spaces, non-empty, ≤254 chars).

    Returns a list of lowercased clean email addresses. Empty list if none are found.
    """
    if not raw:
        return []
    results: list[str] = []
    for part in re.split(r'\s*,\s*', raw.strip()):
        clean = _ANNOTATION_RE.sub('', part).strip()
        if clean and '@' in clean and ' ' not in clean and len(clean) <= 254:
            results.append(clean.lower())
    return results
SUBMIT_TIMEOUT_SEC    = 60.0
POLL_TIMEOUT_SEC      = 30.0
CSV_DOWNLOAD_TIMEOUT  = 300.0
_TRANSIENT_5XX        = frozenset({502, 503, 504})
_TRANSIENT_BACKOFF    = (2.0, 5.0, 10.0)   # seconds; 3 retries before giving up


# ─────────────────────────────────────────────────────────────────────────────
# State mapping — pure helpers (unit-tested in scripts/test_email_clean_parser.py)
# ─────────────────────────────────────────────────────────────────────────────

def parse_truelist_state(raw_state: Optional[str]) -> tuple[str, Optional[bool]]:
    """
    Map a Truelist `Email State` value (from batch CSV OR verify_inline) to
    KJLE's (email_status, email_valid) pair.

    Accepts both the clean batch-CSV vocabulary (`ok` / `invalid` / `risky` /
    `unknown`) and verify_inline's occasional `email_*` prefixed forms
    (`email_ok` / `email_invalid` / `email_risky` / `email_unknown`) — same
    semantics, different naming.

    Returns:
      ("valid",         True)   when Truelist says ok
      ("invalid",       False)  when Truelist says invalid
      ("unknown",       False)  when Truelist says risky (accept_all / catch-all domain)
      ("unconfirmable", False)  when Truelist says unknown or anything else
    """
    if not raw_state:
        return ("unconfirmable", False)
    s = str(raw_state).strip().lower()
    # Strip `email_` prefix if present (verify_inline inconsistency)
    if s.startswith("email_"):
        s = s[len("email_"):]
    if s == "ok":
        return ("valid", True)
    if s == "invalid":
        return ("invalid", False)
    if s == "risky":
        return ("unknown", False)   # accept_all / catch-all domain
    return ("unconfirmable", False) # unknown or any unrecognised state


def _truelist_trust(raw_state: Optional[str]) -> str:
    """
    Map a Truelist `Email State` to the email_trust vocabulary.
    Differentiates 'risky' (catch_all domains) from 'unknown' (genuinely unconfirmable)
    — a distinction parse_truelist_state cannot express via email_status alone.

    Best-verdict precedence: valid(0) > unconfirmable(1) > catch_all(2) > invalid(3)
    """
    if not raw_state:
        return "unconfirmable"
    s = str(raw_state).strip().lower()
    if s.startswith("email_"):
        s = s[len("email_"):]
    if s == "ok":
        return "valid"
    if s == "invalid":
        return "invalid"
    if s == "risky":
        return "catch_all"      # accept_all / catch-all domain
    return "unconfirmable"      # unknown or anything else


def is_campaign_eligible(email_status: Optional[str]) -> bool:
    """
    Positive whitelist for outbound email campaigns.
    ONLY `valid` is eligible. Unknown/error/None/anything else → not eligible.
    See module docstring for the rationale (don't email risky/role/accept-all).
    """
    return email_status == "valid"


# ─────────────────────────────────────────────────────────────────────────────
# API key loader — admin_settings first, env var fallback
# ─────────────────────────────────────────────────────────────────────────────

def _get_truelist_api_key(db) -> str:
    """Load Truelist API key from admin_settings, fall back to config env var."""
    try:
        res = db.table("admin_settings").select("value").eq("key", "truelist_api_key").execute()
        if res.data and res.data[0].get("value"):
            val = res.data[0]["value"].strip()
            if val:
                return val
    except Exception as e:
        logger.warning(f"Could not load truelist_api_key from admin_settings: {e}")

    if settings.TRUELIST_API_KEY:
        return settings.TRUELIST_API_KEY

    raise HTTPException(
        status_code=400,
        detail="Truelist API key not configured. Set via POST /admin/settings with key='truelist_api_key'.",
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Batch submitter
# ─────────────────────────────────────────────────────────────────────────────

async def submit_batch(
    db,
    api_key: str,
    leads: list[dict],
    *,
    submitted_by: str,
    notes: Optional[str] = None,
) -> dict:
    """
    Submit a single batch of leads to Truelist /api/v1/batches and record
    the batch in the truelist_batches table. Marks each lead with
    email_status='pending_batch' and email_truelist_batch_id=<batch_id>.

    leads: [{"id": uuid, "email": "..."}, ...] — emails must be normalized
    upstream. Empty list → returns {"submitted": 0, "skipped": True}.
    """
    if not leads:
        return {"submitted": 0, "skipped": True, "reason": "empty_batch"}

    # Extract individual email addresses from each lead's email field.
    # The email column may contain multiple addresses and/or provider annotations
    # (e.g. "foo@bar.com (accept_all) , baz@qux.com (accept_all)") from the
    # LeadRocks/LeadSays ingest. TrueList rejects a CSV that contains these raw
    # strings — it expects one plain, valid email address per row.
    # _extract_emails() splits on commas, strips "(…)" annotations, and validates
    # each candidate. The lead is stamped by ID (not by email) so multi-email
    # leads are still correctly associated with the batch for later ingestion.
    seen_emails: set[str] = set()
    payload_data: list[list[str]] = []
    stampable_ids: list[str] = []

    for lead in leads:
        lead_id = lead.get("id")
        raw_em = (lead.get("email") or "").strip()
        extracted = _extract_emails(raw_em)
        if not extracted:
            logger.warning(
                f"[submit_batch] lead {lead_id}: no extractable email from "
                f"{raw_em!r:.120s} — skipping"
            )
            continue
        for em in extracted:
            if em not in seen_emails:
                seen_emails.add(em)
                payload_data.append([em])
        if lead_id:
            stampable_ids.append(lead_id)

    if not payload_data:
        return {"submitted": 0, "skipped": True, "reason": "no_usable_emails"}

    # Build CSV with "email" header — TrueList /api/v1/batches requires a CSV
    # file upload (multipart/form-data), not a JSON body. A single "email" column
    # is the unambiguous safe form per TrueList's current API.
    csv_buf = io.StringIO()
    csv_writer = csv.writer(csv_buf)
    csv_writer.writerow(["email"])
    for row in payload_data:
        csv_writer.writerow(row)
    csv_bytes = csv_buf.getvalue().encode("utf-8")

    # Guard: the CSV must have at least one data row and the email header.
    # Unreachable under normal flow (payload_data check above), but prevents
    # sending a headerless or empty CSV on any future regression.
    if len(payload_data) == 0 or not csv_bytes.strip().startswith(b"email"):
        raise ValueError(
            f"submit_batch: CSV sanity check failed — "
            f"data_rows={len(payload_data)}, csv_start={csv_bytes[:40]!r}. "
            "Not submitting to Truelist."
        )

    # Unique name per submission so Truelist doesn't hash-dedup identical email lists
    # across nightly runs (same 9K unknown leads → same content hash → 409 without this).
    batch_name = f"kjle_reval_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    async with httpx.AsyncClient(timeout=SUBMIT_TIMEOUT_SEC) as client:
        r = await client.post(
            TRUELIST_BATCHES_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("emails.csv", csv_bytes, "text/csv")},
            data={"name": batch_name},
        )

    adopted_existing = False
    if r.status_code == 409:
        # Truelist deduped the upload — extract the pre-existing batch id and adopt it
        # so the leads still get associated with a live batch and drained normally.
        try:
            err_body = r.json()
            existing_batch_id = (err_body.get("existing_batch") or {}).get("id")
        except Exception:
            existing_batch_id = None
        if existing_batch_id:
            logger.warning(
                f"[truelist.submit_batch] Truelist returned existing batch {existing_batch_id} "
                f"for duplicate submission; adopting it"
            )
            body = {"id": existing_batch_id}
            adopted_existing = True
        else:
            body_excerpt = r.text[:500]
            logger.error(f"[truelist.submit_batch] HTTP 409 without existing_batch: {body_excerpt}")
            raise HTTPException(
                status_code=502,
                detail=f"Truelist submit failed: HTTP 409 (no existing_batch id): {body_excerpt}",
            )
    elif r.status_code != 200 and r.status_code != 201:
        body_excerpt = r.text[:500]
        logger.error(f"[truelist.submit_batch] HTTP {r.status_code}: {body_excerpt}")
        raise HTTPException(
            status_code=502,
            detail=f"Truelist submit failed: HTTP {r.status_code}: {body_excerpt}",
        )
    else:
        body = r.json()

    batch_id = body.get("id")
    if not batch_id:
        logger.error(f"[truelist.submit_batch] response missing id: {body}")
        raise HTTPException(status_code=502, detail="Truelist response missing batch id")

    # Insert truelist_batches row; on the adopt path the row may already exist
    # (it was created when the original submission went through), so upsert with
    # ignore_duplicates to keep it idempotent.
    try:
        batch_record = {
            "id":           batch_id,
            "email_count":  body.get("email_count") or len(payload_data),
            "status":       body.get("batch_state") or "pending",
            "submitted_at": _now_iso(),
            "submitted_by": submitted_by,
            "notes":        notes,
        }
        if adopted_existing:
            db.table("truelist_batches").upsert(
                batch_record, on_conflict="id", ignore_duplicates=True
            ).execute()
        else:
            db.table("truelist_batches").insert(batch_record).execute()
    except Exception as e:
        # If audit insert fails, the batch is still live at Truelist — bail
        # before marking leads pending_batch so we don't lose track of them.
        logger.error(f"[truelist.submit_batch] audit insert failed for {batch_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to record batch {batch_id}: {e}")

    # Mark leads pending_batch in chunks of STAMP_CHUNK_SIZE.
    # CRITICAL: ingest_batch_result finds leads via WHERE email_truelist_batch_id=batch_id.
    # If stamping fails silently, ingest returns processed=0 and the leads get re-submitted
    # the next night to a different batch. Use STAMP_CHUNK_SIZE=100 to stay under Supabase's
    # 8KB PATCH URL limit (500 UUIDs × 37 chars = 18KB → silent 414, was the root-cause bug).
    # stampable_ids contains only lead IDs that had at least one extractable email address.
    updated = 0
    for i in range(0, len(stampable_ids), STAMP_CHUNK_SIZE):
        chunk = stampable_ids[i:i + STAMP_CHUNK_SIZE]
        try:
            db.table("leads").update({
                "email_status":            "pending_batch",
                "email_truelist_batch_id": batch_id,
            }).in_("id", chunk).execute()
            updated += len(chunk)
        except Exception as e:
            logger.error(
                f"[truelist.submit_batch] lead mark-pending chunk failed for batch {batch_id} "
                f"(offset={i}, chunk_size={len(chunk)}): {e}"
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to stamp leads for batch {batch_id} at offset {i}: {e}",
            )

    if updated != len(stampable_ids):
        logger.warning(
            f"[truelist.submit_batch] stamp count mismatch for {batch_id}: "
            f"expected={len(stampable_ids)}, stamped={updated}"
        )

    return {
        "submitted":   len(payload_data),
        "leads_marked":updated,
        "batch_id":    batch_id,
        "batch_state": body.get("batch_state"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Lead selection — priority queue HOT > WARM > COLD, pain_score DESC
# ─────────────────────────────────────────────────────────────────────────────

def select_uncleaned_leads(db, limit: int) -> list[dict]:
    """
    Pull up to `limit` uncleaned leads in priority order:
      1. segment_label='hot',  pain_score DESC
      2. segment_label='warm', pain_score DESC
      3. segment_label='cold', pain_score DESC
      4. unclassified (segment_label IS NULL), pain_score DESC

    Three back-to-back paginated selects keeps the SQL plan simple — the
    partial index idx_leads_email_uncleaned makes each one cheap.

    Predicate: is_active=True, email present, email_cleaned_at IS NULL,
    email_truelist_batch_id IS NULL. The batch-id check prevents the same
    lead from being submitted to two concurrent batches. We filter on
    batch_id (not email_status='pending_batch') because PostgREST's neq
    operator OMITS NULL-valued rows — and the 469K uncleaned leads have
    email_status=NULL, so a neq filter would silently exclude them all.
    See memory/project_postgrest_in_url_cap.md neighbor: NULL semantics in PostgREST.
    """
    # PostgREST/Supabase caps responses at ~1000 rows even when .limit(N) is
    # called with a larger N. To pull 25K rows we paginate via .range() in
    # 1000-row pages per segment.
    PAGE = 1000

    def _drain_segment(filter_fn) -> list[dict]:
        """Page through one segment until limit hit or no more rows."""
        collected: list[dict] = []
        offset = 0
        while True:
            need = limit - len(out) - len(collected)
            if need <= 0:
                return collected
            page_size = min(PAGE, need)
            q = (
                db.table("leads")
                .select("id, email, segment_label, pain_score")
                .eq("is_active", True)
                .not_.is_("email", "null")
                .neq("email", "")
                .is_("email_cleaned_at", "null")
                .is_("email_truelist_batch_id", "null")
            )
            q = filter_fn(q)
            q = q.order("pain_score", desc=True).range(offset, offset + page_size - 1)
            try:
                rows = q.execute().data or []
            except Exception as e:
                logger.error(f"[select_uncleaned_leads] page fetch failed: {e}")
                rows = []
            if not rows:
                return collected
            collected.extend(rows)
            if len(rows) < page_size:
                return collected
            offset += page_size

    out: list[dict] = []
    for label in ("hot", "warm", "cold"):
        if len(out) >= limit:
            break
        out.extend(_drain_segment(lambda q, lbl=label: q.eq("segment_label", lbl)))

    # Catch unclassified rows last (segment_label IS NULL)
    if len(out) < limit:
        out.extend(_drain_segment(lambda q: q.is_("segment_label", "null")))

    return out[:limit]


def select_revalidation_leads(db, limit: int, min_age_days: int = 90) -> list[dict]:
    """
    Pull up to `limit` unknown-status leads for re-validation.

    Predicate: is_active=True, email present, email_truelist_batch_id IS NULL
    (not currently in-flight), email_status='unknown', AND
    email_cleaned_at < now() - min_age_days.

    The min_age_days cooldown prevents infinite nightly loops: ingest_batch_result
    re-stamps email_cleaned_at on every result write (lines ~499-508), so a lead
    that stays 'unknown' after re-check won't re-qualify for ~90 more days.

    Order: email_cleaned_at ASC (oldest re-checks first — drain backlog front-to-back).
    Invalid leads (email_status='invalid') are intentionally excluded.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
    PAGE = 1000

    collected: list[dict] = []
    offset = 0
    while True:
        need = limit - len(collected)
        if need <= 0:
            break
        page_size = min(PAGE, need)
        try:
            rows = (
                db.table("leads")
                .select("id, email, segment_label, pain_score")
                .eq("is_active", True)
                .not_.is_("email", "null")
                .neq("email", "")
                .is_("email_truelist_batch_id", "null")
                .eq("email_status", "unknown")
                .lt("email_cleaned_at", cutoff)
                .order("email_cleaned_at", desc=False)
                .range(offset, offset + page_size - 1)
                .execute()
                .data or []
            )
        except Exception as e:
            logger.error(f"[select_revalidation_leads] page fetch failed: {e}")
            rows = []
        if not rows:
            break
        collected.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size

    return collected[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# Batch poller + result ingestion
# ─────────────────────────────────────────────────────────────────────────────

async def poll_batch(api_key: str, batch_id: str) -> dict:
    """GET /api/v1/batches/{id} — returns the raw Truelist response dict.

    Retry policy:
    - HTTP 429 (rate limit): up to 3 retries, backoff 1.5s / 3s / 6s.
    - HTTP 502/503/504 or connection/timeout errors (transient upstream blip):
      up to 3 retries, backoff 2s / 5s / 10s; raises TruelistTransientError
      after exhausting retries so the poll loop can skip this batch without
      failing the whole run.
    - HTTP 4xx (non-429): raises HTTPException immediately (real error, no retry).
    - Other 5xx (e.g. 500): raises HTTPException immediately.
    """
    transient_count = 0
    rate_limit_count = 0

    while True:
        try:
            async with httpx.AsyncClient(timeout=POLL_TIMEOUT_SEC) as client:
                r = await client.get(
                    f"{TRUELIST_BATCHES_URL}/{batch_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            transient_count += 1
            if transient_count <= 3:
                delay = _TRANSIENT_BACKOFF[transient_count - 1]
                logger.warning(
                    f"[poll_batch] {batch_id}: network error ({exc.__class__.__name__}), "
                    f"transient retry {transient_count}/3 in {delay}s"
                )
                await asyncio.sleep(delay)
                continue
            raise TruelistTransientError(
                f"TrueList poll for {batch_id} failed after {transient_count} attempts: {exc}"
            ) from exc

        if r.status_code == 200:
            return r.json()

        if r.status_code == 429:
            rate_limit_count += 1
            if rate_limit_count <= 3:
                delay = 1.5 * (2 ** (rate_limit_count - 1))
                logger.warning(
                    f"[poll_batch] {batch_id}: HTTP 429, rate-limit retry {rate_limit_count}/3 in {delay}s"
                )
                await asyncio.sleep(delay)
                continue
            raise HTTPException(
                status_code=502,
                detail=f"Truelist poll failed for {batch_id}: HTTP 429 after retries: {r.text[:300]}",
            )

        if r.status_code in _TRANSIENT_5XX:
            transient_count += 1
            if transient_count <= 3:
                delay = _TRANSIENT_BACKOFF[transient_count - 1]
                logger.warning(
                    f"[poll_batch] {batch_id}: HTTP {r.status_code} (transient upstream), "
                    f"transient retry {transient_count}/3 in {delay}s"
                )
                await asyncio.sleep(delay)
                continue
            raise TruelistTransientError(
                f"TrueList poll for {batch_id} returned HTTP {r.status_code} after "
                f"{transient_count} attempts: {r.text[:200]}"
            )

        raise HTTPException(
            status_code=502,
            detail=f"Truelist poll failed for {batch_id}: HTTP {r.status_code}: {r.text[:300]}",
        )


def _parse_annotated_csv(csv_text: str) -> dict[str, dict]:
    """
    Parse Truelist's annotated CSV into a {email_lower: {state, sub_state}} map.

    CSV header columns (per smoke test 2026-05-10):
      <unnamed leading column>, Email Address, Did you mean, Email State, Email Sub-State
    """
    out: dict[str, dict] = {}
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return out
    header = [h.strip() for h in rows[0]]
    # Find columns defensively (don't trust column order)
    try:
        em_idx = header.index("Email Address")
    except ValueError:
        em_idx = 1  # fallback: second column per observed format
    try:
        st_idx = header.index("Email State")
    except ValueError:
        st_idx = 3
    try:
        sub_idx = header.index("Email Sub-State")
    except ValueError:
        sub_idx = 4

    for r in rows[1:]:
        if len(r) <= max(em_idx, st_idx, sub_idx):
            continue
        em = (r[em_idx] or "").strip().lower()
        if not em:
            continue
        out[em] = {
            "state":     (r[st_idx] or "").strip(),
            "sub_state": (r[sub_idx] or "").strip() if sub_idx < len(r) else "",
        }
    return out


async def fetch_annotated_csv(api_key: str, url: str) -> str:
    """Download the annotated CSV. Authenticated via the same Bearer token."""
    async with httpx.AsyncClient(timeout=CSV_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Truelist CSV download failed: HTTP {r.status_code}: {r.text[:200]}",
        )
    return r.text


async def ingest_batch_result(
    db, api_key: str, batch_id: str, *, force_reingest: bool = False
) -> dict:
    """
    Idempotent ingestion of a completed batch's annotated CSV into the leads
    table. Matches leads by EMAIL from the CSV (lower(email) = ANY(chunk))
    rather than by email_truelist_batch_id — so it works even when the stamp
    step in submit_batch silently failed (root cause of processed=0 batches).

    Uses psycopg2 directly for the lower(email)=ANY() predicate since the
    Supabase client cannot perform column-transform filters.

    Idempotency: the WHERE clause includes `AND email_cleaned_at IS NULL` so
    re-running the same batch a second time touches 0 rows.

    force_reingest: skip the already_ingested early-exit so a previously
    zero-processed batch can be re-run after this fix.
    """
    print(f"[HB] ingest_batch_result start: batch={batch_id} force={force_reingest}", flush=True)

    # Look up batch row
    res = db.table("truelist_batches").select("*").eq("id", batch_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not in truelist_batches")
    batch = res.data[0]

    if batch.get("status") == "ingested" and not force_reingest:
        print(f"[HB] ingest_batch_result {batch_id}: already_ingested, skipping", flush=True)
        return {
            "batch_id":    batch_id,
            "status":      "already_ingested",
            "ingested_at": batch.get("ingested_at"),
        }

    csv_url = batch.get("annotated_csv_url")
    if not csv_url:
        # Re-poll Truelist to populate URLs
        print(f"[HB] ingest_batch_result {batch_id}: no csv_url, re-polling Truelist", flush=True)
        raw = await poll_batch(api_key, batch_id)
        csv_url = raw.get("annotated_csv_url")
        if not csv_url:
            raise HTTPException(
                status_code=409,
                detail=f"Batch {batch_id} has no annotated_csv_url yet (state={raw.get('batch_state')})",
            )
        # Persist what we got from re-poll
        db.table("truelist_batches").update({
            "annotated_csv_url":     csv_url,
            "safest_bet_csv_url":    raw.get("safest_bet_csv_url"),
            "highest_reach_csv_url": raw.get("highest_reach_csv_url"),
            "only_invalid_csv_url":  raw.get("only_invalid_csv_url"),
            "status":                raw.get("batch_state") or "completed",
            "completed_at":          _now_iso() if (raw.get("batch_state") == "completed") else batch.get("completed_at"),
        }).eq("id", batch_id).execute()

    print(f"[HB] ingest_batch_result {batch_id}: downloading CSV", flush=True)
    csv_text = await fetch_annotated_csv(api_key, csv_url)
    email_map = _parse_annotated_csv(csv_text)
    if not email_map:
        raise HTTPException(status_code=502, detail=f"Annotated CSV for {batch_id} parsed to 0 rows")
    print(f"[HB] ingest_batch_result {batch_id}: CSV has {len(email_map)} emails", flush=True)

    # Fetch all leads associated with this batch that haven't been cleaned yet.
    # Match by email_truelist_batch_id (stamped by lead ID during submit) rather
    # than by raw email equality. The old RPC path did WHERE lower(email)=ANY(emails)
    # which returned 0 rows because leads.email contains raw annotated strings like
    # "foo@bar.com (accept_all)" that never match the clean "foo@bar.com" in the CSV.
    PAGE = 1000
    all_leads: list[dict] = []
    offset = 0
    while True:
        try:
            rows = (
                db.table("leads")
                .select("id, email")
                .eq("email_truelist_batch_id", batch_id)
                .is_("email_cleaned_at", "null")
                .range(offset, offset + PAGE - 1)
                .execute()
                .data or []
            )
        except Exception as e:
            logger.error(f"[ingest_batch_result] lead fetch failed at offset {offset}: {e}")
            rows = []
        all_leads.extend(rows)
        if len(rows) < PAGE:
            break
        offset += PAGE

    print(f"[HB] ingest_batch_result {batch_id}: {len(all_leads)} leads to process", flush=True)

    # For each lead, extract clean addresses and resolve the best verdict from the CSV map.
    # Best-verdict precedence (by email_trust): valid(0) > unconfirmable(1) > catch_all(2) > invalid(3)
    # Using _truelist_trust() so risky (catch_all domains) ranks below genuinely unconfirmable.
    _VERDICT_ORDER = {"valid": 0, "unconfirmable": 1, "catch_all": 2, "invalid": 3}
    verdict_to_leads: dict[tuple, list[str]] = {}   # key: (email_status, email_valid, email_trust)
    no_match = 0

    for lead in all_leads:
        clean_emails = _extract_emails(lead.get("email") or "")
        best_sv: Optional[tuple[str, Optional[bool]]] = None
        best_trust: Optional[str] = None
        for em in clean_emails:
            entry = email_map.get(em)
            if entry is None:
                continue
            sv = parse_truelist_state(entry["state"])
            trust = _truelist_trust(entry["state"])
            if best_trust is None or _VERDICT_ORDER.get(trust, 99) < _VERDICT_ORDER.get(best_trust, 99):
                best_sv = sv
                best_trust = trust
        if best_trust is None:
            no_match += 1
            continue
        verdict_to_leads.setdefault((best_sv[0], best_sv[1], best_trust), []).append(lead["id"])

    print(
        f"[HB] ingest_batch_result {batch_id}: "
        f"matched={len(all_leads) - no_match} no_match={no_match} "
        f"verdict_groups={len(verdict_to_leads)}",
        flush=True,
    )

    counts: dict[str, int] = {"valid": 0, "catch_all": 0, "unconfirmable": 0, "invalid": 0, "error": 0}
    total_processed = 0
    now_iso = _now_iso()

    for (status, valid, trust), lead_ids in verdict_to_leads.items():
        for i in range(0, len(lead_ids), STAMP_CHUNK_SIZE):
            chunk = lead_ids[i:i + STAMP_CHUNK_SIZE]
            try:
                db.table("leads").update({
                    "email_status":     status,
                    "email_valid":      valid,
                    "email_trust":      trust,   # set at ingest time, bypass Tier-1 heuristic
                    "email_cleaned_at": now_iso,
                }).in_("id", chunk).execute()
                total_processed += len(chunk)
                counts[trust] = counts.get(trust, 0) + len(chunk)
                print(
                    f"[HB] ingest_batch_result {batch_id}: "
                    f"chunk offset={i} trust={trust} updated={len(chunk)}",
                    flush=True,
                )
            except Exception as e:
                logger.error(f"[ingest_batch_result] verdict update chunk failed (trust={trust}): {e}")
                counts["error"] = counts.get("error", 0) + len(chunk)

    print(
        f"[HB] ingest_batch_result {batch_id}: done "
        f"total_processed={total_processed} counts={counts}",
        flush=True,
    )

    # Fail-loud guard: TrueList reported valid emails but we wrote 0 valid leads.
    # This catches the contacts/leads table mismatch (e.g. clean_national_realtors batches
    # whose contacts live in the contacts table, not leads) and prevents silently marking
    # them ingested with 0 processed — which would block the correct contacts ingest path.
    ok_count = int(batch.get("ok_count") or 0)
    if batch.get("annotated_csv_url") and ok_count > 0 and counts.get("valid", 0) == 0:
        msg = (
            f"[ingest_batch_result] {batch_id}: annotated_csv_url present, "
            f"TrueList ok_count={ok_count}, but writeback produced 0 valid leads "
            f"(total_processed={total_processed}). Refusing to mark ingested — "
            f"likely a table mismatch (contacts vs leads pipeline). "
            f"Use the correct per-pipeline ingest path for recovery."
        )
        logger.error(msg)
        raise HTTPException(status_code=500, detail=msg)

    # Finalize batch row
    db.table("truelist_batches").update({
        "status":      "ingested",
        "ingested_at": _now_iso(),
        "notes":       (batch.get("notes") or "") + (
            f" | ingest(batch-match v3): processed={total_processed}, valid={counts['valid']}, "
            f"catch_all={counts.get('catch_all', 0)}, "
            f"unconfirmable={counts.get('unconfirmable', 0)}, "
            f"invalid={counts.get('invalid', 0)}, "
            f"errors={counts['error']}, no_match={no_match}"
        ),
    }).eq("id", batch_id).execute()

    return {
        "batch_id":        batch_id,
        "status":          "ingested",
        "leads_processed": total_processed,
        "counts":          counts,   # keyed by email_trust: valid/catch_all/unconfirmable/invalid/error
        "no_match":        no_match,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stamp recovery — re-stamp leads by email match when a batch has 0 tagged leads
# ─────────────────────────────────────────────────────────────────────────────

async def recover_batch_stamps(db, api_key: str, batch_id: str) -> dict:
    """
    Recovery path for batches where submit_batch's stamp step silently failed
    (root cause: PostgREST 414 from too-large .in_() URL with 500 UUIDs).

    Algorithm:
      1. Verify 0 leads currently carry this batch_id (safety guard).
      2. Download the annotated CSV from Truelist (or re-poll if URL missing).
      3. For each email in the CSV, find leads with that email that are
         still untagged (email_truelist_batch_id IS NULL, email_cleaned_at IS NULL).
      4. Stamp matching leads with batch_id + 'pending_batch'.
      5. Run ingest_batch_result to apply the CSV results.

    Safe to re-run: if leads are already stamped or already ingested the
    relevant checks inside ingest_batch_result exit cleanly.
    """
    # Step 1: check current tag count
    tagged_res = (
        db.table("leads")
        .select("id", count="exact")
        .eq("email_truelist_batch_id", batch_id)
        .execute()
    )
    already_tagged = tagged_res.count or 0
    logger.info(f"[recover_batch_stamps] batch={batch_id} already_tagged={already_tagged}")

    if already_tagged > 0:
        logger.info(f"[recover_batch_stamps] {already_tagged} leads already tagged — skipping re-stamp, proceeding to ingest")
    else:
        # Step 2: get CSV URL
        res = db.table("truelist_batches").select("*").eq("id", batch_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id} not in truelist_batches")
        batch_row = res.data[0]

        csv_url = batch_row.get("annotated_csv_url")
        if not csv_url:
            raw = await poll_batch(api_key, batch_id)
            csv_url = raw.get("annotated_csv_url")
            if not csv_url:
                raise HTTPException(
                    status_code=409,
                    detail=f"Batch {batch_id} has no annotated_csv_url (state={raw.get('batch_state')}) — cannot re-stamp",
                )
            db.table("truelist_batches").update({
                "annotated_csv_url":     csv_url,
                "safest_bet_csv_url":    raw.get("safest_bet_csv_url"),
                "highest_reach_csv_url": raw.get("highest_reach_csv_url"),
                "only_invalid_csv_url":  raw.get("only_invalid_csv_url"),
                "status":                raw.get("batch_state") or "completed",
            }).eq("id", batch_id).execute()

        # Step 3: parse CSV for email list
        csv_text = await fetch_annotated_csv(api_key, csv_url)
        email_map = _parse_annotated_csv(csv_text)
        if not email_map:
            raise HTTPException(status_code=502, detail=f"CSV for {batch_id} parsed to 0 emails")

        emails = list(email_map.keys())
        logger.info(f"[recover_batch_stamps] {len(emails)} emails in CSV — looking up untagged leads")

        # Step 4: find and stamp leads by email in chunks
        stamped_total = 0
        not_found = 0
        for i in range(0, len(emails), STAMP_CHUNK_SIZE):
            email_chunk = emails[i:i + STAMP_CHUNK_SIZE]
            try:
                rows = (
                    db.table("leads")
                    .select("id")
                    .in_("email", email_chunk)
                    .is_("email_truelist_batch_id", "null")
                    .is_("email_cleaned_at", "null")
                    .execute()
                    .data or []
                )
            except Exception as e:
                logger.error(f"[recover_batch_stamps] email lookup chunk failed: {e}")
                continue

            if not rows:
                not_found += len(email_chunk)
                continue

            lead_ids = [r["id"] for r in rows]
            for j in range(0, len(lead_ids), STAMP_CHUNK_SIZE):
                id_chunk = lead_ids[j:j + STAMP_CHUNK_SIZE]
                try:
                    db.table("leads").update({
                        "email_status":            "pending_batch",
                        "email_truelist_batch_id": batch_id,
                    }).in_("id", id_chunk).execute()
                    stamped_total += len(id_chunk)
                except Exception as e:
                    logger.error(f"[recover_batch_stamps] stamp chunk failed: {e}")

        logger.info(
            f"[recover_batch_stamps] batch={batch_id} stamped={stamped_total} not_found={not_found}"
        )

        if stamped_total == 0:
            logger.warning(
                f"[recover_batch_stamps] 0 leads stamped for {batch_id} — "
                "leads may have already been cleaned by a later batch"
            )
            # Mark the batch ingested with 0 so it won't be re-processed
            db.table("truelist_batches").update({
                "status":      "ingested",
                "ingested_at": _now_iso(),
                "notes":       (batch_row.get("notes") or "") + " | recover_stamps: 0 untagged leads found (already cleaned)",
            }).eq("id", batch_id).execute()
            return {"batch_id": batch_id, "status": "recovery_noop", "stamped": 0}

    # Step 5: ingest — force_reingest=True bypasses the "already_ingested" early-exit
    # so we can drain leads that were stamped but never matched due to the email-match bug.
    result = await ingest_batch_result(db, api_key, batch_id, force_reingest=True)
    stamped = already_tagged if already_tagged > 0 else stamped_total
    return {"batch_id": batch_id, "status": "recovered", "stamped": stamped, "ingest": result}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP API
# ─────────────────────────────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    batches: int = 1            # how many batches to submit in this call
    batch_size: Optional[int] = None  # override admin_settings if set
    submitted_by: str = "manual_submit"
    notes: Optional[str] = None


@router.post("/email-clean/submit")
async def submit_endpoint(body: SubmitRequest, db=Depends(get_db)):
    """
    Submit N batches of uncleaned leads to Truelist.
    Each batch holds up to `batch_size` emails (default from admin_settings
    `email_clean_batch_size_emails`, fallback 25000). Returns a list of
    submission results — one entry per batch.
    """
    if body.batches < 1 or body.batches > 50:
        raise HTTPException(status_code=400, detail="batches must be 1..50")

    api_key = _get_truelist_api_key(db)

    # Resolve batch_size from admin_settings if not overridden
    batch_size = body.batch_size
    if batch_size is None:
        try:
            res = db.table("admin_settings").select("value").eq("key", "email_clean_batch_size_emails").execute()
            batch_size = int((res.data or [{}])[0].get("value") or "25000")
        except Exception:
            batch_size = 25000
    batch_size = max(1, min(batch_size, 250_000))

    # Re-validation settings — configurable via admin_settings, safe defaults hardcoded.
    revalidation_enabled = True
    revalidation_min_age_days = 90
    try:
        rv_res = db.table("admin_settings").select("key, value").in_("key", [
            "email_revalidation_enabled",
            "email_revalidation_min_age_days",
        ]).execute()
        for row in (rv_res.data or []):
            if row["key"] == "email_revalidation_enabled":
                revalidation_enabled = str(row.get("value", "true")).lower() not in ("false", "0", "no")
            elif row["key"] == "email_revalidation_min_age_days":
                try:
                    revalidation_min_age_days = int(row["value"])
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        logger.warning(f"[submit_endpoint] Could not load revalidation settings: {e}")

    results = []
    for i in range(body.batches):
        # New (never-cleaned) leads get first priority, filling up to batch_size.
        new_leads = select_uncleaned_leads(db, batch_size)

        # Top up remaining capacity with re-validation of old 'unknown' leads.
        # This reallocates unused quota — it does NOT inflate total submission volume.
        revalidation_leads: list[dict] = []
        if revalidation_enabled and len(new_leads) < batch_size:
            remaining = batch_size - len(new_leads)
            revalidation_leads = select_revalidation_leads(
                db, remaining, revalidation_min_age_days
            )

        leads = new_leads + revalidation_leads

        if not leads:
            results.append({
                "index": i,
                "submitted": 0,
                "skipped": True,
                "reason": "no_uncleaned_leads",
                "new_leads": 0,
                "revalidation_leads": 0,
            })
            break
        try:
            r = await submit_batch(
                db, api_key, leads,
                submitted_by=body.submitted_by,
                notes=body.notes,
            )
            r["index"] = i
            r["new_leads"] = len(new_leads)
            r["revalidation_leads"] = len(revalidation_leads)
            results.append(r)
        except HTTPException as e:
            results.append({"index": i, "submitted": 0, "error": e.detail})
            break

    return {
        "status":                    "ok" if results else "no_op",
        "batches_attempted":         len(results),
        "batch_size":                batch_size,
        "revalidation_enabled":      revalidation_enabled,
        "revalidation_min_age_days": revalidation_min_age_days,
        "results":                   results,
    }


@router.post("/email-clean/poll/{batch_id}")
async def poll_endpoint(batch_id: str, ingest_if_done: bool = True, db=Depends(get_db)):
    """
    Poll one batch's status. If ingest_if_done=true (default) AND the batch
    has reached `completed`, also ingest the annotated CSV right away.
    """
    api_key = _get_truelist_api_key(db)
    raw = await poll_batch(api_key, batch_id)
    state = raw.get("batch_state")

    # Persist the latest counts/URLs (idempotent UPDATE)
    try:
        db.table("truelist_batches").update({
            "status":                    state,
            "annotated_csv_url":         raw.get("annotated_csv_url"),
            "safest_bet_csv_url":        raw.get("safest_bet_csv_url"),
            "highest_reach_csv_url":     raw.get("highest_reach_csv_url"),
            "only_invalid_csv_url":      raw.get("only_invalid_csv_url"),
            "ok_count":                  raw.get("ok_count"),
            "ok_for_all_count":          raw.get("ok_for_all_count"),
            "role_count":                raw.get("role_count"),
            "disposable_count":          raw.get("disposable_count"),
            "failed_syntax_check_count": raw.get("failed_syntax_check_count"),
            "failed_mx_check_count":     raw.get("failed_mx_check_count"),
            "failed_no_mailbox_count":   raw.get("failed_no_mailbox_count"),
            "completed_at":              _now_iso() if state == "completed" else None,
        }).eq("id", batch_id).execute()
    except Exception as e:
        logger.warning(f"[poll_endpoint] persist failed (non-fatal) for {batch_id}: {e}")

    if state == "completed" and ingest_if_done:
        ingest = await ingest_batch_result(db, api_key, batch_id)
        return {"batch_id": batch_id, "state": state, "raw": raw, "ingest": ingest}

    return {"batch_id": batch_id, "state": state, "raw": raw}


@router.post("/email-clean/recover-stamps/{batch_id}")
async def recover_stamps_endpoint(batch_id: str, db=Depends(get_db)):
    """
    Recovery endpoint for batches where stamp step silently failed (processed=0 bug).
    Re-stamps leads by email match from the Truelist annotated CSV, then re-ingests.
    Safe to call on a batch that was already ingested — returns early with no-op.
    """
    api_key = _get_truelist_api_key(db)
    result = await recover_batch_stamps(db, api_key, batch_id)
    return result


@router.post("/email-clean/recover-zero-batches")
async def recover_zero_batches_endpoint(
    hours_back: int = Query(default=48, ge=1, le=168),
    dry_run: bool = False,
    db=Depends(get_db),
):
    """
    Find recently-ingested batches whose notes show processed=0 and re-ingest
    them using the email-match path. Recovers leads lost to the stamp-timeout bug.

    hours_back: how far back to scan (default 48h, max 168h/7d).
    dry_run: list matching batches without re-ingesting.
    """
    api_key = _get_truelist_api_key(db)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()

    res = (
        db.table("truelist_batches")
        .select("id, notes, submitted_at, ingested_at, annotated_csv_url")
        .eq("status", "ingested")
        .gt("submitted_at", cutoff)
        .execute()
    )
    all_recent = res.data or []
    zero_batches = [r for r in all_recent if r.get("notes") and "processed=0" in r["notes"]]
    print(
        f"[HB] recover-zero-batches: scanned {len(all_recent)} batches in last {hours_back}h, "
        f"{len(zero_batches)} have processed=0",
        flush=True,
    )

    if dry_run:
        return {"dry_run": True, "hours_back": hours_back, "zero_batches": zero_batches}

    results = []
    for row in zero_batches:
        bid = row["id"]
        has_csv = bool(row.get("annotated_csv_url"))
        print(f"[HB] recover-zero-batches: re-ingesting {bid} (has_csv={has_csv})", flush=True)
        try:
            result = await ingest_batch_result(db, api_key, bid, force_reingest=True)
            recovered = result.get("leads_processed", 0)
            print(f"[HB] recover-zero-batches: {bid} → recovered={recovered}", flush=True)
            results.append({"batch_id": bid, "recovered": recovered, "result": result})
        except Exception as e:
            logger.error(f"[recover_zero_batches] {bid} failed: {e}")
            results.append({"batch_id": bid, "recovered": 0, "error": str(e)})

    total_recovered = sum(r.get("recovered", 0) for r in results)
    print(
        f"[HB] recover-zero-batches: complete — {len(results)} batches, total_recovered={total_recovered}",
        flush=True,
    )
    return {
        "status":          "complete",
        "batches_found":   len(zero_batches),
        "batches_tried":   len(results),
        "total_recovered": total_recovered,
        "results":         results,
    }


@router.get("/email-clean/batches")
async def list_batches(limit: int = Query(default=20, ge=1, le=200), db=Depends(get_db)):
    """List recent batches (newest first)."""
    res = (
        db.table("truelist_batches")
        .select("*")
        .order("submitted_at", desc=True)
        .limit(limit)
        .execute()
    )
    return {"count": len(res.data or []), "batches": res.data or []}


@router.get("/email-clean/batches/{batch_id}")
async def get_batch(batch_id: str, db=Depends(get_db)):
    """Get one batch row."""
    res = db.table("truelist_batches").select("*").eq("id", batch_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Batch not found")
    return res.data[0]


@router.get("/email-clean/status")
async def email_clean_status(db=Depends(get_db)):
    """
    Coverage stats. Extended in v2 to include pending_batch count and
    in-flight batch summary.
    """
    total_res = (
        db.table("leads")
        .select("id", count="exact")
        .not_.is_("email", "null")
        .neq("email", "")
        .execute()
    )
    total_with_email = total_res.count or 0

    cleaned_res = (
        db.table("leads")
        .select("id", count="exact")
        .not_.is_("email", "null")
        .neq("email", "")
        .not_.is_("email_cleaned_at", "null")
        .execute()
    )
    total_cleaned = cleaned_res.count or 0

    pending_res = (
        db.table("leads")
        .select("id", count="exact")
        .eq("email_status", "pending_batch")
        .execute()
    )
    pending_batch = pending_res.count or 0

    in_flight_res = (
        db.table("truelist_batches")
        .select("id, status, email_count, submitted_at")
        .in_("status", ["pending", "processing"])
        .order("submitted_at", desc=True)
        .execute()
    )
    in_flight = in_flight_res.data or []

    uncleaned = total_with_email - total_cleaned - pending_batch
    coverage_pct = round((total_cleaned / total_with_email * 100), 1) if total_with_email > 0 else 0.0

    return {
        "total_leads_with_email": total_with_email,
        "total_cleaned":          total_cleaned,
        "pending_batch":          pending_batch,
        "uncleaned":              max(0, uncleaned),
        "coverage_pct":           coverage_pct,
        "in_flight_batches":      in_flight,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Legacy single-lead path — retained for ad-hoc verify_inline use
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/email-clean/single/{lead_id}")
async def single_email_clean(lead_id: str, db=Depends(get_db)):
    """
    Validate a single lead's email via verify_inline (sync, 1-email path).
    Useful for ad-hoc testing; the bulk pipeline is the production path.
    """
    api_key = _get_truelist_api_key(db)

    res = db.table("leads").select("id, email").eq("id", lead_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Lead {lead_id} not found.")
    lead = res.data[0]
    email = (lead.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail=f"Lead {lead_id} has no email address.")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                TRUELIST_VERIFY_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                params={"email": email},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Truelist API error: {e}")

    try:
        email_obj = data["emails"][0]
    except (KeyError, IndexError, TypeError):
        raise HTTPException(status_code=502, detail=f"Unexpected Truelist response shape: {data}")

    raw_state = email_obj.get("email_state")
    status, valid = parse_truelist_state(raw_state)
    trust = _truelist_trust(raw_state)
    sub_state = email_obj.get("email_sub_state")

    db.table("leads").update({
        "email_status":     status,
        "email_valid":      valid,
        "email_trust":      trust,
        "email_sub_state":  sub_state,
        "email_cleaned_at": _now_iso(),
    }).eq("id", lead_id).execute()

    return {
        "lead_id":           lead_id,
        "email":             email,
        "email_status":      status,
        "email_valid":       valid,
        "email_trust":       trust,
        "email_state_raw":   raw_state,
        "email_sub_state":   sub_state,
        "did_you_mean":      email_obj.get("did_you_mean"),
        "campaign_eligible": is_campaign_eligible(status),
    }
