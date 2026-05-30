"""
KJLE — Phone carrier-pattern classifier (Phase 4 Layer 3 Slice 3B)
File: api/lib/phone_filters.py

Pure-function phone-pattern classifier. NO DB access, NO IO. Safe to call
from any code path.

SEPARATE from api/lib/phone_utils.py (which is normalization-only —
phone_utils.py is in the Slice 3B protected files list). This module
operates on the OUTPUT of phone_utils.normalize_phone(): a fully-normalized
US E.164 string like '+15551234567'.

Used by:
  - api/routes/dnc.py: short-circuit /dnc/check on carrier-pattern garbage
    BEFORE any DB work (cheapest deterministic guard runs first).
  - api/routes/local_scraper_ingest.py: stamp contactable=false on incoming
    leads whose phone is structurally unreachable.

Component 3 of Phase 4 Layer 3. The leadcrap (business-name) classifier
lives in api/lib/lead_filters.py (Component 2).
"""
from __future__ import annotations

import re

# ── 555 numbers ──────────────────────────────────────────────────────────────
# NPA-555-0100 through NPA-555-0199 are the ONLY assignable 555 numbers
# (reserved for fictional/test use in entertainment/media). Everything else
# in 555-XXXX is unallocated and unreachable.
#
# The regex blocks ALL 555 numbers including 0100-0199 — they're test-use
# only, never legitimate sales targets. Two branches:
#   1. NPA = 555 (not allocated by NANP — never a real phone)
#   2. NXX = 555 in any NPA (the classic fictional-number exchange)
_PATTERN_555 = re.compile(r'^\+1(555\d{7}|\d{3}555\d{4})$')

# ── Sequential / canonical-test patterns ────────────────────────────────────
_PATTERN_1234567  = re.compile(r'^\+1\d{3}1234567$')
_PATTERN_ALLZEROS = re.compile(r'^\+1\d{3}0000000$')
_PATTERN_ALLNINES = re.compile(r'^\+1\d{3}9999999$')

# ── Toll-free NPAs ──────────────────────────────────────────────────────────
# KJLE is a cold-outbound product — we never legitimately dial toll-free.
# Set membership is faster than regex alternation and the list is short.
# 822 IS included (Jim CORRECTION 2 — eight NPAs total, not seven).
_TOLL_FREE_NPAS: frozenset[str] = frozenset({
    "800", "822", "833", "844", "855", "866", "877", "888",
})


def classify_phone_quality(
    phone_e164: str | None,
    carrier_info: dict | None = None,
) -> dict:
    """
    Classify a normalized E.164 phone's contactability based on structural
    patterns: NPA-555 test range, all-same-digit subscriber portion, canonical
    sequential test numbers (1234567/0000000/9999999), and the toll-free NPA
    block.

    Args:
        phone_e164: E.164 phone like '+15551234567'. Caller is responsible
                    for running normalize_phone() first — this function does
                    NOT re-normalize. None / empty returns a contactable=True
                    no-op so callers can pipe optional phones straight through.
        carrier_info: optional dict from a future carrier_lookup hook —
                      reserved, not consumed in this slice.

    Returns:
        {
          "contactable":   bool,
          "pattern_hits":  [str],  # token tags (debug + audit)
          "reasons":       [str],  # human-readable reasons (for filter_reasons col)
        }

    All-same-digit check: digits 5-11 of E.164 (the 7 subscriber digits, i.e.
    NXX-XXXX). If every digit is identical, flag 'all_same_digit'.
    """
    if not phone_e164:
        return {"contactable": True, "pattern_hits": [], "reasons": []}

    phone = str(phone_e164)
    hits: list[str] = []
    blocking = False

    # 555 — blocks every NPA-555-XXXX number regardless of NPA. Critical:
    # the test case '+13105551234' (310 NPA, 555 exchange) MUST be blocked.
    if _PATTERN_555.match(phone):
        hits.append("test_number_555")
        blocking = True

    # Canonical sequential test patterns
    if _PATTERN_1234567.match(phone):
        hits.append("sequential_1234567")
        blocking = True
    if _PATTERN_ALLZEROS.match(phone):
        hits.append("all_zeros")
        blocking = True
    if _PATTERN_ALLNINES.match(phone):
        hits.append("all_nines")
        blocking = True

    # All-same-digit subscriber portion. E.164 layout: '+', '1', NPA(3),
    # NXX(3), XXXX(4). Subscriber portion = digits 5..11 (NXX-XXXX, 7 digits).
    if len(phone) == 12 and phone.startswith("+1"):
        subscriber = phone[5:12]
        if subscriber.isdigit() and len(set(subscriber)) == 1:
            hits.append("all_same_digit")
            blocking = True

    # Toll-free NPA (Jim CORRECTION 2 — 822 included).
    if len(phone) >= 5 and phone.startswith("+1"):
        npa = phone[2:5]
        if npa in _TOLL_FREE_NPAS:
            hits.append("toll_free_npa")
            blocking = True

    # Dedupe while preserving first-seen order (defensive; patterns above
    # are disjoint today, but future additions could overlap).
    seen: set[str] = set()
    deduped: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            deduped.append(h)

    return {
        "contactable":  not blocking,
        "pattern_hits": deduped,
        "reasons":      list(deduped),
    }


if __name__ == "__main__":
    cases: list[tuple[str, bool, list[str]]] = [
        ("+15551234567",  False, ["test_number_555"]),
        ("+18005551111",  False, ["test_number_555", "toll_free_npa"]),
        ("+18221234567",  False, ["toll_free_npa"]),
        ("+13105551234",  False, ["test_number_555"]),
        ("+13109876543",  True,  []),
        ("+18000000000",  False, ["all_zeros", "toll_free_npa"]),
    ]
    fails = 0
    for phone, want_contactable, want_reasons in cases:
        got = classify_phone_quality(phone)
        ok = (got["contactable"] == want_contactable
              and set(want_reasons).issubset(set(got["reasons"])))
        mark = "OK" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"[{mark}] {phone} -> {got}")
    print(f"{len(cases) - fails}/{len(cases)} passed")
