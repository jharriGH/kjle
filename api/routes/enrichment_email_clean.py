"""
KJLE — Email Cleaning via Truelist Bulk Batches API
Route prefix handled in main.py: /kjle/v1/enrichment

V2 architecture (Session 2B, 2026-05-10) — bulk POST /api/v1/batches.

Replaces the v1 verify_inline pipeline which was rate-limited to ~1 RPS
and capped at 2,500 emails/night. V2 submits 25K-email batches to
Truelist and ingests the annotated CSV per batch on completion. ~19
batches clears the 469K backlog.

Truelist API (confirmed via smoke test 2026-05-10):
  Submit: POST https://api.truelist.io/api/v1/batches
          body: {"data": [["email1"], ["email2"], ...]}
          response: {"id": <uuid>, "batch_state": "pending", "email_count": N, ...}
  Poll:   GET  https://api.truelist.io/api/v1/batches/{uuid}
          response: includes batch_state and (on completion) 4 CSV URLs
          states observed: pending → processing → completed
  CSV:    GET annotated_csv_url
          columns: <leading-blank>, Email Address, Did you mean, Email State, Email Sub-State
          values:  Email State ∈ {ok, invalid, risky, unknown}
  Auth:   Authorization: Bearer <TRUELIST_API_KEY>

Mapping from Truelist `Email State` → KJLE columns:
  ok       → email_status='valid',   email_valid=True
  invalid  → email_status='invalid', email_valid=False
  risky    → email_status='unknown', email_valid=None
  unknown  → email_status='unknown', email_valid=None

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
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db
from ..config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

TRUELIST_BATCHES_URL  = "https://api.truelist.io/api/v1/batches"
TRUELIST_VERIFY_URL   = "https://api.truelist.io/api/v1/verify_inline"  # retained for single endpoint

INGEST_CHUNK_SIZE     = 500   # lead-id chunks for CSV ingestion UPDATE
                              # (well under PostgREST URL-length cap — see
                              # memory/project_postgrest_in_url_cap.md)
SUBMIT_TIMEOUT_SEC    = 60.0
POLL_TIMEOUT_SEC      = 30.0
CSV_DOWNLOAD_TIMEOUT  = 300.0


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
      ("valid",   True)   when Truelist says ok
      ("invalid", False)  when Truelist says invalid
      ("unknown", None)   when Truelist says risky or unknown
      ("unknown", None)   when Truelist returns anything else (defensive default)
    """
    if not raw_state:
        return ("unknown", None)
    s = str(raw_state).strip().lower()
    # Strip `email_` prefix if present (verify_inline inconsistency)
    if s.startswith("email_"):
        s = s[len("email_"):]
    if s == "ok":
        return ("valid", True)
    if s == "invalid":
        return ("invalid", False)
    if s in ("risky", "unknown"):
        return ("unknown", None)
    return ("unknown", None)


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

    # Deduplicate emails within this batch — Truelist will dedupe anyway, but
    # we want a stable email→[lead_id,…] map for ingestion.
    seen: set[str] = set()
    payload_data: list[list[str]] = []
    for lead in leads:
        em = (lead.get("email") or "").strip()
        if not em or em.lower() in seen:
            continue
        seen.add(em.lower())
        payload_data.append([em])

    if not payload_data:
        return {"submitted": 0, "skipped": True, "reason": "no_usable_emails"}

    async with httpx.AsyncClient(timeout=SUBMIT_TIMEOUT_SEC) as client:
        r = await client.post(
            TRUELIST_BATCHES_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"data": payload_data},
        )
    if r.status_code != 200 and r.status_code != 201:
        body_excerpt = r.text[:500]
        logger.error(f"[truelist.submit_batch] HTTP {r.status_code}: {body_excerpt}")
        raise HTTPException(
            status_code=502,
            detail=f"Truelist submit failed: HTTP {r.status_code}: {body_excerpt}",
        )

    body = r.json()
    batch_id = body.get("id")
    if not batch_id:
        logger.error(f"[truelist.submit_batch] response missing id: {body}")
        raise HTTPException(status_code=502, detail="Truelist response missing batch id")

    # Insert truelist_batches row
    try:
        db.table("truelist_batches").insert({
            "id":           batch_id,
            "email_count":  body.get("email_count") or len(payload_data),
            "status":       body.get("batch_state") or "pending",
            "submitted_at": _now_iso(),
            "submitted_by": submitted_by,
            "notes":        notes,
        }).execute()
    except Exception as e:
        # If audit insert fails, the batch is still live at Truelist — bail
        # before marking leads pending_batch so we don't lose track of them.
        logger.error(f"[truelist.submit_batch] audit insert failed for {batch_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to record batch {batch_id}: {e}")

    # Mark leads pending_batch in chunks of INGEST_CHUNK_SIZE (URL-length cap)
    ids = [l["id"] for l in leads]
    updated = 0
    for i in range(0, len(ids), INGEST_CHUNK_SIZE):
        chunk = ids[i:i + INGEST_CHUNK_SIZE]
        try:
            db.table("leads").update({
                "email_status":            "pending_batch",
                "email_truelist_batch_id": batch_id,
            }).in_("id", chunk).execute()
            updated += len(chunk)
        except Exception as e:
            logger.error(f"[truelist.submit_batch] lead mark-pending chunk failed: {e}")
            # Continue — partial marks are recoverable; CSV ingest scopes by batch_id

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

    Retries on HTTP 429 (Truelist rate limit) with exponential backoff so that
    polling several in-flight batches in one cycle does not trip the limit and
    fail the whole run. Non-429 errors raise immediately.
    """
    last_text = ""
    for attempt in range(4):  # backoff between tries: 1.5s, 3s, 6s
        async with httpx.AsyncClient(timeout=POLL_TIMEOUT_SEC) as client:
            r = await client.get(
                f"{TRUELIST_BATCHES_URL}/{batch_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429 and attempt < 3:
            await asyncio.sleep(1.5 * (2 ** attempt))
            last_text = r.text[:300]
            continue
        raise HTTPException(
            status_code=502,
            detail=f"Truelist poll failed for {batch_id}: HTTP {r.status_code}: {r.text[:300]}",
        )
    raise HTTPException(
        status_code=502,
        detail=f"Truelist poll failed for {batch_id}: HTTP 429 after retries: {last_text}",
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


async def ingest_batch_result(db, api_key: str, batch_id: str) -> dict:
    """
    Idempotent ingestion of a completed batch's annotated CSV into the leads
    table. Page through leads where email_truelist_batch_id=batch_id (cursor
    by id), look up each email's classification, UPDATE by id chunks.

    Safe to re-run: if a batch row is already status='ingested', returns the
    cached summary instead of re-downloading.
    """
    # Look up batch row
    res = db.table("truelist_batches").select("*").eq("id", batch_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not in truelist_batches")
    batch = res.data[0]

    if batch.get("status") == "ingested":
        return {
            "batch_id":  batch_id,
            "status":    "already_ingested",
            "ingested_at": batch.get("ingested_at"),
        }

    csv_url = batch.get("annotated_csv_url")
    if not csv_url:
        # Re-poll Truelist to populate URLs
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

    csv_text = await fetch_annotated_csv(api_key, csv_url)
    email_map = _parse_annotated_csv(csv_text)
    if not email_map:
        raise HTTPException(status_code=502, detail=f"Annotated CSV for {batch_id} parsed to 0 rows")

    # Cursor-paginate the leads scoped to this batch_id
    counts = {"valid": 0, "invalid": 0, "unknown": 0, "error": 0, "no_csv_match": 0}
    sub_state_seen: dict[str, int] = {}
    last_id: Optional[str] = None
    total_processed = 0
    now_iso = _now_iso()

    while True:
        q = (
            db.table("leads")
            .select("id, email")
            .eq("email_truelist_batch_id", batch_id)
            .order("id", desc=False)
            .limit(INGEST_CHUNK_SIZE)
        )
        if last_id is not None:
            q = q.gt("id", last_id)
        try:
            rows = q.execute().data or []
        except Exception as e:
            logger.error(f"[ingest_batch_result] lead-page fetch failed: {e}")
            raise

        if not rows:
            break

        # Bucket by target email_status for grouped UPDATE-by-id chunks
        groups: dict[tuple[str, Optional[bool], str], list[str]] = {}
        for r_lead in rows:
            em = (r_lead.get("email") or "").strip().lower()
            mapped = email_map.get(em)
            if not mapped:
                counts["no_csv_match"] += 1
                # Leave at pending_batch — recovery will pick it up next poll/run
                continue
            status, valid = parse_truelist_state(mapped["state"])
            sub = mapped.get("sub_state", "") or ""
            sub_state_seen[sub] = sub_state_seen.get(sub, 0) + 1
            counts[status] = counts.get(status, 0) + 1
            key = (status, valid, sub)
            groups.setdefault(key, []).append(r_lead["id"])

        for (status, valid, sub), id_list in groups.items():
            for i in range(0, len(id_list), INGEST_CHUNK_SIZE):
                chunk = id_list[i:i + INGEST_CHUNK_SIZE]
                try:
                    db.table("leads").update({
                        "email_status":     status,
                        "email_valid":      valid,
                        "email_sub_state":  sub or None,
                        "email_cleaned_at": now_iso,
                    }).in_("id", chunk).execute()
                except Exception as e:
                    logger.error(f"[ingest_batch_result] update chunk failed ({status}): {e}")
                    counts["error"] = counts.get("error", 0) + len(chunk)

        total_processed += len(rows)
        last_id = rows[-1]["id"]
        if len(rows) < INGEST_CHUNK_SIZE:
            break

    # Finalize batch row
    db.table("truelist_batches").update({
        "status":      "ingested",
        "ingested_at": _now_iso(),
        "notes":       (batch.get("notes") or "") + (
            f" | ingest: processed={total_processed}, valid={counts['valid']}, "
            f"invalid={counts['invalid']}, unknown={counts['unknown']}, "
            f"no_csv_match={counts['no_csv_match']}, errors={counts['error']}"
        ),
    }).eq("id", batch_id).execute()

    return {
        "batch_id":         batch_id,
        "status":           "ingested",
        "leads_processed":  total_processed,
        "counts":           counts,
        "sub_states_top":   sorted(sub_state_seen.items(), key=lambda x: -x[1])[:10],
    }


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
    sub_state = email_obj.get("email_sub_state")

    db.table("leads").update({
        "email_status":     status,
        "email_valid":      valid,
        "email_sub_state":  sub_state,
        "email_cleaned_at": _now_iso(),
    }).eq("id", lead_id).execute()

    return {
        "lead_id":           lead_id,
        "email":             email,
        "email_status":      status,
        "email_valid":       valid,
        "email_state_raw":   raw_state,
        "email_sub_state":   sub_state,
        "did_you_mean":      email_obj.get("did_you_mean"),
        "campaign_eligible": is_campaign_eligible(status),
    }
