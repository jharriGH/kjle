"""
KJLE — Phase 4 Layer 2 Slice 2B: Soft-TTL background refresh helper
File: api/lib/cache_refresh.py

Invoked by /dnc/check (api/routes/dnc.py) via FastAPI BackgroundTasks AFTER
the response has been served from a stale cache row. Refreshes that row out of
band so the next caller gets a fresh value, without making the original caller
wait for the provider round-trip.

Key invariants:

1. **DB-level CAS, not app-level locks.**
   The refresh worker acquires exclusive ownership of the phone by issuing:

       UPDATE dnc_cache
       SET refresh_in_flight = true, last_refresh_attempt_at = now()
       WHERE phone = $1 AND refresh_in_flight = false
       RETURNING phone

   If no row is returned, another worker already won the race and we bail
   silently. Two concurrent stale hits can never both call the provider.

2. **Budget guard runs AFTER the CAS.**
   We don't want to consume budget headroom for a worker that lost the race.

3. **Reset refresh_in_flight on every exit path** (success, provider failure,
   exception, budget skip) so the row never deadlocks 'in flight'.

4. **Never raise.**
   FastAPI's BackgroundTasks runs after the response is flushed; an exception
   here cannot reach the client, but it would still pollute logs and (if the
   stack ever crashes mid-flight) leak the in-flight flag. Catch everything,
   log it, return cleanly.

Audit-log result values emitted by this module:

  - background_refresh_success         — refresh ran, cache updated
  - background_refresh_failure         — provider returned error or exception
  - background_refresh_skipped_budget  — budget guard rejected the refresh
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# Same per-lookup price as a foreground /dnc/check fresh lookup.
# Kept local rather than imported from routes/dnc.py to avoid a circular edge.
SEARCHBUG_COST_PER_LOOKUP_USD = 0.0214


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _get_admin_setting(db, key: str, default: str) -> str:
    try:
        res = db.table("admin_settings").select("value").eq("key", key).execute()
        if res.data and res.data[0].get("value") is not None:
            return str(res.data[0]["value"])
    except Exception as e:
        logger.warning(f"cache_refresh: admin_settings read failed for {key}: {e}")
    return default


def _audit(db, *, phone: str, result: str, is_dnc: Optional[bool],
           cost_usd: float = 0.0, error: Optional[str] = None,
           metadata: Optional[dict] = None) -> None:
    try:
        db.table("dnc_audit_log").insert({
            "phone":       phone,
            "source":      "kjle_internal",
            "result":      result,
            "is_dnc":      is_dnc,
            "cost_usd":    cost_usd,
            "error":       error,
            "metadata":    metadata or {},
            "occurred_at": _now_iso(),
        }).execute()
    except Exception as e:
        logger.error(f"cache_refresh: audit insert failed for {phone}: {e}")


def _release_in_flight(db, phone: str) -> None:
    """Reset refresh_in_flight=false on failure paths so the row isn't stuck."""
    try:
        db.table("dnc_cache").update({
            "refresh_in_flight": False,
        }).eq("phone", phone).execute()
    except Exception as e:
        logger.error(f"cache_refresh: failed to release in_flight for {phone}: {e}")


