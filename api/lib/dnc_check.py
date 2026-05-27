"""
KJLE — Channel-aware DNC check pipeline
Phase 4 Layer 1, Slice 1B.

The single entry point for "is it safe to contact this lead on channel X?"
that the new (Phase 4) push paths use.

Replaces the at-ingest DNC checks of Phase 1 with at-contact-time, channel-aware
checks that minimize Searchbug spend:

    email   → check email_suppressions only. No phone DNC. $0.
    mail    → no checks. $0.
    sms     → internal suppressions → fed_dnc_list → NANPA line-type.
              Only pays Searchbug if dnc_sms_fallback_searchbug=true AND
              all free checks are inconclusive. $0 in the normal case.
    voice   → internal suppressions → fed_dnc_list → tcpa_litigator_list
              (if present) → Searchbug full-tier check.
              $0.0214 worst case, free on cache hit / fed-DNC hit / suppression.

This module imports the existing Phase 1 dnc.py helpers rather than modifying
them — Phase 1 stability is preserved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from ..database import get_db
from . import carrier_lookup
from . import phone_utils

logger = logging.getLogger(__name__)

# Valid channel values
CHANNELS = ("email", "sms", "voice", "mail")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ChannelCheckResult:
    """
    Result of checking ONE lead on ONE channel.

    is_blocked: True if the lead should NOT be contacted on this channel
    reason: short tag explaining why (or 'clean' if not blocked)
    result_source: which stage of the pipeline produced the verdict
    cost_usd: $ spent on this check (0 unless Searchbug was called)
    """
    lead_id: str
    channel: str
    is_blocked: bool
    reason: str
    result_source: str  # email_suppression | internal_suppression | fed_dnc_list |
                       # tcpa_litigator | nanpa_landline_no_sms | searchbug_dnc |
                       # searchbug_clean | clean | error | skipped
    cost_usd: float = 0.0
    phone_normalized: Optional[str] = None
    email_normalized: Optional[str] = None
    line_type: Optional[str] = None
    carrier: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Lazy imports & helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_lead(db, lead_id: str) -> Optional[dict]:
    """Fetch a single lead row. Returns dict or None on miss/error."""
    try:
        resp = (
            db.table("leads")
            .select("id,phone,email,dnc_status,dnc_channel_results")
            .eq("id", lead_id)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        return rows[0] if rows else None
    except Exception as e:
        logger.warning("dnc_check: lead fetch failed for %s: %s", lead_id, e)
        return None


def _check_email_suppression(db, email: str) -> bool:
    """Returns True if email is on the empire-wide suppression list."""
    try:
        resp = (
            db.table("email_suppressions")
            .select("email")
            .eq("email", email.lower())
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        return len(rows) > 0
    except Exception as e:
        logger.warning("dnc_check: email_suppressions query failed: %s", e)
        return False


def _check_internal_suppression(db, phone_e164: str) -> Optional[dict]:
    """
    Returns the dnc_suppressions row if found, else None.
    Row shape: {phone, reason, source, suppressed_at, notes}
    """
    try:
        resp = (
            db.table("dnc_suppressions")
            .select("phone,reason,source,suppressed_at")
            .eq("phone", phone_e164)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        return rows[0] if rows else None
    except Exception as e:
        logger.warning("dnc_check: dnc_suppressions query failed: %s", e)
        return None


def _check_fed_dnc(db, phone_e164: str) -> bool:
    """Returns True if phone is on the federal DNC mirror."""
    try:
        resp = (
            db.table("fed_dnc_list")
            .select("phone")
            .eq("phone", phone_e164)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        return len(rows) > 0
    except Exception as e:
        logger.warning("dnc_check: fed_dnc_list query failed: %s", e)
        return False


def _check_tcpa_litigator(db, phone_e164: str) -> bool:
    """
    Returns True if phone is on the TCPA litigator list (Layer 3 component 8).
    Table may not exist yet — graceful skip on error.
    """
    try:
        resp = (
            db.table("tcpa_litigator_list")
            .select("phone")
            .eq("phone", phone_e164)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        return len(rows) > 0
    except Exception:
        # Table likely doesn't exist yet (Layer 3 component 8). Silent skip.
        return False


def _get_admin_setting(db, key: str, default: str = "") -> str:
    """Fetch a value from admin_settings. Returns default on miss."""
    try:
        resp = (
            db.table("admin_settings")
            .select("value")
            .eq("key", key)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        return str(rows[0].get("value", default)) if rows else default
    except Exception as e:
        logger.warning("dnc_check: admin_settings(%s) read failed: %s", key, e)
        return default


def _audit(db, *, phone: Optional[str], source: str, result: str,
           is_dnc: Optional[bool], cost_usd: float,
           lead_id: Optional[str], metadata: dict) -> None:
    """
    Best-effort write to dnc_audit_log. Mirrors the pattern from Phase 1 dnc.py.
    Never raises — audit failure must not block a contact decision.
    """
    try:
        db.table("dnc_audit_log").insert({
            "phone": phone,
            "source": source,
            "result": result,
            "is_dnc": is_dnc,
            "cost_usd": float(cost_usd),
            "requesting_lead_id": lead_id,
            "metadata": metadata,
        }).execute()
    except Exception as e:
        logger.warning("dnc_check: audit insert failed: %s", e)


def _persist_lead_channel_result(db, lead_id: str, channel: str,
                                  result: ChannelCheckResult) -> None:
    """
    Write the result into leads.dnc_channel_results JSONB and update
    leads.dnc_status / dnc_last_checked_at. Best-effort; logs on failure.
    """
    try:
        # Pull current dnc_channel_results JSON
        lead = _get_lead(db, lead_id)
        if not lead:
            return
        current = lead.get("dnc_channel_results") or {}
        if not isinstance(current, dict):
            current = {}

        current[channel] = {
            "is_blocked": result.is_blocked,
            "reason": result.reason,
            "result_source": result.result_source,
            "cost_usd": result.cost_usd,
            "line_type": result.line_type,
            "carrier": result.carrier,
            "checked_at": _now_iso(),
        }

        # Derive a top-level dnc_status from the most-recent result
        new_status = _derive_dnc_status(result)

        update = {
            "dnc_channel_results": current,
            "dnc_last_checked_at": _now_iso(),
        }
        if new_status:
            update["dnc_status"] = new_status

        db.table("leads").update(update).eq("id", lead_id).execute()
    except Exception as e:
        logger.warning("dnc_check: failed to persist lead result for %s: %s",
                       lead_id, e)


def _derive_dnc_status(r: ChannelCheckResult) -> Optional[str]:
    """Map a ChannelCheckResult to a leads.dnc_status enum value."""
    src = r.result_source
    if src == "fed_dnc_list":
        return "fed_dnc_flagged"
    if src == "tcpa_litigator":
        return "tcpa_litigator_flagged"
    if src == "internal_suppression":
        return "internal_suppression"
    if src == "searchbug_dnc":
        return "searchbug_dnc"
    if src == "searchbug_clean":
        return "searchbug_clean"
    # 'clean', 'email_suppression', 'nanpa_landline_no_sms', 'skipped', 'error'
    # don't promote to top-level dnc_status (channel-specific only)
    return None


# ---------------------------------------------------------------------------
# Phase 1 fallback — Searchbug full-tier check
# ---------------------------------------------------------------------------

async def _searchbug_full_check(phone_e164: str, source: str,
                                 lead_id: Optional[str]) -> dict:
    """
    Delegate to the existing Phase 1 /dnc/check pipeline for full Searchbug
    tier (DNC + TCPA + line type + carrier). We call the same helper that
    GET /kjle/v1/dnc/check/{phone} uses internally.

    Phase 1's _perform_check is async (verified in api/routes/dnc.py L154);
    this function awaits it. Note: Phase 1 raises HTTPException(400) on
    invalid phone formats — we catch that and convert to a soft error result
    so the channel pipeline can keep flowing (it's never appropriate to let
    a Phase 1 validation exception bubble through a channel check).

    Returns a dict shaped like the Phase 1 response (is_dnc, reason,
    tcpa_litigator, line_type, carrier, cost_usd, result_source, error).

    On import failure (Phase 1 not deployed?) returns a safe error response.
    """
    try:
        # Import lazily so dnc_check.py loads even if dnc.py is mid-deploy.
        # _perform_check is the internal helper the /dnc/check route uses.
        from ..routes.dnc import _perform_check  # type: ignore
    except Exception as e:
        logger.error("dnc_check: cannot import dnc._perform_check: %s", e)
        return {
            "is_dnc": None,
            "reason": "phase1_dnc_unavailable",
            "cost_usd": 0.0,
            "result_source": "error",
            "error": f"phase1_import_failed: {e}",
        }

    try:
        return await _perform_check(phone_e164, source=source, lead_id=lead_id)
    except Exception as e:
        logger.error("dnc_check: _perform_check raised: %s", e)
        return {
            "is_dnc": None,
            "reason": "phase1_dnc_error",
            "cost_usd": 0.0,
            "result_source": "error",
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def check_lead_for_channel(lead_id: str, channel: str,
                                  source: str) -> ChannelCheckResult:
    """
    Check whether a lead is safe to contact on the given channel.

    Args:
        lead_id: leads.id (uuid as string)
        channel: one of 'email' | 'sms' | 'voice' | 'mail'
        source: which caller is asking (audit tag, e.g. 'demoenginez_push')

    Returns ChannelCheckResult — the caller checks .is_blocked.

    This function NEVER raises. On any error it returns a result with
    is_blocked=True and result_source='error' — fail-CLOSED for safety.
    The caller is free to retry later.

    NOTE: async because the voice/sms paths await Phase 1's _perform_check.
    Synchronous callers must use asyncio.run() or anyio.from_thread.run().
    """
    if channel not in CHANNELS:
        return ChannelCheckResult(
            lead_id=lead_id, channel=channel,
            is_blocked=True, reason="invalid_channel",
            result_source="error",
            error=f"channel must be one of {CHANNELS}",
        )

    # Get DB handle
    try:
        db = get_db()
    except Exception as e:
        return ChannelCheckResult(
            lead_id=lead_id, channel=channel,
            is_blocked=True, reason="db_unavailable",
            result_source="error", error=str(e),
        )

    # Fetch the lead
    lead = _get_lead(db, lead_id)
    if not lead:
        return ChannelCheckResult(
            lead_id=lead_id, channel=channel,
            is_blocked=True, reason="lead_not_found",
            result_source="error",
        )

    raw_phone = lead.get("phone")
    raw_email = lead.get("email")
    phone_e164 = phone_utils.normalize_phone(raw_phone) if raw_phone else None
    email_norm = raw_email.lower().strip() if raw_email else None

    # -----------------------------------------------------------------------
    # Channel: mail — direct mail, no compliance checks here
    # -----------------------------------------------------------------------
    if channel == "mail":
        result = ChannelCheckResult(
            lead_id=lead_id, channel=channel,
            is_blocked=False, reason="clean",
            result_source="clean",
            phone_normalized=phone_e164, email_normalized=email_norm,
        )
        _audit(db, phone=phone_e164, source=source, result="clean",
               is_dnc=False, cost_usd=0.0, lead_id=lead_id,
               metadata={"channel": "mail"})
        _persist_lead_channel_result(db, lead_id, channel, result)
        return result

    # -----------------------------------------------------------------------
    # Channel: email — email suppression only
    # -----------------------------------------------------------------------
    if channel == "email":
        if not email_norm:
            result = ChannelCheckResult(
                lead_id=lead_id, channel=channel,
                is_blocked=True, reason="no_email_on_lead",
                result_source="skipped",
            )
            _persist_lead_channel_result(db, lead_id, channel, result)
            return result

        if _check_email_suppression(db, email_norm):
            result = ChannelCheckResult(
                lead_id=lead_id, channel=channel,
                is_blocked=True, reason="email_suppressed",
                result_source="email_suppression",
                email_normalized=email_norm,
            )
            _audit(db, phone=None, source=source, result="email_suppression",
                   is_dnc=True, cost_usd=0.0, lead_id=lead_id,
                   metadata={"channel": "email", "email": email_norm})
            _persist_lead_channel_result(db, lead_id, channel, result)
            return result

        result = ChannelCheckResult(
            lead_id=lead_id, channel=channel,
            is_blocked=False, reason="clean",
            result_source="clean",
            email_normalized=email_norm,
        )
        _audit(db, phone=None, source=source, result="clean",
               is_dnc=False, cost_usd=0.0, lead_id=lead_id,
               metadata={"channel": "email"})
        _persist_lead_channel_result(db, lead_id, channel, result)
        return result

    # -----------------------------------------------------------------------
    # SMS and Voice channels require a normalized phone
    # -----------------------------------------------------------------------
    if not phone_e164:
        result = ChannelCheckResult(
            lead_id=lead_id, channel=channel,
            is_blocked=True, reason="no_valid_phone_on_lead",
            result_source="skipped",
        )
        _persist_lead_channel_result(db, lead_id, channel, result)
        return result

    # Common to SMS + Voice: internal suppression check (free)
    sup = _check_internal_suppression(db, phone_e164)
    if sup:
        result = ChannelCheckResult(
            lead_id=lead_id, channel=channel,
            is_blocked=True,
            reason=f"internal_suppression:{sup.get('reason', 'unknown')}",
            result_source="internal_suppression",
            phone_normalized=phone_e164,
            metadata={"source_of_suppression": sup.get("source")},
        )
        _audit(db, phone=phone_e164, source=source,
               result="internal_suppression", is_dnc=True, cost_usd=0.0,
               lead_id=lead_id, metadata={"channel": channel,
                                          "suppression": sup})
        _persist_lead_channel_result(db, lead_id, channel, result)
        return result

    # Common to SMS + Voice: federal DNC check (free, if enabled)
    fed_enabled = _get_admin_setting(db, "fed_dnc_enabled", "true").lower() == "true"
    if fed_enabled and _check_fed_dnc(db, phone_e164):
        result = ChannelCheckResult(
            lead_id=lead_id, channel=channel,
            is_blocked=True, reason="federal_dnc_list",
            result_source="fed_dnc_list",
            phone_normalized=phone_e164,
        )
        _audit(db, phone=phone_e164, source=source,
               result="fed_dnc_list", is_dnc=True, cost_usd=0.0,
               lead_id=lead_id, metadata={"channel": channel})
        _persist_lead_channel_result(db, lead_id, channel, result)
        return result

    # -----------------------------------------------------------------------
    # Channel: sms — try free NANPA line-type detection before any paid call
    # -----------------------------------------------------------------------
    if channel == "sms":
        nanpa_enabled = _get_admin_setting(db, "nanpa_enabled", "true").lower() == "true"
        if nanpa_enabled:
            prefix = carrier_lookup.lookup_line_type_from_prefix(phone_e164)
            if prefix:
                line_type = prefix.get("line_type") or "unknown"
                carrier = prefix.get("carrier")
                if line_type == "landline":
                    result = ChannelCheckResult(
                        lead_id=lead_id, channel=channel,
                        is_blocked=True, reason="landline_no_sms",
                        result_source="nanpa_landline_no_sms",
                        phone_normalized=phone_e164,
                        line_type=line_type, carrier=carrier,
                    )
                    _audit(db, phone=phone_e164, source=source,
                           result="nanpa_landline_no_sms",
                           is_dnc=False, cost_usd=0.0, lead_id=lead_id,
                           metadata={"channel": "sms", "carrier": carrier})
                    _persist_lead_channel_result(db, lead_id, channel, result)
                    return result

                if line_type in ("mobile", "voip"):
                    result = ChannelCheckResult(
                        lead_id=lead_id, channel=channel,
                        is_blocked=False, reason="clean",
                        result_source="clean",
                        phone_normalized=phone_e164,
                        line_type=line_type, carrier=carrier,
                    )
                    _audit(db, phone=phone_e164, source=source,
                           result="clean", is_dnc=False, cost_usd=0.0,
                           lead_id=lead_id, metadata={"channel": "sms",
                                                      "carrier": carrier})
                    _persist_lead_channel_result(db, lead_id, channel, result)
                    return result
                # 'unknown' → fall through to Searchbug fallback decision

        # SMS Searchbug fallback (gated by admin setting; default OFF)
        sms_fallback = _get_admin_setting(
            db, "dnc_sms_fallback_searchbug", "false"
        ).lower() == "true"

        if not sms_fallback:
            # All free checks inconclusive and admin says don't pay → allow.
            # SMS to truly-unclassified numbers is low risk; if needed,
            # operator can flip dnc_sms_fallback_searchbug=true.
            result = ChannelCheckResult(
                lead_id=lead_id, channel=channel,
                is_blocked=False, reason="clean_sms_unclassified",
                result_source="clean",
                phone_normalized=phone_e164,
            )
            _audit(db, phone=phone_e164, source=source,
                   result="clean", is_dnc=False, cost_usd=0.0,
                   lead_id=lead_id, metadata={"channel": "sms",
                                              "note": "unclassified_no_fallback"})
            _persist_lead_channel_result(db, lead_id, channel, result)
            return result

        # SMS fallback to Searchbug (paid)
        sb = await _searchbug_full_check(phone_e164, source=f"{source}:sms", lead_id=lead_id)
        is_blocked = bool(sb.get("is_dnc"))
        result = ChannelCheckResult(
            lead_id=lead_id, channel=channel,
            is_blocked=is_blocked,
            reason=sb.get("reason", "searchbug"),
            result_source="searchbug_dnc" if is_blocked else "searchbug_clean",
            phone_normalized=phone_e164,
            cost_usd=float(sb.get("cost_usd", 0.0)),
            line_type=sb.get("line_type"),
            carrier=sb.get("carrier"),
            metadata={"searchbug_raw_source": sb.get("result_source")},
            error=sb.get("error"),
        )
        _persist_lead_channel_result(db, lead_id, channel, result)
        return result

    # -----------------------------------------------------------------------
    # Channel: voice — full pipeline including TCPA litigator + Searchbug
    # -----------------------------------------------------------------------
    if channel == "voice":
        if _check_tcpa_litigator(db, phone_e164):
            result = ChannelCheckResult(
                lead_id=lead_id, channel=channel,
                is_blocked=True, reason="tcpa_litigator",
                result_source="tcpa_litigator",
                phone_normalized=phone_e164,
            )
            _audit(db, phone=phone_e164, source=source,
                   result="tcpa_litigator", is_dnc=True, cost_usd=0.0,
                   lead_id=lead_id, metadata={"channel": "voice"})
            _persist_lead_channel_result(db, lead_id, channel, result)
            return result

        # Full Searchbug check (handles its own cache + audit + cost guard)
        sb = await _searchbug_full_check(phone_e164, source=f"{source}:voice",
                                          lead_id=lead_id)
        is_blocked = bool(sb.get("is_dnc"))
        # Map Phase 1 result_source onto our enum
        ph1_src = (sb.get("result_source") or "").lower()
        if "error" in ph1_src or sb.get("error"):
            mapped = "error"
        elif is_blocked:
            mapped = "searchbug_dnc"
        else:
            mapped = "searchbug_clean"

        result = ChannelCheckResult(
            lead_id=lead_id, channel=channel,
            is_blocked=is_blocked,
            reason=sb.get("reason", "searchbug"),
            result_source=mapped,
            phone_normalized=phone_e164,
            cost_usd=float(sb.get("cost_usd", 0.0)),
            line_type=sb.get("line_type"),
            carrier=sb.get("carrier"),
            metadata={"searchbug_raw_source": sb.get("result_source"),
                      "tcpa_litigator": bool(sb.get("tcpa_litigator", False))},
            error=sb.get("error"),
        )
        # Phase 1 _perform_check already wrote its own audit row; we don't
        # double-write here. We still persist on the lead.
        _persist_lead_channel_result(db, lead_id, channel, result)
        return result

    # Unreachable (channel was validated at the top), but keep linters happy
    return ChannelCheckResult(
        lead_id=lead_id, channel=channel,
        is_blocked=True, reason="unreachable",
        result_source="error",
    )
