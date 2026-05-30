"""
KJLE — Lead quality classifier (Phase 4 Layer 3 Slice 3B)
File: api/lib/lead_filters.py

Pure-function lead-quality classifier. NO DB access, NO IO. Safe to call from
any code path (ingest, batch tools, future stat endpoints) without coupling
to a DB session.

Used by:
  - api/routes/local_scraper_ingest.py: stamps contactable + filter_reasons
    on each lead before insert.

Component 2 of Phase 4 Layer 3. The carrier-pattern phone classifier lives
in api/lib/phone_filters.py (Component 3, separate module — phone_utils.py
is normalization-only and is in the protected files list).
"""
from __future__ import annotations

import re

# ── Compile all regex once at module load ──────────────────────────────────

_F = re.IGNORECASE

# HARD-BLOCK — permanently / intentionally not contactable
_HARD_BLOCK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(permanently closed|closed for business|closed permanently)\b', _F),
     "permanently_closed"),
    (re.compile(r'\b(no longer in business|out of business|going out of business)\b', _F),
     "out_of_business"),
    # Jim CORRECTION 3 — catches scrape leakage of dead businesses
    # ("Acme Corp, closed since 2019", "Joe's Diner, defunct as of 2021").
    (re.compile(r'\b(closed|defunct|dissolved)\b\s+(in|since|as of)\b', _F),
     "closed_since_dated"),
    # Jim CORRECTION 3 — businesses in transition aren't ready to engage.
    (re.compile(r'\b(under new management|under construction|opening soon)\b', _F),
     "in_transition"),
]

# GOVERNMENT / INSTITUTIONAL — legit org but not a sales target
_GOVERNMENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(department of|dept\.? of|division of|bureau of)\b', _F),
     "government_dept"),
    # \w+(?:\s+\w+)* lets multi-word city names ("Long Beach") sit between
    # the prefix ("city of") and the suffix ("hall"/"office").
    (re.compile(r'\b(city of|county of|state of|town of)\s+\w+(?:\s+\w+)*\s+(hall|office)\b', _F),
     "government_office"),
    (re.compile(r'\b(public school|high school|elementary school|middle school|community college|university)\b', _F),
     "educational_institution"),
    (re.compile(r'\bUSPS\b|\bpost office\b|\bpostmaster\b', _F),
     "postal_service"),
    (re.compile(r'\b(fire|police|sheriff)\s+(department|dept|station)\b', _F),
     "emergency_services"),
    (re.compile(r'\b(library|courthouse|jail|prison|correctional)\b', _F),
     "government_facility"),
]

# CORPORATE HQ — wrong type of contact for SMB outreach. SOFT — does NOT
# flip contactable to false (the lead is still reachable, just at a parent
# org HQ). Records the hit so analytics can show "this is a corporate office."
_CORPORATE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bcorporate\s+(office|hq|headquarters)\b', _F),
     "corporate_hq"),
    (re.compile(r'\bregional\s+(office|headquarters)\b', _F),
     "regional_office"),
    (re.compile(r'\bworld\s+headquarters\b', _F),
     "world_hq"),
]

# TEST / DUMMY — matched against business_name only. A legit description
# ("we offer free samples") shouldn't trip the flag; only obviously-bogus
# business names should.
_TEST_DATA_PATTERN = re.compile(
    r'\b(test|sample|dummy|placeholder|do not use|fake)\b', _F
)
_TEST_DATA_EXACT_NAMES = {"john doe", "jane doe"}
_TEST_DATA_XXX = re.compile(r'^x{3,}$', _F)


def _build_haystack(lead: dict) -> str:
    parts: list[str] = []
    bn = lead.get("business_name")
    if bn:
        parts.append(str(bn))
    desc = lead.get("description")
    if desc:
        parts.append(str(desc))
    cats = lead.get("categories")
    if isinstance(cats, list):
        for c in cats:
            if c:
                parts.append(str(c))
    elif isinstance(cats, str) and cats:
        parts.append(cats)
    return " ".join(parts).lower()


def classify_lead_quality(lead: dict | None) -> dict:
    """
    Classify a lead's contactability based on its business name + description
    + category list.

    Args:
        lead: dict with at least 'business_name' key. May also include
              'address', 'city', 'state', 'categories' (list[str]),
              'description'.

    Returns:
        {
          "contactable": bool,
          "filter_hits": [str],   # which reason tokens fired (debug + audit)
          "reasons":     [str],   # human-readable reasons (for filter_reasons col)
        }

    Corporate-HQ matches are recorded as 'filter_hits' but do NOT flip
    contactable to false — they're informational, not blocking.

    Empty / None input → {"contactable": True, "filter_hits": [], "reasons": []}
    (don't punish missing data).
    """
    if not lead or not isinstance(lead, dict):
        return {"contactable": True, "filter_hits": [], "reasons": []}

    haystack = _build_haystack(lead)
    if not haystack.strip():
        return {"contactable": True, "filter_hits": [], "reasons": []}

    hits: list[str] = []
    blocking = False

    for pat, reason in _HARD_BLOCK_PATTERNS:
        if pat.search(haystack):
            hits.append(reason)
            blocking = True

    for pat, reason in _GOVERNMENT_PATTERNS:
        if pat.search(haystack):
            hits.append(reason)
            blocking = True

    # Corporate HQ — informational only, does NOT set blocking.
    for pat, reason in _CORPORATE_PATTERNS:
        if pat.search(haystack):
            hits.append(reason)

    # Test / dummy data — business_name only.
    raw_name = lead.get("business_name") or ""
    name_norm = str(raw_name).strip().lower()
    if name_norm:
        if _TEST_DATA_PATTERN.search(name_norm):
            hits.append("test_data")
            blocking = True
        elif name_norm in _TEST_DATA_EXACT_NAMES:
            hits.append("test_data")
            blocking = True
        elif _TEST_DATA_XXX.match(name_norm):
            hits.append("test_data")
            blocking = True

    # Dedupe while preserving first-seen order.
    seen: set[str] = set()
    deduped: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            deduped.append(h)

    return {
        "contactable": not blocking,
        "filter_hits": deduped,
        "reasons":     list(deduped),
    }


if __name__ == "__main__":
    cases: list[tuple[dict, bool, list[str]]] = [
        ({"business_name": "Joe's HVAC"},                                                True,  []),
        ({"business_name": "City of Long Beach Hall"},                                   False, ["government_office"]),
        ({"business_name": "Acme Corp - Permanently Closed"},                            False, ["permanently_closed"]),
        ({"business_name": "Test Data"},                                                 False, ["test_data"]),
        ({"business_name": "Smith's Plumbing", "description": "Under new management"},   False, ["in_transition"]),
        ({"business_name": "Mike's Auto Repair, closed since 2019"},                     False, ["closed_since_dated"]),
    ]
    fails = 0
    for lead, want_contactable, want_reasons in cases:
        got = classify_lead_quality(lead)
        ok = (got["contactable"] == want_contactable
              and set(want_reasons).issubset(set(got["reasons"])))
        mark = "OK" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"[{mark}] {lead} -> {got}")
    print(f"{len(cases) - fails}/{len(cases)} passed")
