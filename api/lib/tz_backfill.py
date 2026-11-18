"""
KJLE — IANA timezone backfill for leads.

Sets leads.timezone (IANA string, e.g. "America/New_York") for every lead
with a US state using a three-tier resolution:
  1. Single-tz states: direct state → IANA lookup.
  2. Split-tz states: longitude boundary if lat/lng present, then
     zip-prefix table for well-known minority zones, then state majority.
  3. Unresolvable: skip (non-US or state not in map).

Idempotent: only processes rows WHERE (timezone IS NULL OR timezone = '').
Chunked at ~20k rows/batch. Grouped UPDATEs by tz (one UPDATE per tz per
chunk) keeps statement count low.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Timezone maps ─────────────────────────────────────────────────────────────

# State majority IANA (also used as split-tz fallback)
STATE_TZ: dict[str, str] = {
    "AK": "America/Anchorage",
    "AL": "America/Chicago",
    "AR": "America/Chicago",
    "AZ": "America/Phoenix",    # no DST
    "CA": "America/Los_Angeles",
    "CO": "America/Denver",
    "CT": "America/New_York",
    "DC": "America/New_York",
    "DE": "America/New_York",
    "GA": "America/New_York",
    "HI": "Pacific/Honolulu",
    "IL": "America/Chicago",
    "IA": "America/Chicago",
    "LA": "America/Chicago",
    "MA": "America/New_York",
    "MD": "America/New_York",
    "ME": "America/New_York",
    "MN": "America/Chicago",
    "MO": "America/Chicago",
    "MS": "America/Chicago",
    "MT": "America/Denver",
    "NC": "America/New_York",
    "NH": "America/New_York",
    "NJ": "America/New_York",
    "NM": "America/Denver",
    "NV": "America/Los_Angeles",
    "NY": "America/New_York",
    "OH": "America/New_York",
    "OK": "America/Chicago",
    "PA": "America/New_York",
    "RI": "America/New_York",
    "SC": "America/New_York",
    "UT": "America/Denver",
    "VA": "America/New_York",
    "VT": "America/New_York",
    "WA": "America/Los_Angeles",
    "WI": "America/Chicago",
    "WV": "America/New_York",
    "WY": "America/Denver",
    # Split states — majority (fallback)
    "FL": "America/New_York",   # most of FL is ET
    "TX": "America/Chicago",    # most of TX is CT
    "KS": "America/Chicago",    # most of KS is CT
    "NE": "America/Chicago",    # most of NE is CT
    "ND": "America/Chicago",    # most of ND is CT
    "SD": "America/Chicago",    # most of SD is CT
    "KY": "America/New_York",   # most of KY is ET
    "TN": "America/Chicago",    # slight majority CT (Nashville, Memphis)
    "IN": "America/New_York",   # all of IN = ET since 2006 (a few CT border areas)
    "MI": "America/New_York",   # most of MI (LP) = ET
    "OR": "America/Los_Angeles", # most of OR = PT
    "ID": "America/Denver",     # most of ID pop is MT (Boise/Twin Falls)
}

# Split states: (longitude_threshold, west_tz, east_tz)
# If lng < threshold → west_tz; else → east_tz
SPLIT_BY_LNG: dict[str, tuple[float, str, str]] = {
    # FL panhandle (Pensacola) is CT; rest is ET
    "FL": (-86.5,  "America/Chicago",     "America/New_York"),
    # TX El Paso / far west is MT; rest is CT
    "TX": (-104.5, "America/Denver",      "America/Chicago"),
    # KS tiny western strip is MT; rest CT
    "KS": (-101.5, "America/Denver",      "America/Chicago"),
    # NE panhandle is MT; rest CT
    "NE": (-103.0, "America/Denver",      "America/Chicago"),
    # ND far west is MT; rest CT
    "ND": (-103.5, "America/Denver",      "America/Chicago"),
    # SD Black Hills / far west is MT; rest CT
    "SD": (-103.5, "America/Denver",      "America/Chicago"),
    # KY western tip (Paducah area) is CT; rest ET
    "KY": (-88.0,  "America/Chicago",     "America/New_York"),
    # TN Memphis/Nashville area is CT; Knoxville/Chattanooga is ET
    "TN": (-88.5,  "America/Chicago",     "America/New_York"),
    # IN far northwest (Gary/Hammond) is CT; rest ET
    "IN": (-87.4,  "America/Chicago",     "America/New_York"),
    # MI Upper Peninsula western end is CT; rest (LP) ET
    "MI": (-87.0,  "America/Chicago",     "America/New_York"),
    # OR Malheur County (far east) is MT; rest PT
    "OR": (-117.5, "America/Denver",      "America/Los_Angeles"),
    # ID Northern panhandle is PT; Southern ID is MT
    "ID": (-114.0, "America/Los_Angeles", "America/Denver"),
}

# Well-known zip3 prefix → IANA overrides for split states
# (supplement when lng is unavailable)
ZIP3_TZ: dict[str, str] = {
    # SD Black Hills / Rapid City area
    "577": "America/Denver",
    "578": "America/Denver",
    # ID panhandle (Coeur d'Alene / Sandpoint)
    "838": "America/Los_Angeles",
    # OR Malheur County (Ontario OR)
    "979": "America/Denver",
    # MI Upper Peninsula western
    "498": "America/Chicago",
    "499": "America/Chicago",
    # NE panhandle (Chadron / Scottsbluff)
    "693": "America/Denver",
    "692": "America/Denver",
    # TX far west (El Paso)
    "799": "America/Denver",
    "798": "America/Denver",
}


def _get_iana_tz(
    state: Optional[str],
    zip5: Optional[str],
    lng: Optional[float],
) -> Optional[str]:
    """
    Derive IANA tz from state + optional zip and longitude.
    Returns None for non-US or unmappable states.
    """
    state = (state or "").strip().upper()
    if not state or state not in STATE_TZ:
        return None

    # Single-tz state: direct lookup
    if state not in SPLIT_BY_LNG:
        return STATE_TZ[state]

    # Split-tz state — try zip prefix first (cheap)
    if zip5 and len(zip5) >= 3:
        z3 = zip5[:3]
        if z3 in ZIP3_TZ:
            return ZIP3_TZ[z3]

    # Then longitude
    if lng is not None:
        try:
            lng_f = float(lng)
            threshold, west_tz, east_tz = SPLIT_BY_LNG[state]
            return west_tz if lng_f < threshold else east_tz
        except (TypeError, ValueError):
            pass

    # Fallback: state majority
    return STATE_TZ[state]


# ── Backfill runner ───────────────────────────────────────────────────────────

CHUNK_SIZE = 20_000
_SELECT_COLS = "id, state, zip, lng"


def run_tz_backfill_chunk(db, chunk_size: int = CHUNK_SIZE) -> dict:
    """
    Process one chunk of unset-timezone leads. Returns a stats dict.
    Caller is responsible for iterating until 'remaining' == 0 or 'assigned' == 0.
    """
    t0 = datetime.now(timezone.utc)

    # Count remaining (uses idx_leads_tz_backfill partial index if built)
    try:
        count_res = (
            db.table("leads")
            .select("id", count="exact")
            .eq("is_active", True)
            .not_.is_("state", "null")
            .or_("timezone.is.null,timezone.eq.")
            .limit(1)
            .execute()
        )
        remaining_before = count_res.count or 0
    except Exception as exc:
        logger.warning(f"[tz_backfill] count query failed: {exc}")
        remaining_before = -1

    # Fetch chunk
    try:
        rows = (
            db.table("leads")
            .select(_SELECT_COLS)
            .eq("is_active", True)
            .not_.is_("state", "null")
            .or_("timezone.is.null,timezone.eq.")
            .order("id")
            .limit(chunk_size)
            .execute()
            .data or []
        )
    except Exception as exc:
        logger.error(f"[tz_backfill] fetch failed: {exc}")
        return {"assigned": 0, "skipped": 0, "remaining": remaining_before, "error": str(exc)}

    if not rows:
        return {"assigned": 0, "skipped": 0, "remaining": 0}

    # Classify each row
    tz_groups: dict[str, list[str]] = {}  # iana_tz → [lead_id, ...]
    skipped = 0
    for row in rows:
        iana = _get_iana_tz(
            row.get("state"),
            row.get("zip"),
            row.get("lng"),
        )
        if iana:
            tz_groups.setdefault(iana, []).append(row["id"])
        else:
            skipped += 1

    # Batch-update grouped by tz
    assigned = 0
    update_errors = 0
    UPDATE_CHUNK = 500  # ids per UPDATE .in_() call
    for iana_tz, ids in tz_groups.items():
        for i in range(0, len(ids), UPDATE_CHUNK):
            batch = ids[i : i + UPDATE_CHUNK]
            try:
                db.table("leads").update({"timezone": iana_tz}).in_("id", batch).execute()
                assigned += len(batch)
            except Exception as exc:
                update_errors += len(batch)
                logger.error(f"[tz_backfill] UPDATE tz={iana_tz} failed: {exc}")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    remaining_after = max(0, remaining_before - assigned) if remaining_before >= 0 else -1
    logger.info(
        f"[tz_backfill] chunk done — assigned={assigned}, skipped={skipped}, "
        f"errors={update_errors}, elapsed={elapsed:.1f}s, remaining~={remaining_after}"
    )

    return {
        "assigned":        assigned,
        "skipped":         skipped,
        "update_errors":   update_errors,
        "remaining":       remaining_after,
        "elapsed_seconds": round(elapsed, 1),
    }
