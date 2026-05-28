"""
KJLE — Carrier prefix lookup (NANPA-derived)
Phase 4 Layer 1, Slice 1B.

Free line-type / carrier detection by looking up the NPA-NXX-X (thousands-block,
7 chars) of a phone number in the nanpa_carrier_prefixes table.

Used by the channel-aware DNC pipeline to skip paid Searchbug calls when we can
classify the line type for free from public NANPA data.

Falls back to NPA-NXX (6 chars) lookup with block='0' when the exact thousands-
block isn't in the DB. Returns None if nothing matches — caller decides whether
to pay for Searchbug.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Optional

from ..database import get_db

logger = logging.getLogger(__name__)

# E.164 US/Canada phone: +1 followed by 10 digits
_E164_US = re.compile(r"^\+1(\d{10})$")


def _extract_npa_nxx_x(phone_e164: str) -> Optional[tuple[str, str]]:
    """
    Extract (npa_nxx_x, npa_nxx) from an E.164 US/Canada phone.

    Returns:
        ('2125550', '212555') for +12125550000
        None if phone is not a valid +1 E.164 number
    """
    if not phone_e164:
        return None
    m = _E164_US.match(phone_e164.strip())
    if not m:
        return None
    digits = m.group(1)  # 10 digits
    npa = digits[0:3]
    nxx = digits[3:6]
    block = digits[6:7]  # the first digit of the last 4 = thousands-block
    return (f"{npa}{nxx}{block}", f"{npa}{nxx}")


@lru_cache(maxsize=2048)
def _lookup_exact_cached(npa_nxx_x: str) -> Optional[dict]:
    """
    Cached point lookup by 7-char primary key.

    Returns dict with keys: carrier, line_type, ocn, state, region, status
    Returns None on miss.

    NB: lru_cache is per-process; on a multi-worker uvicorn the cache won't be
    shared across workers, but each worker still gets the same DB. This is fine
    for our access pattern (a few thousand unique prefixes covering the bulk of
    real-world traffic).
    """
    try:
        db = get_db()
    except Exception as e:
        logger.warning("carrier_lookup: get_db() failed: %s", e)
        return None

    try:
        resp = (
            db.table("nanpa_carrier_prefixes")
            .select("carrier,line_type,ocn,state,region,status")
            .eq("npa_nxx_x", npa_nxx_x)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.warning("carrier_lookup: query failed for %s: %s", npa_nxx_x, e)
        return None

    rows = getattr(resp, "data", None) or []
    if not rows:
        return None
    return rows[0]


def _lookup_block_zero_fallback(npa_nxx: str) -> Optional[dict]:
    """
    Fallback: try (NPA+NXX+'0') if the exact thousands-block wasn't found.
    Many older non-pooled blocks only have an entry for the '0' block.
    """
    return _lookup_exact_cached(f"{npa_nxx}0")


def lookup_line_type_from_prefix(phone_e164: str) -> Optional[dict]:
    """
    Look up carrier and line_type for a phone number from the NANPA mirror.

    Args:
        phone_e164: phone in +1XXXXXXXXXX format (must already be normalized)

    Returns:
        dict {carrier, line_type, ocn, state, region, status, source} on match
        None if no match in nanpa_carrier_prefixes (caller decides what to do)

    line_type values: 'mobile' | 'landline' | 'voip' | 'unknown'

    This function NEVER raises. On any error (bad phone, DB down, etc.) it
    returns None and logs a warning. The caller (channel-aware DNC pipeline)
    treats None as "inconclusive — fall through to next check".
    """
    parts = _extract_npa_nxx_x(phone_e164)
    if not parts:
        return None
    npa_nxx_x, npa_nxx = parts

    # Try exact thousands-block first
    row = _lookup_exact_cached(npa_nxx_x)
    if row:
        return {
            "carrier": row.get("carrier"),
            "line_type": row.get("line_type") or "unknown",
            "ocn": row.get("ocn"),
            "state": row.get("state"),
            "region": row.get("region"),
            "status": row.get("status"),
            "source": "nanpa_exact",
            "npa_nxx_x": npa_nxx_x,
        }

    # Fallback: block-0 entry
    row = _lookup_block_zero_fallback(npa_nxx)
    if row:
        return {
            "carrier": row.get("carrier"),
            "line_type": row.get("line_type") or "unknown",
            "ocn": row.get("ocn"),
            "state": row.get("state"),
            "region": row.get("region"),
            "status": row.get("status"),
            "source": "nanpa_block_zero_fallback",
            "npa_nxx_x": f"{npa_nxx}0",
        }

    return None


# ---------------------------------------------------------------------------
# OCN / carrier-name heuristic for ETL classification (v3 — 2026-05-27)
# ---------------------------------------------------------------------------
# NANPA Thousands-Block Assignment data does NOT include a direct line_type
# field. The ETL job that imports NANPA must derive line_type from the
# carrier-name patterns below. This classifier is exposed here so the import
# script uses the same patterns the lookup helpers expect.
#
# v3 (2026-05-27): tightened after dry-run against real NANPA file.
# Compliance-safe default: returns 'unknown' (defer to Searchbug at
# contact-time) when no pattern matches, rather than guessing landline.
#
# Validated against 1,414,151-row file from reports.nanpa.com:
#   mobile:    37.9%
#   voip:      35.0%
#   landline:  20.5%
#   unknown:    6.7%  (defer to Searchbug — safer than guessing)
# ---------------------------------------------------------------------------

_MOBILE_PATTERNS = (
    " wireless", "wireless ", " mobility", "mobility ", "cellular",
    " pcs,", " pcs ", "t-mobile", "tmobile", "metropcs", "metro by t",
    "us cellular", "cricket ", "boost mobile", "boost subscriber",
    "sprint spectrum", "tracfone", "h2o wireless", "mint mobile",
    "google fi", "visible service", "consumer cellular", "straight talk",
    # Mobile carrier legacy entities (acquired/renamed):
    "new cingular",          # AT&T Mobility legacy
    "omnipoint",             # T-Mobile legacy
    "aerial communications", # T-Mobile legacy
    "cellco partnership",    # Verizon Wireless legal entity
    "powertel",              # Mobile legacy
    "bell atlantic mobile",  # Verizon Wireless predecessor
    "voicestream",           # T-Mobile predecessor
)

_VOIP_PATTERNS = (
    "voip", " voip ", "bandwidth.com", "bandwidth ", "twilio", "vonage",
    " onvoy", "onvoy ", "onvoy,", "inteliquent", "peerless network",
    "peerless ", "level 3", "level3", "voxbone", "8x8", "ringcentral",
    "nextiva", "ooma", "magicjack", "anveo", "callcentric", "telnyx",
    "plivo", "skyswitch", "intermedia", "fusion connect",
    # Internet-protocol naming conventions:
    "ip phone", "ip enabled", " ip ", "ip horizon", "ip telecom",
    # CPaaS / SIP trunking providers:
    "commio", "ymax", "ytel", "flowroute", "thinq", "broadvox",
    "broadvoice", "callfire", "nuso", "csc voice", "hd carrier",
    "core communications",
)

_LANDLINE_PATTERNS = (
    # Bell System ILECs / RBOC operating companies:
    "bellsouth", "southwestern bell", "pacific bell",
    "illinois bell", "michigan bell", "ohio bell", "indiana bell",
    "wisconsin bell", "southern new england tel", "snet",
    " bell tel",   # "X Bell Tel Co"; won't match VoIP IP-something
    # ILECs:
    "qwest", "centurylink", "frontier communications", "frontier north",
    "frontier west", "frontier comm of", "frontier california",
    "frontier florida", "frontier south",
    "windstream", "consolidated communications", "lumen technologies",
    "brightspeed",          # Lumen wireline spinoff
    # Generic telephone-company patterns (won't match VoIP IP-something):
    " telephone ", "telephone co", "telephone company",
    # Verizon WIRELINE entities (NOT "Verizon Wireless" — caught by mobile):
    "verizon new", "verizon north", "verizon south", "verizon penn",
    "verizon maryland", "verizon virginia", "verizon california, inc",
    "verizon washington dc", "verizon access",
    # AT&T WIRELINE (NOT "AT&T Mobility" — caught by mobile rule):
    "at&t corp", "at&t enterprises", "at&t communications of",
    # Older Bell System derivatives / smaller ILECs:
    "ameritech", "u s west", "us west", "embarq", "cincinnati bell",
    "alaska communications", "tds telecom", "tds metrocom", "fairpoint",
    "hawaiian telcom", "ntelos telephone", "intrado communications",
    # CLECs (competitive local exchange carriers — wireline):
    "mcimetro", "paetec", "broadwing", "xo communications", "tw telecom",
    "teleport communications", "global crossing", "telcove",
    "cavalier telephone", "talk america", "mci communications",
    "mci worldcom", "neutral tandem", "neustar", "terra nova",
    "tpx communications", "allstream",
)


def classify_line_type(company, ocn=None):
    """
    Derive line_type from OCN + carrier-name patterns.

    NANPA data lacks an explicit mobile/landline/voip flag, so we infer from
    the company name. v3 patterns validated against 1,414,151 real NANPA rows
    achieve 93.3% classification (6.7% unknown defers to Searchbug).

    Returns one of: 'mobile' | 'voip' | 'landline' | 'unknown'

    COMPLIANCE NOTE: 'unknown' is the safe fallback. Mistakenly classifying
    a mobile number as landline would BLOCK legitimate SMS sends (revenue
    impact); mistakenly classifying a landline as mobile would let SMS go
    to a non-mobile (annoying but not illegal). When in doubt, return
    'unknown' so the channel-aware pipeline falls through to a paid
    Searchbug lookup at contact-time, which is authoritative.
    """
    if not company:
        return "unknown"
    # Pad with spaces so word-boundary tricks work
    name = " " + company.lower() + " "

    for pat in _MOBILE_PATTERNS:
        if pat in name:
            return "mobile"
    for pat in _VOIP_PATTERNS:
        if pat in name:
            return "voip"
    for pat in _LANDLINE_PATTERNS:
        if pat in name:
            return "landline"
    return "unknown"


# ---------------------------------------------------------------------------
# Public helpers for cache management (used by import job)
# ---------------------------------------------------------------------------

def clear_cache() -> None:
    """Clear the in-process lookup cache. Call after a NANPA refresh import."""
    _lookup_exact_cached.cache_clear()


def cache_info() -> dict:
    """Return lru_cache hit/miss stats — useful for /dnc/nanpa/status."""
    info = _lookup_exact_cached.cache_info()
    return {
        "hits": info.hits,
        "misses": info.misses,
        "maxsize": info.maxsize,
        "currsize": info.currsize,
    }
