"""
KJLE — DNC endpoints
File: api/routes/dnc.py

Routes (PREFIX /kjle/v1 added by main.py):
  GET    /dnc/check/{phone}          — public, called by AVA / TH / etc.
  GET    /dnc/check                  — public, query-param fallback (?phone=+15...)
  POST   /dnc/add                    — auth, manual suppression
  POST   /dnc/scrub-batch            — auth, batch lookup (≤100, sequential)
  GET    /dnc/stats                  — auth, observability rollup

Lookup pipeline (executed in /check):
  1. Normalize phone → E.164 (400 on invalid; no audit row)
  2. Internal suppression hit  → free, instant
  3. Cache hit (TTL valid)     → free
  4. Budget guard (cost_guard) → fail-closed if cap exceeded
  5. Provider call (Searchbug) → fail-closed on any error
  6. UPSERT cache, log cost, log audit, persist Searchbug balance to
     admin_settings.searchbug_balance_last
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db
from ..lib import cost_guard
from ..lib.dnc_provider import DNCResult, get_active_provider
from ..lib.phone_utils import normalize_phone

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Auth ─────────────────────────────────────────────────────────────────────
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Constants ────────────────────────────────────────────────────────────────
SEARCHBUG_COST_PER_LOOKUP_USD = 0.0214
SCRUB_BATCH_MAX               = 100


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class AddSuppressionRequest(BaseModel):
    phone:    str
    reason:   str
    source:   str
    notes:    Optional[str]  = None
    metadata: Optional[dict] = None


class ScrubBatchRequest(BaseModel):
    phones: list[str]
    source: str


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_admin_setting(key: str, default: str) -> str:
    try:
        db = get_db()
        res = db.table("admin_settings").select("value").eq("key", key).execute()
        if res.data and res.data[0].get("value") is not None:
            return str(res.data[0]["value"])
    except Exception as e:
        logger.warning(f"dnc: admin_settings read failed for {key}: {e}")
    return default


def _upsert_admin_setting(key: str, value: str) -> None:
    try:
        db = get_db()
        db.table("admin_settings").upsert(
            {"key": key, "value": value, "updated_at": _now_iso()},
            on_conflict="key",
        ).execute()
    except Exception as e:
        logger.warning(f"dnc: admin_settings upsert failed for {key}: {e}")


def _audit(
    *,
    phone: Optional[str],
    source: str,
    result: str,
    is_dnc: Optional[bool],
    cost_usd: float = 0.0,
    requesting_lead_id: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    try:
        db = get_db()
        db.table("dnc_audit_log").insert({
            "phone":              phone,
            "source":             source,
            "result":             result,
            "is_dnc":             is_dnc,
            "cost_usd":           cost_usd,
            "requesting_lead_id": requesting_lead_id,
            "error":              error,
            "metadata":           metadata or {},
            "occurred_at":        _now_iso(),
        }).execute()
    except Exception as e:
        # Auditing must never break the request path.
        logger.error(f"dnc: audit insert failed: {e}")


def _serialize_response(
    *,
    result_source: str,
    phone_normalized: str,
    is_dnc: Optional[bool],
    reason: str = "",
    tcpa: bool = False,
    line_type: str = "unknown",
    carrier: str = "",
    cost_usd: float = 0.0,
    error: Optional[str] = None,
) -> dict:
    return {
        "is_dnc":            is_dnc,
        "reason":            reason,
        "tcpa_litigator":    tcpa,
        "line_type":         line_type,
        "carrier":           carrier,
        "cost_usd":          cost_usd,
        "result_source":     result_source,
        "phone_normalized":  phone_normalized,
        "error":             error,
    }


def _persist_searchbug_balance(raw_response: dict) -> None:
    """Pull Data.STATS.BALANCE off a Searchbug response and save it for ops visibility."""
    try:
        balance = (
            (raw_response or {})
            .get("Data", {})
            .get("STATS", {})
            .get("BALANCE")
        )
        if balance is not None and str(balance).strip():
            _upsert_admin_setting("searchbug_balance_last", str(balance).strip())
    except Exception as e:
        logger.warning(f"dnc: searchbug balance persist failed: {e}")


async def _perform_check(
    raw_phone: str,
    source: str,
    lead_id: Optional[str],
) -> dict:
    """
    Shared lookup pipeline used by both /dnc/check/{phone} and /dnc/check.
    Returns a serializable dict. Raises HTTPException(400) only on invalid
    phone format (the one case where we don't write an audit row).
    """
    phone = normalize_phone(raw_phone)
    if not phone:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_phone_format: could not normalize {raw_phone!r} to US E.164",
        )

    db = get_db()
    src = source or "unknown"

    # 1. Internal suppression list — free, fastest path
    try:
        sup = db.table("dnc_suppressions").select("phone, reason").eq("phone", phone).execute()
        if sup.data:
            row = sup.data[0]
            _audit(
                phone=phone, source=src, result="internal_suppression",
                is_dnc=True, requesting_lead_id=lead_id,
                metadata={"suppression_reason": row.get("reason")},
            )
            return _serialize_response(
                result_source="internal_suppression",
                phone_normalized=phone, is_dnc=True,
                reason=f"internal_suppression:{row.get('reason') or 'unknown'}",
            )
    except Exception as e:
        logger.warning(f"dnc: suppressions read failed for {phone}: {e}")
        # Fall through — better to fail the check via cache/provider than 500 here.

    # 2. Cache hit (non-expired) — free
    try:
        now_iso = _now_iso()
        c = (
            db.table("dnc_cache")
            .select("*")
            .eq("phone", phone)
            .gt("expires_at", now_iso)
            .execute()
        )
        if c.data:
            row = c.data[0]
            _audit(
                phone=phone, source=src, result="cache_hit",
                is_dnc=row.get("is_dnc"), requesting_lead_id=lead_id,
            )
            return _serialize_response(
                result_source="cache_hit",
                phone_normalized=phone,
                is_dnc=row.get("is_dnc"),
                reason=row.get("dnc_reason") or "",
                tcpa=bool(row.get("tcpa_litigator")),
                line_type=row.get("line_type") or "unknown",
                carrier=row.get("carrier") or "",
            )
    except Exception as e:
        logger.warning(f"dnc: cache read failed for {phone}: {e}")
        # Fall through to provider.

    # 3. Budget guard — fail-closed if cap exceeded
    try:
        budget_ok = await cost_guard.check_budget(
            service="searchbug",
            estimated_cost_usd=SEARCHBUG_COST_PER_LOOKUP_USD,
            job_name="dnc_check",
            leads_affected=1,
        )
    except Exception as e:
        logger.error(f"dnc: budget_guard call failed: {e}")
        budget_ok = False

    if not budget_ok:
        err = "budget_cap_exceeded"
        _audit(
            phone=phone, source=src, result="error",
            is_dnc=True, requesting_lead_id=lead_id, error=err,
        )
        return _serialize_response(
            result_source="error",
            phone_normalized=phone,
            is_dnc=True,                # fail-closed
            reason=err,
            error=err,
        )

    # 4. Provider call
    provider = get_active_provider()
    try:
        result: DNCResult = await provider.check_phone(phone)
    except Exception as e:
        err = f"provider_exception: {type(e).__name__}: {e}"
        logger.error(f"dnc: provider raised: {err}")
        _audit(
            phone=phone, source=src, result="error",
            is_dnc=True, requesting_lead_id=lead_id, error=err,
        )
        return _serialize_response(
            result_source="error",
            phone_normalized=phone,
            is_dnc=True,
            reason="provider_error",
            error=err,
        )

    if result.error:
        # Provider returned a structured failure — don't bill, don't cache, fail-closed.
        _audit(
            phone=phone, source=src, result="error",
            is_dnc=True, requesting_lead_id=lead_id, error=result.error,
            metadata={"provider": result.provider},
        )
        return _serialize_response(
            result_source="error",
            phone_normalized=phone,
            is_dnc=True,
            reason=result.reason or "provider_error",
            error=result.error,
        )

    # 5. Successful fresh lookup — cache, bill, audit, persist balance
    ttl_days = int(_get_admin_setting("dnc_cache_ttl_days", "14") or "14")
    expires_at = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat()

    try:
        db.table("dnc_cache").upsert({
            "phone":           phone,
            "is_dnc":          result.is_dnc,
            "dnc_reason":      result.reason or "",
            "tcpa_litigator":  result.tcpa_litigator,
            "line_type":       result.line_type or "unknown",
            "carrier":         result.carrier or "",
            "fetched_at":      _now_iso(),
            "expires_at":      expires_at,
            "raw_response":    result.raw_response or {},
            "source_provider": result.provider or "searchbug",
        }, on_conflict="phone").execute()
    except Exception as e:
        logger.error(f"dnc: cache upsert failed for {phone}: {e}")

    try:
        await cost_guard.log_cost(
            stage="dnc",
            service="searchbug",
            cost_usd=SEARCHBUG_COST_PER_LOOKUP_USD,
            lead_id=lead_id,
            items_fetched=1,
            job_run_id="dnc_check",
            metadata={"phone": phone, "source": src},
        )
    except Exception as e:
        logger.error(f"dnc: cost_guard.log_cost failed: {e}")

    _persist_searchbug_balance(result.raw_response)

    _audit(
        phone=phone, source=src, result="fresh_lookup",
        is_dnc=result.is_dnc, cost_usd=SEARCHBUG_COST_PER_LOOKUP_USD,
        requesting_lead_id=lead_id,
        metadata={"provider": result.provider, "line_type": result.line_type},
    )

    return _serialize_response(
        result_source="fresh_lookup",
        phone_normalized=phone,
        is_dnc=result.is_dnc,
        reason=result.reason,
        tcpa=result.tcpa_litigator,
        line_type=result.line_type,
        carrier=result.carrier,
        cost_usd=SEARCHBUG_COST_PER_LOOKUP_USD,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /dnc/check  (query-param fallback — must precede /{phone} for clarity)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dnc/check")
async def dnc_check_query(
    phone:   str           = Query(..., description="US phone in any format"),
    source:  str           = Query("unknown"),
    lead_id: Optional[str] = Query(None),
):
    return await _perform_check(phone, source, lead_id)


# ─────────────────────────────────────────────────────────────────────────────
# GET /dnc/check/{phone}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dnc/check/{phone}")
async def dnc_check_path(
    phone:   str,
    source:  str           = Query("unknown"),
    lead_id: Optional[str] = Query(None),
):
    return await _perform_check(phone, source, lead_id)


# ─────────────────────────────────────────────────────────────────────────────
# POST /dnc/add
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/dnc/add")
async def dnc_add(body: AddSuppressionRequest, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)

    phone = normalize_phone(body.phone)
    if not phone:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_phone_format: could not normalize {body.phone!r} to US E.164",
        )

    db = get_db()

    payload = {
        "phone":         phone,
        "reason":        body.reason,
        "source":        body.source,
        "suppressed_at": _now_iso(),
        "notes":         body.notes,
        "metadata":      body.metadata or {},
    }

    try:
        db.table("dnc_suppressions").upsert(payload, on_conflict="phone").execute()
    except Exception as e:
        logger.error(f"dnc: suppression upsert failed for {phone}: {e}")
        raise HTTPException(status_code=500, detail=f"suppression_write_failed: {e}")

    # Invalidate any cache entry — set expires_at to past so subsequent /check
    # will re-evaluate via suppression hit.
    try:
        past = "2000-01-01T00:00:00+00:00"
        db.table("dnc_cache").update({"expires_at": past}).eq("phone", phone).execute()
    except Exception as e:
        logger.warning(f"dnc: cache invalidate failed for {phone} (non-fatal): {e}")

    _audit(
        phone=phone, source=body.source, result="internal_suppression",
        is_dnc=True,
        metadata={"action": "added", "reason": body.reason, "notes": body.notes},
    )

    return {
        "status":           "success",
        "phone_normalized": phone,
        "reason":           body.reason,
        "source":           body.source,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /dnc/scrub-batch
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/dnc/scrub-batch")
async def dnc_scrub_batch(body: ScrubBatchRequest, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)

    if not body.phones:
        raise HTTPException(status_code=400, detail="phones list is empty")
    if len(body.phones) > SCRUB_BATCH_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"batch too large: {len(body.phones)} > {SCRUB_BATCH_MAX}",
        )

    src = body.source or "scrub_batch"
    results = []

    for raw in body.phones:
        try:
            res = await _perform_check(raw, src, None)
            results.append(res)
        except HTTPException as he:
            # Most likely invalid phone format — surface but keep going.
            results.append({
                "is_dnc":            None,
                "reason":            "",
                "tcpa_litigator":    False,
                "line_type":         "unknown",
                "carrier":           "",
                "cost_usd":          0.0,
                "result_source":     "error",
                "phone_normalized":  None,
                "error":             str(he.detail),
                "input":             raw,
            })

    summary = {
        "total":        len(results),
        "fresh":        sum(1 for r in results if r.get("result_source") == "fresh_lookup"),
        "cache_hits":   sum(1 for r in results if r.get("result_source") == "cache_hit"),
        "suppressions": sum(1 for r in results if r.get("result_source") == "internal_suppression"),
        "errors":       sum(1 for r in results if r.get("result_source") == "error"),
        "cost_usd":     round(sum(float(r.get("cost_usd") or 0) for r in results), 5),
    }

    return {"status": "success", "summary": summary, "results": results}


# ─────────────────────────────────────────────────────────────────────────────
# GET /dnc/stats
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dnc/stats")
async def dnc_stats(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)

    db = get_db()
    now = datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_7d  = (now - timedelta(days=7)).isoformat()

    # cache_size — total dnc_cache rows
    try:
        cache_size_res = db.table("dnc_cache").select("phone", count="exact").limit(1).execute()
        cache_size = cache_size_res.count or 0
    except Exception:
        cache_size = 0

    # suppression list size
    try:
        sup_res = db.table("dnc_suppressions").select("phone", count="exact").limit(1).execute()
        sup_size = sup_res.count or 0
    except Exception:
        sup_size = 0

    # Audit rollups (24h)
    try:
        audit_24h = (
            db.table("dnc_audit_log")
            .select("result, cost_usd, source, error")
            .gte("occurred_at", cutoff_24h)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"dnc_stats: 24h audit fetch failed: {e}")
        audit_24h = []

    fresh_24h     = sum(1 for r in audit_24h if r.get("result") == "fresh_lookup")
    cache_hits_24 = sum(1 for r in audit_24h if r.get("result") == "cache_hit")
    halts_24h     = sum(
        1 for r in audit_24h
        if r.get("result") == "error" and (r.get("error") or "").startswith("budget_cap")
    )
    cost_24h = round(
        sum(
            float(r.get("cost_usd") or 0)
            for r in audit_24h
            if not (
                r.get("result") == "error"
                and (r.get("error") or "").startswith("budget_cap")
            )
        ),
        5,
    )

    # Cache hit rate (7d)
    try:
        audit_7d = (
            db.table("dnc_audit_log")
            .select("result")
            .gte("occurred_at", cutoff_7d)
            .execute()
            .data or []
        )
    except Exception:
        audit_7d = []

    real_7d   = sum(1 for r in audit_7d if r.get("result") in ("fresh_lookup", "cache_hit"))
    hits_7d   = sum(1 for r in audit_7d if r.get("result") == "cache_hit")
    hit_rate  = round(hits_7d / real_7d * 100, 2) if real_7d else 0.0

    cost_7d = round(
        sum(
            float(r.get("cost_usd") or 0)
            for r in (
                db.table("dnc_audit_log")
                .select("cost_usd, result, error")
                .gte("occurred_at", cutoff_7d)
                .execute()
                .data or []
            )
            if not (
                r.get("result") == "error"
                and (r.get("error") or "").startswith("budget_cap")
            )
        ),
        5,
    )

    # Top sources (24h)
    src_counts: dict = {}
    for r in audit_24h:
        s = r.get("source") or "unknown"
        src_counts[s] = src_counts.get(s, 0) + 1
    top_sources = [
        {"source": s, "calls": n}
        for s, n in sorted(src_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    ]

    avg_cost = round(cost_24h / fresh_24h, 5) if fresh_24h else 0.0

    return {
        "cache_size":                  cache_size,
        "cache_hit_rate_7d_pct":       hit_rate,
        "fresh_lookups_24h":           fresh_24h,
        "cache_hits_24h":              cache_hits_24,
        "halt_events_24h":             halts_24h,
        "cost_24h_usd":                cost_24h,
        "cost_7d_usd":                 cost_7d,
        "suppression_list_size":       sup_size,
        "top_sources_24h":             top_sources,
        "avg_fresh_lookup_cost_usd":   avg_cost,
        "searchbug_balance_last":      _get_admin_setting("searchbug_balance_last", ""),
    }
