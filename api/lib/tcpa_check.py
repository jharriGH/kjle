"""
KJLE — TCPA litigator lookup helper
File: api/lib/tcpa_check.py

Tiny single-purpose module. PK lookup against tcpa_litigators — no Searchbug,
no cache, no network. Sits between the internal suppression check and the
soft-TTL cache in api/routes/dnc.py::_perform_check (Phase 4 Layer 3 Slice 3A,
Section 3.4 of the design doc).

Litigator status overrides the cache by design: if a phone enters the list
after we cached a "clean" result, the pre-check still fires and we don't
accidentally dial.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def is_tcpa_litigator(phone_e164: str, db) -> dict:
    """
    Check if a phone matches the TCPA litigator list.

    Args:
        phone_e164 — already-normalized E.164 US phone (+1 + 10 digits).
                     Caller is responsible for normalization; we don't re-normalize
                     to avoid silently masking upstream bugs.
        db         — Supabase client (passed in so the route can reuse its handle
                     and tests can pass a mock).

    Returns:
        {
          "is_litigator": bool,
          "matched_row":  dict | None,   # full row if matched, None otherwise
        }

    Read failures are not fatal: the function logs a warning and returns
    is_litigator=False so the request pipeline can fall through to the cache
    + provider rather than 500. Pre-check is a hardening layer, not a
    correctness gate.
    """
    if not phone_e164:
        return {"is_litigator": False, "matched_row": None}

    try:
        res = (
            db.table("tcpa_litigators")
            .select("*")
            .eq("phone", phone_e164)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.warning(f"tcpa_check: tcpa_litigators read failed for {phone_e164}: {e}")
        return {"is_litigator": False, "matched_row": None}

    rows = res.data or []
    if not rows:
        return {"is_litigator": False, "matched_row": None}

    return {"is_litigator": True, "matched_row": rows[0]}
