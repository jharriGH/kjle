"""
KJLE — Local Scraper webhook ingest (Phase 4 Layer 1, Slice 1C)
File: api/routes/local_scraper_ingest.py

Routes (PREFIX /kjle/v1 added by main.py):
  POST  /ingest/localscraper                 — HMAC X-Webhook-Signature; new run
  GET   /ingest/localscraper/status          — auth; aggregate ingest stats
  GET   /ingest/localscraper/runs            — auth; paginated run log
  POST  /ingest/localscraper/replay/{run_id} — auth; re-process a run

Phase 4 Layer 1 design notes:
- Per-run idempotency via UNIQUE(run_id) on local_scraper_ingest_log table.
  Replayed webhooks return {status:"already_processed"} without re-inserting.
- LENIENT row processing (Jim's 2026-05-24 decision):
    results_count   = raw len(payload.results)
    inserted_count  = successful upserts
    duplicate_count = phone/email-collision skips
    filtered_count  = normalize-fails + leads with no contact path
    error           = nullable; only on whole-batch failure
- HMAC: GitHub-style 'X-Webhook-Signature: sha256=<hex>' over raw body bytes,
  secret in LOCAL_SCRAPER_WEBHOOK_SECRET env var. Constant-time compare.
- Leads land with dnc_status='unchecked' — Phase 4 defers DNC to contact-time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from ..database import get_db
from ..lib.lead_filters import classify_lead_quality
from ..lib.phone_filters import classify_phone_quality
from ..lib.phone_utils import normalize_phone

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Auth ─────────────────────────────────────────────────────────────────────
API_SECRET_KEY = os.environ.get("API_SECRET_KEY")
LOCAL_SCRAPER_WEBHOOK_SECRET = (
    os.environ.get("LOCAL_SCRAPER_WEBHOOK_SECRET", "").strip()
)


def verify_api_key(x_api_key: str = Header(...)) -> None:
    if not API_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="server_misconfigured: API_SECRET_KEY env var unset",
        )
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _verify_webhook_signature(header_value: Optional[str],
                               body_bytes: bytes) -> bool:
    """
    Constant-time HMAC-SHA256 verification of the X-Webhook-Signature header.
    Header format: 'sha256=<hex digest>' (GitHub style).
    Fail-closed on missing secret, missing header, malformed prefix, or mismatch.
    """
    if not LOCAL_SCRAPER_WEBHOOK_SECRET:
        return False
    if not header_value:
        return False
    prefix = "sha256="
    if not header_value.startswith(prefix):
        return False
    provided_hex = header_value[len(prefix):].strip()
    if not provided_hex:
        return False
    expected_hex = hmac.new(
        LOCAL_SCRAPER_WEBHOOK_SECRET.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    try:
        return hmac.compare_digest(expected_hex, provided_hex)
    except Exception:
        return False


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_email(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).strip().lower()
    if not s or "@" not in s or s.startswith("@") or s.endswith("@"):
        return None
    return s


def _normalize_website(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).strip()
    return s or None


def _process_results(db, results: list, run_id: str, worker_id: str,
                     scraper_id: str, settings: dict) -> dict:
    """
    Iterate results[], normalize, dedup, upsert to leads.

    LENIENT mode: per-row failures bump filtered_count and continue.
    Whole-batch DB failures bubble up to the caller.

    Returns: {inserted, duplicates, filtered}
    """
    inserted = 0
    duplicates = 0
    filtered = 0

    source_metadata = {
        "worker_id": worker_id,
        "scraper_id": scraper_id,
        "run_id": run_id,
        "settings": settings,
    }

    for raw_lead in results:
        try:
            if not isinstance(raw_lead, dict):
                filtered += 1
                continue

            # Local Scraper emits capitalized field names (Phone/Email/
            # Website/Name/City/State/Zip). Normalize keys to lowercase so the
            # .get() lookups below resolve. Validation is unchanged — a real
            # contact path is still required.
            raw_lead = {
                (k.lower() if isinstance(k, str) else k): v
                for k, v in raw_lead.items()
            }

            phone_raw = raw_lead.get("phone")
            email_raw = raw_lead.get("email")
            website_raw = raw_lead.get("website")

            phone = normalize_phone(phone_raw) if phone_raw else None
            email = _normalize_email(email_raw)
            website = _normalize_website(website_raw)

            # Need at least one contact path
            if not phone and not email and not website:
                filtered += 1
                continue

            # Dedup: phone first, then email. Website does not dedup
            # (landing pages would suppress legitimately distinct leads).
            existing = None
            if phone:
                try:
                    r = (db.table("leads").select("id")
                         .eq("phone", phone).limit(1).execute())
                    existing = r.data[0] if r.data else None
                except Exception as e:
                    logger.warning(
                        "local_scraper_ingest: phone dedup read failed: %s", e
                    )
            if not existing and email:
                try:
                    r = (db.table("leads").select("id")
                         .eq("email", email).limit(1).execute())
                    existing = r.data[0] if r.data else None
                except Exception as e:
                    logger.warning(
                        "local_scraper_ingest: email dedup read failed: %s", e
                    )

            if existing:
                duplicates += 1
                continue

            state = raw_lead.get("state")
            state = state.upper() if isinstance(state, str) and state else None

            # Phase 4 Layer 3 Slice 3B — lead + phone quality classifiers.
            # Both are pure-function, no IO. Non-contactable leads are STILL
            # SAVED (with contactable=false) so research / analytics retain
            # the data — we don't hard-delete.
            quality = classify_lead_quality(raw_lead)
            phone_quality = (
                classify_phone_quality(phone) if phone else None
            )
            contactable = quality["contactable"] and (
                phone_quality["contactable"] if phone_quality else True
            )
            filter_reasons = list(quality["reasons"]) + (
                list(phone_quality["reasons"]) if phone_quality else []
            )

            payload = {
                "business_name": (raw_lead.get("business_name") or
                                  raw_lead.get("name") or "").strip() or None,
                "phone":         phone,
                "email":         email,
                "website":       website,
                "address":       (raw_lead.get("address") or "").strip() or None,
                "city":          (raw_lead.get("city") or "").strip() or None,
                "state":         state,
                "zip":           (raw_lead.get("zip") or "").strip() or None,
                "niche_slug":    (raw_lead.get("niche_slug") or
                                  raw_lead.get("category") or "").strip() or None,
                "source":        "local_scraper",
                "source_metadata": source_metadata,
                "dnc_status":    "unchecked",
                "is_active":     True,
                "enrichment_stage": 0,
                "contactable":   contactable,
                "filter_reasons": filter_reasons,
            }
            payload = {k: v for k, v in payload.items()
                       if v is not None and v != ""}
            # contactable / filter_reasons are intentionally preserved even
            # when False / [] — re-add after the falsy-strip pass.
            payload["contactable"]    = contactable
            payload["filter_reasons"] = filter_reasons

            try:
                db.table("leads").insert(payload).execute()
                inserted += 1
            except Exception as e:
                logger.warning(
                    "local_scraper_ingest: insert failed (lenient): %s", e
                )
                filtered += 1

        except Exception as e:
            logger.warning(
                "local_scraper_ingest: per-row failure (lenient): %s", e
            )
            filtered += 1

    return {
        "inserted": inserted,
        "duplicates": duplicates,
        "filtered": filtered,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /ingest/localscraper
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ingest/localscraper")
async def ingest_localscraper(request: Request):
    """
    Webhook endpoint for Local Scraper completion events.

    Authentication: HMAC-SHA256 over raw request body, header
    'X-Webhook-Signature: sha256=<hex>'. Secret in LOCAL_SCRAPER_WEBHOOK_SECRET.

    Expected payload (LENIENT — extra keys ignored):
        {
          "event":      "scrape_complete",
          "timestamp":  "...",
          "worker_id":  "z820",
          "scraper_id": "google_maps_v3",
          "run_id":     "uuid-or-string-unique-per-run",
          "settings":   { ... keyword, location, threads, etc. ... },
          "results": [
            {"name|business_name": "...", "phone": "...", "email": "...",
             "website": "...", "address": "...", "city": "...", "state": "...",
             "category|niche_slug": "...", ...},
            ...
          ]
        }

    Idempotency: replaying the same run_id returns the original processing
    result (no re-insertion).

    Response (200 once signature passes):
        {
          "run_id":          str,
          "status":          "processed" | "already_processed" | "error",
          "results_count":   int,
          "inserted_count":  int,
          "duplicate_count": int,
          "filtered_count":  int,
          "processing_ms":   int,
          "error":           null | "..."
        }

    HTTP 401 on signature failure.
    """
    body_bytes = await request.body()
    signature = request.headers.get("X-Webhook-Signature")

    if not _verify_webhook_signature(signature, body_bytes):
        logger.warning(
            "local_scraper_ingest: signature INVALID — sig_present=%s, "
            "secret_configured=%s, body_len=%d",
            bool(signature), bool(LOCAL_SCRAPER_WEBHOOK_SECRET), len(body_bytes),
        )
        raise HTTPException(status_code=401, detail="invalid_signature")

    # Parse payload
    try:
        body = (json.loads(body_bytes.decode("utf-8") or "{}")
                if body_bytes else {})
    except Exception as e:
        logger.error("local_scraper_ingest: payload not JSON: %s", e)
        return {
            "run_id":          None,
            "status":          "error",
            "results_count":   0,
            "inserted_count":  0,
            "duplicate_count": 0,
            "filtered_count":  0,
            "processing_ms":   0,
            "error":           f"payload_not_json: {e}",
        }

    if not isinstance(body, dict):
        return {
            "run_id":          None,
            "status":          "error",
            "results_count":   0,
            "inserted_count":  0,
            "duplicate_count": 0,
            "filtered_count":  0,
            "processing_ms":   0,
            "error":           "payload_not_object",
        }

    run_id = (body.get("run_id") or "").strip()
    worker_id = (body.get("worker_id") or "unknown").strip()
    scraper_id = (body.get("scraper_id") or "unknown").strip()
    settings = body.get("settings") or {}
    results = body.get("results")

    if not run_id:
        return {
            "run_id":          None,
            "status":          "error",
            "results_count":   0,
            "inserted_count":  0,
            "duplicate_count": 0,
            "filtered_count":  0,
            "processing_ms":   0,
            "error":           "missing_run_id",
        }

    if not isinstance(results, list):
        return {
            "run_id":          run_id,
            "status":          "error",
            "results_count":   0,
            "inserted_count":  0,
            "duplicate_count": 0,
            "filtered_count":  0,
            "processing_ms":   0,
            "error":           "missing_or_invalid_results_field",
        }

    results_count = len(results)
    db = get_db()

    # Idempotency: existing 'processed' run → return prior result
    try:
        existing = (db.table("local_scraper_ingest_log").select("*")
                    .eq("run_id", run_id).limit(1).execute())
        existing_row = existing.data[0] if existing.data else None
    except Exception as e:
        logger.warning(
            "local_scraper_ingest: idempotency check failed: %s — proceeding", e
        )
        existing_row = None

    if existing_row and existing_row.get("status") == "processed":
        return {
            "run_id":          run_id,
            "status":          "already_processed",
            "results_count":   existing_row.get("results_count", 0),
            "inserted_count":  existing_row.get("inserted_count", 0),
            "duplicate_count": existing_row.get("duplicate_count", 0),
            "filtered_count":  existing_row.get("filtered_count", 0),
            "processing_ms":   existing_row.get("processing_ms", 0),
            "error":           None,
        }

    # Insert ingest_log row with status='received' (or update existing
    # if a prior attempt errored out)
    log_row = {
        "worker_id":     worker_id,
        "scraper_id":    scraper_id,
        "run_id":        run_id,
        "raw_payload":   body,
        "results_count": results_count,
        "status":        "received",
    }
    try:
        if existing_row:
            db.table("local_scraper_ingest_log").update(
                log_row).eq("run_id", run_id).execute()
        else:
            db.table("local_scraper_ingest_log").insert(log_row).execute()
    except Exception as e:
        logger.error(
            "local_scraper_ingest: ingest_log write failed: %s", e
        )
        return {
            "run_id":          run_id,
            "status":          "error",
            "results_count":   results_count,
            "inserted_count":  0,
            "duplicate_count": 0,
            "filtered_count":  0,
            "processing_ms":   0,
            "error":           f"ingest_log_write_failed: {str(e)[:200]}",
        }

    # Process the results
    t_start = time.monotonic()
    try:
        stats = _process_results(db, results, run_id, worker_id,
                                  scraper_id, settings)
    except Exception as e:
        logger.error(
            "local_scraper_ingest: batch processing aborted: %s", e
        )
        duration_ms = int((time.monotonic() - t_start) * 1000)
        try:
            db.table("local_scraper_ingest_log").update({
                "status":        "error",
                "error":         f"batch_aborted: {type(e).__name__}: "
                                  f"{str(e)[:200]}",
                "processing_ms": duration_ms,
            }).eq("run_id", run_id).execute()
        except Exception as ee:
            logger.warning(
                "local_scraper_ingest: error-update of ingest_log failed: %s", ee
            )
        return {
            "run_id":          run_id,
            "status":          "error",
            "results_count":   results_count,
            "inserted_count":  0,
            "duplicate_count": 0,
            "filtered_count":  0,
            "processing_ms":   duration_ms,
            "error":           f"batch_aborted: {type(e).__name__}: "
                                f"{str(e)[:200]}",
        }

    duration_ms = int((time.monotonic() - t_start) * 1000)

    # Update the log row with final counts
    try:
        db.table("local_scraper_ingest_log").update({
            "status":          "processed",
            "inserted_count":  stats["inserted"],
            "duplicate_count": stats["duplicates"],
            "filtered_count":  stats["filtered"],
            "processing_ms":   duration_ms,
        }).eq("run_id", run_id).execute()
    except Exception as e:
        logger.warning(
            "local_scraper_ingest: final ingest_log update failed: %s", e
        )

    logger.info(
        "local_scraper_ingest: run_id=%s worker=%s scraper=%s "
        "results=%d inserted=%d dup=%d filtered=%d ms=%d",
        run_id, worker_id, scraper_id, results_count,
        stats["inserted"], stats["duplicates"], stats["filtered"], duration_ms,
    )

    return {
        "run_id":          run_id,
        "status":          "processed",
        "results_count":   results_count,
        "inserted_count":  stats["inserted"],
        "duplicate_count": stats["duplicates"],
        "filtered_count":  stats["filtered"],
        "processing_ms":   duration_ms,
        "error":           None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /ingest/localscraper/status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/ingest/localscraper/status")
async def ingest_status(x_api_key: str = Header(...)) -> dict:
    """
    Aggregate stats for Local Scraper ingest activity.

    Response:
        {
          "last_24h":  {runs, leads_inserted, duplicates, filtered, errors},
          "last_7d":   { ... same ... },
          "all_time":  { ... same ... },
          "active_workers": [{worker_id, last_seen, runs_24h}, ...],
          "active_scrapers": [{scraper_id, last_run, leads_24h}, ...]
        }
    """
    verify_api_key(x_api_key)
    db = get_db()

    def _window_stats(since_iso: Optional[str]) -> dict:
        out = {"runs": 0, "leads_inserted": 0, "duplicates": 0,
               "filtered": 0, "errors": 0}
        try:
            q = db.table("local_scraper_ingest_log").select(
                "status,inserted_count,duplicate_count,filtered_count"
            )
            if since_iso:
                q = q.gte("received_at", since_iso)
            rows = q.execute().data or []
            for row in rows:
                out["runs"] += 1
                out["leads_inserted"] += (row.get("inserted_count") or 0)
                out["duplicates"] += (row.get("duplicate_count") or 0)
                out["filtered"] += (row.get("filtered_count") or 0)
                if row.get("status") == "error":
                    out["errors"] += 1
        except Exception as e:
            logger.warning(
                "local_scraper_ingest.status: window aggregation failed: %s", e
            )
        return out

    now = datetime.now(timezone.utc)
    iso_24h = (now.replace(microsecond=0) -
               _timedelta_safe(hours=24)).isoformat()
    iso_7d = (now.replace(microsecond=0) -
              _timedelta_safe(days=7)).isoformat()

    last_24h = _window_stats(iso_24h)
    last_7d = _window_stats(iso_7d)
    all_time = _window_stats(None)

    # Active workers and scrapers (last 24h)
    active_workers: list[dict] = []
    active_scrapers: list[dict] = []
    try:
        rows = (db.table("local_scraper_ingest_log")
                .select("worker_id,scraper_id,received_at,inserted_count")
                .gte("received_at", iso_24h)
                .order("received_at", desc=True)
                .execute().data or [])

        worker_agg: dict[str, dict] = {}
        scraper_agg: dict[str, dict] = {}
        for row in rows:
            wid = row.get("worker_id") or "unknown"
            sid = row.get("scraper_id") or "unknown"
            recv = row.get("received_at")
            ins = row.get("inserted_count") or 0

            if wid not in worker_agg:
                worker_agg[wid] = {
                    "worker_id": wid, "last_seen": recv, "runs_24h": 1,
                }
            else:
                worker_agg[wid]["runs_24h"] += 1

            if sid not in scraper_agg:
                scraper_agg[sid] = {
                    "scraper_id": sid, "last_run": recv, "leads_24h": ins,
                }
            else:
                scraper_agg[sid]["leads_24h"] += ins

        active_workers = sorted(
            worker_agg.values(),
            key=lambda x: x.get("last_seen") or "",
            reverse=True,
        )
        active_scrapers = sorted(
            scraper_agg.values(),
            key=lambda x: x.get("last_run") or "",
            reverse=True,
        )
    except Exception as e:
        logger.warning(
            "local_scraper_ingest.status: workers/scrapers query failed: %s", e
        )

    return {
        "last_24h":        last_24h,
        "last_7d":         last_7d,
        "all_time":        all_time,
        "active_workers":  active_workers,
        "active_scrapers": active_scrapers,
    }


def _timedelta_safe(**kwargs):
    """Import-isolated timedelta accessor (keeps the route import list tight)."""
    from datetime import timedelta
    return timedelta(**kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# GET /ingest/localscraper/runs
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/ingest/localscraper/runs")
async def ingest_runs(
    limit: int = Query(50, ge=1, le=200),
    worker_id: Optional[str] = Query(None),
    scraper_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    since: Optional[str] = Query(None,
        description="ISO timestamp filter: only runs received after this time"),
    x_api_key: str = Header(...),
) -> dict:
    """
    Paginated list of recent ingest log rows.

    Returns the most-recent-first list, WITHOUT raw_payload (use /replay/{run_id}
    if you need the full original body — but really you should keep the payload
    on the source side).
    """
    verify_api_key(x_api_key)
    db = get_db()

    try:
        q = (db.table("local_scraper_ingest_log")
             .select("id,received_at,worker_id,scraper_id,run_id,"
                     "results_count,inserted_count,duplicate_count,"
                     "filtered_count,error,processing_ms,status")
             .order("received_at", desc=True)
             .limit(limit))
        if worker_id:
            q = q.eq("worker_id", worker_id)
        if scraper_id:
            q = q.eq("scraper_id", scraper_id)
        if status:
            q = q.eq("status", status)
        if since:
            q = q.gte("received_at", since)
        rows = q.execute().data or []
    except Exception as e:
        logger.error("local_scraper_ingest.runs: query failed: %s", e)
        raise HTTPException(
            status_code=500, detail=f"query_failed: {str(e)[:200]}"
        )

    return {
        "count":   len(rows),
        "limit":   limit,
        "filters": {
            "worker_id":  worker_id,
            "scraper_id": scraper_id,
            "status":     status,
            "since":      since,
        },
        "runs": rows,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /ingest/localscraper/replay/{run_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ingest/localscraper/replay/{run_id}")
async def ingest_replay(run_id: str, x_api_key: str = Header(...)) -> dict:
    """
    Re-process an existing ingest_log entry from its raw_payload.

    Useful when an earlier run errored out, or when filter logic has
    changed and you want to re-apply it to an old payload.

    Skips signature check (admin-only operation, authenticated via x-api-key).
    Updates the existing log row with status='replayed'.
    """
    verify_api_key(x_api_key)
    db = get_db()

    try:
        r = (db.table("local_scraper_ingest_log").select("*")
             .eq("run_id", run_id).limit(1).execute())
        row = r.data[0] if r.data else None
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"ingest_log_read_failed: {str(e)[:200]}"
        )

    if not row:
        raise HTTPException(
            status_code=404, detail=f"run_id_not_found: {run_id}"
        )

    raw_payload = row.get("raw_payload") or {}
    if not isinstance(raw_payload, dict):
        raise HTTPException(
            status_code=400,
            detail="raw_payload corrupt — cannot replay",
        )

    results = raw_payload.get("results")
    if not isinstance(results, list):
        raise HTTPException(
            status_code=400,
            detail="raw_payload.results missing or not a list",
        )

    worker_id = row.get("worker_id") or "unknown"
    scraper_id = row.get("scraper_id") or "unknown"
    settings = raw_payload.get("settings") or {}

    t_start = time.monotonic()
    try:
        stats = _process_results(db, results, run_id, worker_id,
                                  scraper_id, settings)
    except Exception as e:
        logger.error("local_scraper_ingest.replay: aborted: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"replay_aborted: {type(e).__name__}: {str(e)[:200]}",
        )

    duration_ms = int((time.monotonic() - t_start) * 1000)

    try:
        db.table("local_scraper_ingest_log").update({
            "status":          "replayed",
            "inserted_count":  stats["inserted"],
            "duplicate_count": stats["duplicates"],
            "filtered_count":  stats["filtered"],
            "processing_ms":   duration_ms,
            "error":           None,
        }).eq("run_id", run_id).execute()
    except Exception as e:
        logger.warning(
            "local_scraper_ingest.replay: log update failed: %s", e
        )

    return {
        "run_id":          run_id,
        "status":          "replayed",
        "results_count":   len(results),
        "inserted_count":  stats["inserted"],
        "duplicate_count": stats["duplicates"],
        "filtered_count":  stats["filtered"],
        "processing_ms":   duration_ms,
        "error":           None,
    }
