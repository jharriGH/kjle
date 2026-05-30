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

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db
from ..lib import cost_guard
from ..lib.cache_refresh import refresh_phone_in_background
from ..lib.dnc_provider import DNCResult, get_active_provider
from ..lib.phone_filters import classify_phone_quality
from ..lib.phone_utils import normalize_phone
from ..lib.tcpa_check import is_tcpa_litigator

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


def _parse_ts(raw) -> Optional[datetime]:
    """Parse a Postgres-emitted ISO-8601 timestamptz to a tz-aware datetime."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        s = str(raw)
        # Postgres often emits "...+00:00"; fromisoformat handles that on 3.11+.
        # Handle "Z" suffix as a safety net.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


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


async def _perform_check(
    raw_phone: str,
    source: str,
    lead_id: Optional[str],
    background_tasks: Optional[BackgroundTasks] = None,
) -> dict:
    """
    Shared lookup pipeline used by both /dnc/check/{phone} and /dnc/check.
    Returns a serializable dict. Raises HTTPException(400) only on invalid
    phone format (the one case where we don't write an audit row).

    background_tasks is optional so /dnc/scrub-batch (which iterates this fn
    in-loop) can pass None. When None, a stale-cache hit degrades to a
    foreground refresh — the batch path is already async-friendly and the
    extra latency there is acceptable.
    """
    phone = normalize_phone(raw_phone)
    if not phone:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_phone_format: could not normalize {raw_phone!r} to US E.164",
        )

    db = get_db()
    src = source or "unknown"

    # 0. Whitelist short-circuit — overrides suppressions, cache, budget cap,
    #    and provider. A whitelist hit means "always allow this number."
    try:
        wl = db.table("dnc_whitelist").select("phone").eq("phone", phone).execute()
        if wl.data:
            _audit(
                phone=phone, source=src, result="whitelist",
                is_dnc=False, requesting_lead_id=lead_id,
            )
            return _serialize_response(
                result_source="whitelist",
                phone_normalized=phone, is_dnc=False,
                reason="whitelisted",
            )
    except Exception as e:
        logger.warning(f"dnc: whitelist read failed for {phone}: {e}")
        # Fall through — better to consult suppressions/cache/provider than 500.

    # 0.5 Carrier-pattern short-circuit (Phase 4 Layer 3 Slice 3B)
    #
    #     Deterministic + free. Run BEFORE suppression / TCPA / cache /
    #     provider so structural garbage (NPA-555 / NXX-555 numbers,
    #     toll-free NPAs, sequential test numbers like X-XXX-1234567,
    #     all-zeros, all-nines, and all-same-digit subscriber portions)
    #     never burns a DB row or a Searchbug $0.0214.
    phone_quality = classify_phone_quality(phone)
    if not phone_quality["contactable"]:
        _audit(
            phone=phone, source=src, result="carrier_pattern_blocked",
            is_dnc=True, cost_usd=0.0, requesting_lead_id=lead_id,
            metadata={
                "pattern_hits": phone_quality["pattern_hits"],
                "reasons":      phone_quality["reasons"],
            },
        )
        return _serialize_response(
            result_source="carrier_pattern_blocked",
            phone_normalized=phone, is_dnc=True,
            reason=",".join(phone_quality["reasons"]),
            tcpa=False,
        )

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

    # 1.5 TCPA litigator pre-check (Phase 4 Layer 3 Slice 3A)
    #
    #     Pre-cache, pre-Searchbug. A phone on the litigator list is DNC under
    #     any circumstance regardless of consent, so we must not let a
    #     previously-cached "clean" row hide the match. The check is a PK
    #     lookup against tcpa_litigators — free, indexed, fast.
    #
    #     is_tcpa_litigator() degrades to is_litigator=False on read failure
    #     so this layer never 500s the request — it's hardening, not gating.
    tcpa_res = await is_tcpa_litigator(phone, db)
    if tcpa_res.get("is_litigator"):
        matched_row = tcpa_res.get("matched_row") or {}
        _audit(
            phone=phone, source=src, result="tcpa_litigator_match",
            is_dnc=True, cost_usd=0.0, requesting_lead_id=lead_id,
            metadata={"litigator_row": matched_row},
        )
        return _serialize_response(
            result_source="tcpa_litigator_match",
            phone_normalized=phone, is_dnc=True,
            reason="tcpa_litigator_match",
            tcpa=True,
        )

    # 2. Cache lookup with soft TTL (Phase 4 Layer 2 Slice 2B)
    #
    #    Three cases:
    #      a. now < expires_at        — FRESH HIT, return as-is
    #      b. now < hard_expires_at   — STALE HIT, return stale value AND queue
    #                                   a background refresh via BackgroundTasks
    #                                   so the next caller gets fresh data
    #      c. now >= hard_expires_at  — fall through to fresh provider lookup
    try:
        c = (
            db.table("dnc_cache")
            .select("*")
            .eq("phone", phone)
            .execute()
        )
        if c.data:
            row = c.data[0]
            now = datetime.now(timezone.utc)
            expires_at_raw      = row.get("expires_at")
            hard_expires_at_raw = row.get("hard_expires_at")
            expires_at      = _parse_ts(expires_at_raw)
            hard_expires_at = _parse_ts(hard_expires_at_raw) or expires_at

            if expires_at and now < expires_at:
                # a) fresh hit
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

            if hard_expires_at and now < hard_expires_at:
                # b) stale hit — serve stale, queue background refresh
                _audit(
                    phone=phone, source=src, result="cache_hit_stale_served",
                    is_dnc=row.get("is_dnc"), requesting_lead_id=lead_id,
                )
                if background_tasks is not None:
                    background_tasks.add_task(refresh_phone_in_background, phone)
                return _serialize_response(
                    result_source="cache_hit_stale_served",
                    phone_normalized=phone,
                    is_dnc=row.get("is_dnc"),
                    reason=row.get("dnc_reason") or "",
                    tcpa=bool(row.get("tcpa_litigator")),
                    line_type=row.get("line_type") or "unknown",
                    carrier=row.get("carrier") or "",
                )
            # c) hard-expired — fall through to fresh provider lookup
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
    ext_days = int(_get_admin_setting("dnc_soft_ttl_extension_days", "7") or "7")
    now_dt = datetime.now(timezone.utc)
    expires_at      = (now_dt + timedelta(days=ttl_days)).isoformat()
    hard_expires_at = (now_dt + timedelta(days=ttl_days + ext_days)).isoformat()

    try:
        db.table("dnc_cache").upsert({
            "phone":             phone,
            "is_dnc":            result.is_dnc,
            "dnc_reason":        result.reason or "",
            "tcpa_litigator":    result.tcpa_litigator,
            "line_type":         result.line_type or "unknown",
            "carrier":           result.carrier or "",
            "fetched_at":        _now_iso(),
            "expires_at":        expires_at,
            "hard_expires_at":   hard_expires_at,
            "raw_response":      result.raw_response or {},
            "source_provider":   result.provider or "searchbug",
            "refresh_in_flight": False,
        }, on_conflict="phone").execute()
    except Exception as e:
        logger.error(f"dnc: cache upsert failed for {phone}: {e}")

    # 5b. TCPA harvest (Slice 3A.1) — if Searchbug flagged this phone as a TCPA
    #     litigator, upsert into tcpa_litigators so future /dnc/check calls
    #     short-circuit at the pre-cache guard (Slice 3A) at $0 cost.
    if result.tcpa_litigator:
        try:
            db.table("tcpa_litigators").upsert({
                "phone":               phone,
                "source":              "searchbug_harvest",
                "name":                None,
                "state":               None,
                "case_count":          None,
                "last_refreshed_at":   _now_iso(),
                "metadata":            {
                    "harvested_from": "searchbug_api_lnd2",
                    "carrier":        result.carrier or "",
                    "line_type":      result.line_type or "unknown",
                    "dnc_reason":     result.reason or "",
                },
            }, on_conflict="phone").execute()
            logger.info(f"dnc: harvested TCPA litigator from Searchbug: {phone}")
        except Exception as e:
            logger.error(f"dnc: tcpa_litigators harvest upsert failed for {phone}: {e}")

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

    # Balance persistence + threshold alerts are handled by SearchbugProvider
    # itself (api/lib/searchbug_provider.py::_persist_and_alert_balance).

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
# Email-side helpers (Phase 3: companion to phone DNC check)
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_email(raw: Optional[str]) -> Optional[str]:
    """Lowercase + strip + sanity-check for '@'. Returns None on invalid."""
    if not raw:
        return None
    s = str(raw).strip().lower()
    if not s or "@" not in s or s.startswith("@") or s.endswith("@"):
        return None
    return s


async def _perform_email_check(raw_email: str, source: str) -> dict:
    """Shared lookup pipeline used by both /dnc/check-email and /dnc/check-email/{email}."""
    email = _normalize_email(raw_email)
    if not email:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_email_format: could not normalize {raw_email!r}",
        )

    db = get_db()
    src = source or "unknown"

    is_suppressed = False
    reason = ""
    metadata: dict = {}

    try:
        res = (
            db.table("email_suppressions")
            .select("email, reason, source")
            .eq("email", email)
            .execute()
        )
        if res.data:
            row = res.data[0]
            is_suppressed = True
            reason = f"internal_suppression:{row.get('reason') or 'unknown'}"
            metadata = {"suppression_reason": row.get("reason"), "suppression_source": row.get("source")}
    except Exception as e:
        logger.warning(f"dnc: email_suppressions read failed for {email}: {e}")
        # Treat lookup failure as 'clean' for the response, but flag in audit.
        metadata = {"lookup_error": str(e)[:200]}

    # Audit row — phone column NULL since this is an email lookup
    _audit(
        phone=None, source=src,
        result=("internal_suppression" if is_suppressed else "clean_email"),
        is_dnc=None,
        metadata={"email": email, **metadata},
    )

    return {
        "is_suppressed":     is_suppressed,
        "reason":            reason,
        "result_source":     "internal_suppression" if is_suppressed else "clean",
        "email_normalized":  email,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /dnc/check-email          (query param fallback — Phase 3)
# GET /dnc/check-email/{email}  (path param)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dnc/check-email")
async def dnc_check_email_query(
    email:  str = Query(..., description="Email address (any case)"),
    source: str = Query("unknown"),
):
    return await _perform_email_check(email, source)


@router.get("/dnc/check-email/{email}")
async def dnc_check_email_path(
    email:  str,
    source: str = Query("unknown"),
):
    return await _perform_email_check(email, source)


# ─────────────────────────────────────────────────────────────────────────────
# GET /dnc/check  (query-param fallback — must precede /{phone} for clarity)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dnc/check")
async def dnc_check_query(
    background_tasks: BackgroundTasks,
    phone:   str           = Query(..., description="US phone in any format"),
    source:  str           = Query("unknown"),
    lead_id: Optional[str] = Query(None),
):
    return await _perform_check(phone, source, lead_id, background_tasks)


# ─────────────────────────────────────────────────────────────────────────────
# GET /dnc/check/{phone}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dnc/check/{phone}")
async def dnc_check_path(
    phone:   str,
    background_tasks: BackgroundTasks,
    source:  str           = Query("unknown"),
    lead_id: Optional[str] = Query(None),
):
    return await _perform_check(phone, source, lead_id, background_tasks)


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

    # Phase 4 Layer 3 Slice 3A — TCPA litigator-list size
    try:
        tcpa_size_res = db.table("tcpa_litigators").select("phone", count="exact").limit(1).execute()
        tcpa_litigator_list_size = tcpa_size_res.count or 0
    except Exception:
        tcpa_litigator_list_size = 0

    # Slice 3A.1: how many of the litigators in the table were harvested from
    # Searchbug responses (vs from a future paid vendor).
    try:
        tcpa_harvest_res = (
            db.table("tcpa_litigators")
            .select("phone", count="exact")
            .like("source", "searchbug_harvest%")
            .limit(1)
            .execute()
        )
        tcpa_litigator_harvested_count = tcpa_harvest_res.count or 0
    except Exception:
        tcpa_litigator_harvested_count = 0

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

    # Phase 4 Layer 2 Slice 2B — soft-TTL observability
    stale_hits_24h          = sum(1 for r in audit_24h if r.get("result") == "cache_hit_stale_served")
    bg_refresh_success_24h  = sum(1 for r in audit_24h if r.get("result") == "background_refresh_success")
    bg_refresh_failure_24h  = sum(1 for r in audit_24h if r.get("result") == "background_refresh_failure")
    background_refreshes_24h = bg_refresh_success_24h + bg_refresh_failure_24h

    # Phase 4 Layer 3 Slice 3A — TCPA litigator matches in last 24h
    tcpa_litigator_matches_24h = sum(
        1 for r in audit_24h if r.get("result") == "tcpa_litigator_match"
    )

    # Phase 4 Layer 3 Slice 3B — carrier-pattern blocks (last 24h)
    carrier_pattern_blocks_24h = sum(
        1 for r in audit_24h if r.get("result") == "carrier_pattern_blocked"
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

    # Phase 4 Layer 3 Slice 3B — carrier-pattern blocks (last 7d)
    carrier_pattern_blocks_7d = sum(
        1 for r in audit_7d if r.get("result") == "carrier_pattern_blocked"
    )

    # Phase 4 Layer 2 Slice 2B — 7d background-refresh failure rate
    bg_success_7d = sum(1 for r in audit_7d if r.get("result") == "background_refresh_success")
    bg_failure_7d = sum(1 for r in audit_7d if r.get("result") == "background_refresh_failure")
    bg_total_7d   = bg_success_7d + bg_failure_7d
    background_refresh_failure_rate_7d_pct = (
        round(bg_failure_7d / bg_total_7d * 100, 2) if bg_total_7d else 0.0
    )

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
        # Phase 4 Layer 2 Slice 2B — soft-TTL observability
        "stale_hits_24h":                          stale_hits_24h,
        "background_refreshes_24h":                background_refreshes_24h,
        "background_refresh_failure_rate_7d_pct":  background_refresh_failure_rate_7d_pct,
        # Phase 4 Layer 3 Slice 3A — TCPA litigator pre-check observability
        "tcpa_litigator_list_size":         tcpa_litigator_list_size,
        "tcpa_litigator_matches_24h":       tcpa_litigator_matches_24h,
        "tcpa_litigator_last_refreshed_at": (
            _get_admin_setting("tcpa_list_last_refresh_at", "") or None
        ),
        # Phase 4 Layer 3 Slice 3A.1 — Searchbug-harvest visibility
        "tcpa_litigator_harvested_count": tcpa_litigator_harvested_count,
        # Phase 4 Layer 3 Slice 3B — carrier-pattern observability
        "carrier_pattern_blocks_24h": carrier_pattern_blocks_24h,
        "carrier_pattern_blocks_7d":  carrier_pattern_blocks_7d,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Per-consumer breakdown — Phase 4 Layer 2 Slice 2A
#
# Shared by:
#   GET /kjle/v1/dnc/stats/by-consumer
#   api/lib/daily_report.py::_dnc_section (consumer breakdown sub-block)
#
# Returns a dict with the same shape as the endpoint response so the report
# can format directly off it. SQL lives here, not duplicated downstream.
# ─────────────────────────────────────────────────────────────────────────────

BY_CONSUMER_PERIOD_HOURS_MAX = 720  # 30 days


def per_consumer_breakdown(period_hours: int) -> dict:
    """
    Aggregate dnc_audit_log over the last `period_hours` grouped by `source`.

    Returns:
        {
          "period_hours": int,
          "as_of": iso8601 str,
          "consumers": [
            {
              consumer_app, calls, fresh_lookups, cache_hits,
              internal_suppressions, errors, hit_rate_pct, cost_usd
            },
            ...
          ],
          "totals": { same shape, aggregate across all consumers }
        }

    hit_rate_pct = cache_hits / (cache_hits + fresh_lookups) * 100. Internal
    suppressions are excluded from the denominator — they're a pre-cache layer.
    Budget-cap errors are excluded from cost_usd (they never actually billed).
    """
    if period_hours < 1:
        period_hours = 1
    if period_hours > BY_CONSUMER_PERIOD_HOURS_MAX:
        period_hours = BY_CONSUMER_PERIOD_HOURS_MAX

    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=period_hours)).isoformat()

    db = get_db()
    try:
        rows = (
            db.table("dnc_audit_log")
            .select("source, result, cost_usd, error")
            .gte("occurred_at", since)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"dnc per_consumer_breakdown: audit fetch failed: {e}")
        rows = []

    # Aggregate per consumer
    agg: dict[str, dict] = {}
    for r in rows:
        src = (r.get("source") or "").strip() or "unknown"
        bucket = agg.setdefault(src, {
            "calls":                 0,
            "fresh_lookups":         0,
            "cache_hits":            0,
            "internal_suppressions": 0,
            "errors":                0,
            "cost_usd_raw":          0.0,
        })
        bucket["calls"] += 1
        result = r.get("result")
        if result == "fresh_lookup":
            bucket["fresh_lookups"] += 1
        elif result == "cache_hit":
            bucket["cache_hits"] += 1
        elif result == "cache_hit_stale_served":
            # Slice 2B — stale-served still counts as a hit for the consumer
            # (no provider call from their perspective).
            bucket["cache_hits"] += 1
        elif result == "background_refresh_success":
            # Slice 2B — billable fresh lookup attributed to kjle_internal
            # via the source column set by cache_refresh.py.
            bucket["fresh_lookups"] += 1
        elif result in ("background_refresh_failure", "background_refresh_skipped_budget"):
            # Slice 2B — non-billable failure; attributed to kjle_internal.
            bucket["errors"] += 1
        elif result == "internal_suppression":
            bucket["internal_suppressions"] += 1
        elif result == "tcpa_litigator_match":
            # Slice 3A — categorical pre-cache block, free to the consumer.
            # Same shape as internal_suppression (no provider call, cost=0)
            # so we bucket it alongside to keep sub-bucket totals consistent.
            bucket["internal_suppressions"] += 1
        elif result == "carrier_pattern_blocked":
            # Slice 3B — structural-garbage pre-cache block, free to the
            # consumer. Same shape as internal_suppression (no provider
            # call, cost=0) — bucket alongside.
            bucket["internal_suppressions"] += 1
        elif result == "error":
            bucket["errors"] += 1

        # Cost — exclude budget-cap rejections (they never billed)
        if not (
            result == "error"
            and (r.get("error") or "").startswith("budget_cap")
        ):
            try:
                bucket["cost_usd_raw"] += float(r.get("cost_usd") or 0)
            except (TypeError, ValueError):
                pass

    def _hit_rate(cache_hits: int, fresh: int) -> float:
        denom = cache_hits + fresh
        return round(cache_hits / denom * 100, 2) if denom else 0.0

    consumers = []
    for src, b in sorted(agg.items(), key=lambda kv: kv[1]["calls"], reverse=True):
        consumers.append({
            "consumer_app":          src,
            "calls":                 b["calls"],
            "fresh_lookups":         b["fresh_lookups"],
            "cache_hits":            b["cache_hits"],
            "internal_suppressions": b["internal_suppressions"],
            "errors":                b["errors"],
            "hit_rate_pct":          _hit_rate(b["cache_hits"], b["fresh_lookups"]),
            "cost_usd":              round(b["cost_usd_raw"], 5),
        })

    # Totals
    t_calls   = sum(c["calls"]                 for c in consumers)
    t_fresh   = sum(c["fresh_lookups"]         for c in consumers)
    t_hits    = sum(c["cache_hits"]            for c in consumers)
    t_supp    = sum(c["internal_suppressions"] for c in consumers)
    t_errs    = sum(c["errors"]                for c in consumers)
    t_cost    = round(sum(c["cost_usd"]        for c in consumers), 5)

    totals = {
        "consumer_app":          "total",
        "calls":                 t_calls,
        "fresh_lookups":         t_fresh,
        "cache_hits":            t_hits,
        "internal_suppressions": t_supp,
        "errors":                t_errs,
        "hit_rate_pct":          _hit_rate(t_hits, t_fresh),
        "cost_usd":              t_cost,
    }

    return {
        "period_hours": period_hours,
        "as_of":        now.isoformat(),
        "consumers":    consumers,
        "totals":       totals,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /dnc/stats/by-consumer
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dnc/stats/by-consumer")
async def dnc_stats_by_consumer(
    x_api_key:    str = Header(...),
    period_hours: int = Query(24, ge=1, le=BY_CONSUMER_PERIOD_HOURS_MAX,
                              description="Window length, hours. Max 720 (30d)."),
):
    verify_api_key(x_api_key)
    return per_consumer_breakdown(period_hours)
