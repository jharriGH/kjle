"""
KJLE — RealValidito DNC provider implementation
File: api/lib/realvalidito_provider.py

Wraps RealValidito's DNC lookup API.

Endpoint: POST https://app.realvalidito.com/dnclookup/validate
Body:     {api_key, api_secret, numbers: ["5551234567"]}   # bare 10-digit US

Notes on response shape:
    The live response shape is UNCONFIRMED — an empty-input test returned
    no `federal_dnc` field. We parse defensively: tolerate the bucket field
    being named differently or absent, and logger.info() the top-level
    keys on every call so the true shape is captured in production logs.

Tight httpx timeout (connect=5s, read=8s) so a hung provider call can
never stall a Render worker.

Fail-closed: error set => is_dnc=True. Mirrors SearchbugProvider semantics.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import httpx

from .dnc_provider import DNCProvider, DNCResult

logger = logging.getLogger(__name__)

REALVALIDITO_URL = "https://app.realvalidito.com/dnclookup/validate"
HTTP_TIMEOUT     = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)

_DIGITS = re.compile(r"\d")


def _to_ten_digits(phone_e164: str) -> str:
    """Strip to bare 10 US digits (drop leading country code / +1)."""
    digits = "".join(_DIGITS.findall(phone_e164 or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _only_digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _bucket_contains(bucket: Any, ten_digits: str) -> bool:
    """
    Defensive containment check.  The bucket may be:
      - a list of strings  (["5551234567", ...])
      - a list of dicts    ([{"number":"5551234567", ...}, ...])
      - a dict keyed by number ({"5551234567": {...}})
      - missing / None
    Return True if `ten_digits` appears anywhere recognisable.
    """
    if not ten_digits or bucket is None:
        return False
    if isinstance(bucket, list):
        for item in bucket:
            if isinstance(item, str):
                if ten_digits in _only_digits(item):
                    return True
            elif isinstance(item, dict):
                for v in item.values():
                    if isinstance(v, str) and ten_digits in _only_digits(v):
                        return True
        return False
    if isinstance(bucket, dict):
        if ten_digits in bucket:
            return True
        for v in bucket.values():
            if isinstance(v, str) and ten_digits in _only_digits(v):
                return True
        return False
    if isinstance(bucket, str):
        return ten_digits in _only_digits(bucket)
    return False


def _find_bucket(data: dict, *candidates: str) -> Optional[Any]:
    """
    Locate a bucket by trying multiple known/likely field names.  Tolerates
    the field being absent (returns None) or under a different case.
    """
    if not isinstance(data, dict):
        return None
    for name in candidates:
        if name in data:
            return data[name]
    lower = {k.lower(): v for k, v in data.items() if isinstance(k, str)}
    for name in candidates:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


class RealValiditoProvider(DNCProvider):
    name = "realvalidito"

    def is_available(self) -> bool:
        return bool(os.getenv("REALVALIDITO_API_KEY")) and bool(os.getenv("REALVALIDITO_API_SECRET"))

    async def check_phone(self, phone_e164: str) -> DNCResult:
        api_key    = os.getenv("REALVALIDITO_API_KEY")
        api_secret = os.getenv("REALVALIDITO_API_SECRET")

        if not api_key or not api_secret:
            return DNCResult(
                is_dnc   = True,
                reason   = "provider_unavailable",
                provider = self.name,
                error    = "provider_unavailable: missing REALVALIDITO_API_KEY or REALVALIDITO_API_SECRET",
            )

        ten = _to_ten_digits(phone_e164)
        if len(ten) != 10:
            return DNCResult(
                is_dnc   = True,
                reason   = "provider_bad_input",
                provider = self.name,
                error    = f"provider_bad_input: could not extract 10 digits from {phone_e164!r}",
            )

        payload = {
            "api_key":    api_key,
            "api_secret": api_secret,
            "numbers":    [ten],
        }

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
                r = await client.post(REALVALIDITO_URL, json=payload)
        except httpx.TimeoutException:
            return DNCResult(
                is_dnc=True, reason="provider_timeout",
                provider=self.name, error="provider_timeout",
            )
        except Exception as e:
            return DNCResult(
                is_dnc=True, reason="provider_network_error",
                provider=self.name,
                error=f"provider_network_error: {type(e).__name__}: {e}",
            )

        if r.status_code in (401, 403):
            return DNCResult(
                is_dnc=True, reason="provider_auth_denied",
                provider=self.name,
                error=f"provider_http_{r.status_code}: {r.text[:200]}",
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

        # Capture the true shape in logs on every call until we lock it down.
        # Empty-input probe returned no `federal_dnc` field, so don't trust
        # convention — let production traffic teach us.
        try:
            if isinstance(data, dict):
                top_keys = sorted(data.keys())
            else:
                top_keys = type(data).__name__
            logger.info(
                "RealValiditoProvider: response top-level keys for ***%s: %s",
                ten[-4:], top_keys,
            )
        except Exception:
            pass

        if not isinstance(data, dict):
            return DNCResult(
                is_dnc=True, reason="provider_bad_response",
                provider=self.name,
                error=f"provider_bad_response: response was {type(data).__name__}",
                raw_response={"raw_text": r.text[:500]},
            )

        federal_bucket = _find_bucket(
            data, "federal_dnc", "federalDnc", "FederalDnc",
            "federal", "dnc_federal",
        )
        tcpa_bucket = _find_bucket(
            data, "tcpa_litigator", "tcpaLitigator", "TcpaLitigator",
            "tcpa", "tcpa_litigators", "litigators",
        )
        invalid_bucket = _find_bucket(
            data, "invalid", "invalid_numbers", "invalidNumbers", "Invalid",
        )

        # Invalid → treat as error and fail-closed (per spec).
        if _bucket_contains(invalid_bucket, ten):
            return DNCResult(
                is_dnc=True, reason="provider_invalid_number",
                provider=self.name,
                error="provider_invalid_number: number in invalid bucket",
                raw_response=data,
            )

        is_dnc = _bucket_contains(federal_bucket, ten)
        tcpa   = _bucket_contains(tcpa_bucket, ten)

        return DNCResult(
            is_dnc         = is_dnc,
            reason         = "federal_dnc" if is_dnc else "clean",
            tcpa_litigator = tcpa,
            line_type      = "unknown",
            carrier        = "",
            raw_response   = data,
            provider       = self.name,
            error          = None,
        )