async def refresh_phone_in_background(phone: str) -> None:
    """
    Background task: attempt to refresh a stale DNC cache entry.

    Args:
        phone: E.164-normalized phone string. Caller (route handler) is
               responsible for normalization; this function trusts the input.

    Flow:
        1. DB-level CAS to claim the row.
        2. Budget guard.
        3. Provider call.
        4. UPSERT cache + log audit + log cost.
        5. Always reset refresh_in_flight on exit.

    Returns:
        None. Failures are audited but never raised.
    """
    # Lazy imports — avoid circular deps and keep this module cheap to import.
    try:
        from ..database import get_db
        from . import cost_guard
        from .dnc_provider import get_active_provider
    except Exception as e:
        logger.error(f"cache_refresh: import failed for {phone}: {e}")
        return

    try:
        db = get_db()
    except Exception as e:
        logger.error(f"cache_refresh: get_db failed for {phone}: {e}")
        return

    # 1. DB-level CAS — claim the row by flipping refresh_in_flight false->true.
    #    Under the hood supabase-py's PATCH returns representation of the rows
    #    actually updated, which is the equivalent of UPDATE ... RETURNING phone.
    try:
        cas = (
            db.table("dnc_cache")
            .update({
                "refresh_in_flight":       True,
                "last_refresh_attempt_at": _now_iso(),
            })
            .eq("phone", phone)
            .eq("refresh_in_flight", False)
            .execute()
        )
    except Exception as e:
        logger.error(f"cache_refresh: CAS update failed for {phone}: {e}")
        return

    if not cas.data:
        # Lost the race — another worker already holds the in-flight flag, or
        # the row no longer exists. Silent bail (expected, not an error).
        return

    # From this point onwards, we MUST clear refresh_in_flight before returning,
    # whether the refresh succeeds, fails, or is skipped.
    try:
        # 2. Budget guard
        try:
            budget_ok = await cost_guard.check_budget(
                service="searchbug",
                estimated_cost_usd=SEARCHBUG_COST_PER_LOOKUP_USD,
                job_name="dnc_background_refresh",
                leads_affected=1,
            )
        except Exception as e:
            logger.error(f"cache_refresh: budget_guard call failed for {phone}: {e}")
            budget_ok = False

        if not budget_ok:
            _audit(
                db, phone=phone,
                result="background_refresh_skipped_budget",
                is_dnc=None,
                error="budget_cap_exceeded",
            )
            _release_in_flight(db, phone)
            return

        # 3. Provider call
        provider = get_active_provider()
        try:
            result = await provider.check_phone(phone)
        except Exception as e:
            err = f"provider_exception: {type(e).__name__}: {e}"
            logger.error(f"cache_refresh: provider raised for {phone}: {err}")
            _audit(
                db, phone=phone,
                result="background_refresh_failure",
                is_dnc=None,
                error=err,
                metadata={"phase": "provider_call"},
            )
            _release_in_flight(db, phone)
            return

        if result.error:
            _audit(
                db, phone=phone,
                result="background_refresh_failure",
                is_dnc=None,
                error=result.error,
                metadata={"provider": result.provider, "phase": "provider_result"},
            )
            _release_in_flight(db, phone)
            return

        # 4. Success — UPSERT cache row with new values, soft + hard TTL,
        #              and clear refresh_in_flight atomically with the write.
        ttl_days = int(_get_admin_setting(db, "dnc_cache_ttl_days", "14") or "14")
        ext_days = int(_get_admin_setting(db, "dnc_soft_ttl_extension_days", "7") or "7")
        now = _now()
        expires_at      = (now + timedelta(days=ttl_days)).isoformat()
        hard_expires_at = (now + timedelta(days=ttl_days + ext_days)).isoformat()

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
            logger.error(f"cache_refresh: cache upsert failed for {phone}: {e}")
            _audit(
                db, phone=phone,
                result="background_refresh_failure",
                is_dnc=result.is_dnc,
                error=f"cache_upsert_failed: {e}",
                metadata={"phase": "cache_upsert"},
            )
            _release_in_flight(db, phone)
            return

        # Log the cost — only after a successful, billable provider response.
        try:
            await cost_guard.log_cost(
                stage="dnc",
                service="searchbug",
                cost_usd=SEARCHBUG_COST_PER_LOOKUP_USD,
                lead_id=None,
                items_fetched=1,
                job_run_id="dnc_background_refresh",
                metadata={"phone": phone, "source": "kjle_internal"},
            )
        except Exception as e:
            logger.error(f"cache_refresh: cost_guard.log_cost failed for {phone}: {e}")

        _audit(
            db, phone=phone,
            result="background_refresh_success",
            is_dnc=result.is_dnc,
            cost_usd=SEARCHBUG_COST_PER_LOOKUP_USD,
            metadata={"provider": result.provider, "line_type": result.line_type},
        )
    except Exception as e:
        # Last-resort safety net — no exception should ever escape this fn.
        logger.error(f"cache_refresh: unexpected exception for {phone}: {e}")
        try:
            _audit(
                db, phone=phone,
                result="background_refresh_failure",
                is_dnc=None,
                error=f"unexpected_exception: {type(e).__name__}: {e}",
            )
        except Exception:
            pass
        _release_in_flight(db, phone)
        return
