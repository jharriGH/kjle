"""
KJLE — Searchbug DNC provider implementation
File: api/lib/searchbug_provider.py

Wraps Searchbug's Phone Validation API (TYPE=api_lnd2 — Advanced Phone ID +
DNC/TCPA). Returns DNCResult per the canonical shape in dnc_provider.py.

Docs: https://www.searchbug.com/info/api/phone-validation-api/
Endpoint: POST https://data.searchbug.com/api/search.aspx (form-encoded)
Tier: api_lnd2 — Federal + 13 state DNC, TCPA litigator flag, line type, VoIP
      flag, carrier, OCN, ported, location data, account balance.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from ..config import settings
from .dnc_provider import DNCProvider, DNCResult

logger = logging.getLogger(__name__)

SEARCHBUG_URL  = "https://data.searchbug.com/api/search.aspx"
SEARCHBUG_TYPE = "api_lnd2"
HTTP_TIMEOUT   = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


def _derive_line_type(phone_data: dict) -> str:
    """Map Searchbug's TYPE + VOIP fields to our canonical line_type vocabulary."""
    voip = (phone_data.get("VOIP") or "").strip().upper()
    if voip == "YES":
        return "voip"
    raw_type = (phone_data.get("TYPE") or "").strip().upper()
    if raw_type == "CELLULAR":
        return "mobile"
    if raw_type == "LANDLINE":
        return "landline"
    return "unknown"


def _get_co_code() -> Optional[str]:
    """Read CO_CODE from admin_settings.dnc_searchbug_co_code at call time."""
    try:
        from ..database import get_db
        db = get_db()
        res = db.table("admin_settings").select("value").eq("key", "dnc_searchbug_co_code").execute()
        if res.data and res.data[0].get("value"):
            return str(res.data[0]["value"]).strip()
    except Exception as e:
        logger.warning(f"SearchbugProvider: admin_settings read for co_code failed: {e}")
    return None


class SearchbugProvider(DNCProvider):
    name = "searchbug"

    def is_available(self) -> bool:
        return bool(settings.SEARCHBUG_API_KEY) and bool(_get_co_code())

    async def check_phone(self, phone_e164: str) -> DNCResult:
        api_key = settings.SEARCHBUG_API_KEY
        co_code = _get_co_code()

        if not api_key or not co_code:
            return DNCResult(
                is_dnc   = True,
                reason   = "provider_unavailable",
                provider = self.name,
                error    = "provider_unavailable: missing SEARCHBUG_API_KEY or co_code",
            )

        # Searchbug accepts any format with formatting chars; strip the leading
        # +1 to send a clean 10-digit number.
        f_param = phone_e164.removeprefix("+1") if phone_e164.startswith("+1") else phone_e164

        form = {
            "CO_CODE": co_code,
            "PASS":    api_key,
            "TYPE":    SEARCHBUG_TYPE,
            "F":       f_param,
            "FORMAT":  "JSON",
            "REF":     "kjle-dnc",
        }

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.post(SEARCHBUG_URL, data=form)
        except httpx.TimeoutException:
            return DNCResult(
                is_dnc=True, reason="provider_timeout",
                provider=self.name, error="provider_timeout",
            )
        except Exception as e:
            return DNCResult(
                is_dnc=True, reason="provider_network_error",
                provider=self.name, error=f"provider_network_error: {type(e).__name__}: {e}",
            )

        if r.status_code != 200:
            return DNCResult(
                is_dnc=True, reason="provider_http_error",
                provider=self.name,
                error=f"provider_http_{r.status_code}: {r.text[:200]}",
            )

        try:
            data = r.json()
        except Exception as e:
            return DNCResult(
                is_dnc=True, reason="provider_bad_response",
                provider=self.name,
                error=f"provider_bad_response: not JSON: {e}",
                raw_response={"raw_text": r.text[:500]},
            )

        # Top-level Status / Error envelope
        status = (data.get("Status") or "").strip()
        error  = data.get("Error")
        if status != "Success" or error:
            return DNCResult(
                is_dnc=True, reason="provider_error",
                provider=self.name,
                error=f"provider_error: status={status!r}, error={error!r}",
                raw_response=data,
            )

        phone_data = (data.get("Data") or {}).get("PHONE") or {}

        # DNC: "NO" = clean. Otherwise comma-separated codes (FED, state codes, CPL).
        # Fail-closed if missing/empty.
        dnc_raw  = (phone_data.get("DNC") or "").strip()
        dnc_norm = dnc_raw.upper()
        is_dnc   = dnc_norm != "NO"           # explicit "NO" => clean; everything else => suppress

        # TCPA: only False on explicit "NO".
        tcpa_raw    = (phone_data.get("TCPA") or "").strip().upper()
        tcpa_litig  = tcpa_raw != "NO"

        line_type = _derive_line_type(phone_data)
        carrier   = (phone_data.get("CARRIER") or "").strip()

        return DNCResult(
            is_dnc         = is_dnc,
            reason         = dnc_raw if is_dnc else "clean",
            tcpa_litigator = tcpa_litig,
            line_type      = line_type,
            carrier        = carrier,
            raw_response   = data,
            provider       = self.name,
            error          = None,
        )
