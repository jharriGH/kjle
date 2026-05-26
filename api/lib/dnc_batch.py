"""
KJLE — Phone-dedup batch DNC check
Phase 4 Layer 1, Slice 1B.

Wraps check_lead_for_channel so that 50 leads sharing the same phone
generate ONE Searchbug call instead of 50. Critical for push paths where
many enriched leads share a corporate phone or franchise main line.

Pipeline:
    1. Fetch lead phones in bulk
    2. Normalize and group by E.164
    3. Call check_lead_for_channel ONCE per unique phone (using a
       representative lead_id from each group)
    4. Fan the result out to every lead_id in that group
    5. Return aggregated stats and per-lead verdicts

Leads with no phone are routed through check_lead_for_channel individually
since they can't be deduped on phone anyway.

NOTE: async. dedupe_and_check_phones() awaits check_lead_for_channel()
which in turn awaits Phase 1's async _perform_check(). Synchronous callers
in Slice 1C push handlers must use FastAPI's `await` in their route fns.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from ..database import get_db
from . import dnc_check
from . import phone_utils

logger = logging.getLogger(__name__)


def _fetch_leads_phones(db, lead_ids: list[str]) -> dict[str, dict]:
    """
    Bulk-fetch the phone column for a list of lead_ids.

    Returns a dict mapping lead_id -> {phone, email}. Leads not found are
    omitted; the caller treats absence as "lookup failed".

    Supabase has a per-request row cap (~1000 by default); we chunk at 500
    to stay well under and keep response payloads small.
    """
    out: dict[str, dict] = {}
    if not lead_ids:
        return out

    CHUNK = 500
    for i in range(0, len(lead_ids), CHUNK):
        chunk = lead_ids[i:i + CHUNK]
        try:
            resp = (
                db.table("leads")
                .select("id,phone,email")
                .in_("id", chunk)
                .execute()
            )
            for row in (getattr(resp, "data", None) or []):
                lid = str(row.get("id"))
                out[lid] = {
                    "phone": row.get("phone"),
                    "email": row.get("email"),
                }
        except Exception as e:
            logger.warning("dnc_batch: leads fetch chunk failed: %s", e)
            # Continue with whatever we have

    return out


def _group_by_phone(
    lead_ids: list[str], rows: dict[str, dict]
) -> tuple[dict[str, list[str]], list[str], list[str]]:
    """
    Group lead_ids by normalized phone.

    Returns:
        groups: {phone_e164: [lead_id, ...]}
        no_phone_lead_ids: leads with missing/invalid phones
        unknown_lead_ids: lead_ids that weren't found in leads table at all
    """
    groups: dict[str, list[str]] = {}
    no_phone: list[str] = []
    unknown: list[str] = []

    for lid in lead_ids:
        row = rows.get(str(lid))
        if row is None:
            unknown.append(lid)
            continue
        raw = row.get("phone")
        phone_e164 = phone_utils.normalize_phone(raw) if raw else None
        if not phone_e164:
            no_phone.append(lid)
            continue
        groups.setdefault(phone_e164, []).append(lid)

    return groups, no_phone, unknown


async def dedupe_and_check_phones(
    lead_ids: Iterable[str], channel: str, source: str,
) -> dict[str, Any]:
    """
    Phone-deduplicated batch DNC check.

    Args:
        lead_ids: iterable of leads.id (uuid strings)
        channel: one of 'email' | 'sms' | 'voice' | 'mail'
        source: audit tag (e.g. 'demoenginez_push')

    Returns:
        {
            'channel': str,
            'source': str,
            'total_leads_requested': int,
            'total_leads_covered': int,
            'unique_phones_checked': int,
            'cost_usd': float,
            'allowed_leads': [lead_id, ...],
            'blocked_leads': [{'lead_id': str, 'reason': str,
                              'result_source': str}, ...],
            'unknown_leads': [lead_id, ...],   # not found in leads table
            'no_phone_leads': [lead_id, ...],  # blocked for phone-bearing channels
            'per_phone_results': [
                {'phone': str, 'lead_ids': [...], 'result': {...ChannelCheckResult...}},
                ...
            ],
        }

    This function NEVER raises. On critical failure, it returns a result with
    everything in blocked_leads and an 'error' key at the top level.
    """
    lead_ids_list = [str(lid) for lid in lead_ids if lid]

    out: dict[str, Any] = {
        "channel": channel,
        "source": source,
        "total_leads_requested": len(lead_ids_list),
        "total_leads_covered": 0,
        "unique_phones_checked": 0,
        "cost_usd": 0.0,
        "allowed_leads": [],
        "blocked_leads": [],
        "unknown_leads": [],
        "no_phone_leads": [],
        "per_phone_results": [],
        "error": None,
    }

    if not lead_ids_list:
        return out

    if channel not in dnc_check.CHANNELS:
        out["error"] = f"invalid channel: {channel}"
        # Everything is blocked, fail-closed
        out["blocked_leads"] = [
            {"lead_id": lid, "reason": "invalid_channel",
             "result_source": "error"}
            for lid in lead_ids_list
        ]
        return out

    # For channels that aren't phone-based, skip the dedup and just call
    # check_lead_for_channel individually (it's still O(n) but each call is
    # cheap because no Searchbug).
    if channel in ("email", "mail"):
        try:
            for lid in lead_ids_list:
                r = await dnc_check.check_lead_for_channel(lid, channel, source)
                out["cost_usd"] += float(r.cost_usd or 0.0)
                out["total_leads_covered"] += 1
                if r.is_blocked:
                    out["blocked_leads"].append({
                        "lead_id": lid,
                        "reason": r.reason,
                        "result_source": r.result_source,
                    })
                else:
                    out["allowed_leads"].append(lid)
        except Exception as e:
            logger.error("dnc_batch: non-phone channel loop failed: %s", e)
            out["error"] = str(e)
        return out

    # Phone-based channels (sms, voice) — dedup pipeline
    try:
        db = get_db()
    except Exception as e:
        out["error"] = f"db_unavailable: {e}"
        out["blocked_leads"] = [
            {"lead_id": lid, "reason": "db_unavailable",
             "result_source": "error"}
            for lid in lead_ids_list
        ]
        return out

    rows = _fetch_leads_phones(db, lead_ids_list)
    groups, no_phone, unknown = _group_by_phone(lead_ids_list, rows)

    # Leads with no phone → blocked for phone-based channels
    for lid in no_phone:
        out["no_phone_leads"].append(lid)
        out["blocked_leads"].append({
            "lead_id": lid,
            "reason": "no_valid_phone_on_lead",
            "result_source": "skipped",
        })
    out["total_leads_covered"] += len(no_phone)

    # Leads we couldn't even find
    for lid in unknown:
        out["unknown_leads"].append(lid)
        out["blocked_leads"].append({
            "lead_id": lid,
            "reason": "lead_not_found",
            "result_source": "error",
        })
    out["total_leads_covered"] += len(unknown)

    # Run ONE check per unique phone
    out["unique_phones_checked"] = len(groups)
    for phone_e164, group_lead_ids in groups.items():
        # Use the first lead_id in the group as the representative
        rep_lead_id = group_lead_ids[0]
        try:
            result = await dnc_check.check_lead_for_channel(
                rep_lead_id, channel, source
            )
        except Exception as e:
            logger.error("dnc_batch: check_lead_for_channel raised for %s: %s",
                         rep_lead_id, e)
            # Treat as blocked, fail-closed
            for lid in group_lead_ids:
                out["blocked_leads"].append({
                    "lead_id": lid,
                    "reason": "check_failed",
                    "result_source": "error",
                })
                out["total_leads_covered"] += 1
            out["per_phone_results"].append({
                "phone": phone_e164,
                "lead_ids": group_lead_ids,
                "result": {"error": str(e), "is_blocked": True},
            })
            continue

        out["cost_usd"] += float(result.cost_usd or 0.0)
        out["per_phone_results"].append({
            "phone": phone_e164,
            "lead_ids": group_lead_ids,
            "result": result.to_dict(),
        })

        # Fan out the verdict to all leads in this group
        for lid in group_lead_ids:
            out["total_leads_covered"] += 1
            if result.is_blocked:
                out["blocked_leads"].append({
                    "lead_id": lid,
                    "reason": result.reason,
                    "result_source": result.result_source,
                })
            else:
                out["allowed_leads"].append(lid)

        # For OTHER leads in the group (non-representative), also persist the
        # channel result onto their leads.dnc_channel_results so the audit
        # trail is consistent across the whole group, not just the rep.
        if len(group_lead_ids) > 1:
            for lid in group_lead_ids[1:]:
                try:
                    fanout_result = dnc_check.ChannelCheckResult(
                        lead_id=lid,
                        channel=channel,
                        is_blocked=result.is_blocked,
                        reason=result.reason,
                        result_source=result.result_source,
                        cost_usd=0.0,  # cost was already charged on rep
                        phone_normalized=result.phone_normalized,
                        line_type=result.line_type,
                        carrier=result.carrier,
                        metadata={
                            **(result.metadata or {}),
                            "fanned_out_from_lead_id": rep_lead_id,
                        },
                    )
                    dnc_check._persist_lead_channel_result(
                        db, lid, channel, fanout_result
                    )
                except Exception as e:
                    logger.warning(
                        "dnc_batch: fan-out persist failed for %s: %s",
                        lid, e,
                    )

    return out
