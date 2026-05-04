"""
KJLE — Phone normalization helpers (US-only, regex-based)
File: api/lib/phone_utils.py

No external deps. Used by the DNC subsystem to ensure cache keys and
suppression list keys all share one canonical form: E.164 (+1XXXXXXXXXX).
"""
from __future__ import annotations

import re
from typing import Optional

# Accepts only E.164 US: +1 followed by 10 digits.
_E164_US_RE = re.compile(r"^\+1\d{10}$")


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """
    Normalize a US phone string to E.164 (+1XXXXXXXXXX).

    Accepts a wide variety of input formats:
        "(555) 123-4567"   -> "+15551234567"
        "555-123-4567"     -> "+15551234567"
        "5551234567"       -> "+15551234567"
        "1-555-123-4567"   -> "+15551234567"
        "+15551234567"     -> "+15551234567"
        "  +1 555.123.4567 " -> "+15551234567"

    Returns None for:
        - None / empty / whitespace
        - non-US country codes (anything other than leading 1 in the 11-digit case)
        - too-short (<10 digits) or too-long (>11 digits) inputs
        - inputs containing letters
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # Strip everything that isn't a digit. Letters anywhere = invalid.
    if re.search(r"[A-Za-z]", s):
        return None
    digits = re.sub(r"\D", "", s)

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    elif len(digits) == 10:
        pass
    else:
        return None

    return f"+1{digits}"


def is_valid_us_phone(phone: Optional[str]) -> bool:
    """True iff phone is already in canonical E.164 US form (+1 + 10 digits)."""
    if not phone:
        return False
    return bool(_E164_US_RE.match(phone))
