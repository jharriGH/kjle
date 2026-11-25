"""
KJLE — IANA timezone backfill for leads.

Sets leads.timezone (IANA string, e.g. "America/New_York") for every lead
with a US state using a three-tier resolution:
  1. Single-tz states: direct state → IANA lookup.
  2. Split-tz states: longitude boundary if lat/lng present, then
     zip-prefix table for well-known minority zones, then state majority.
  3. Unresolvable: skip (non-US or state not in map).

Idempotent: only processes rows WHERE (timezone IS NULL OR timezone = '').
Keyset-paginated by id in 1000-row pages (the PostgREST response cap).
Grouped UPDATEs by tz (one UPDATE per tz per page) keep statement count low.
"""
from __future__ import annotations

import logging
import time
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
PAGE_SIZE = 1000  # PostgREST hard-caps responses at 1000 rows regardless of .limit()
UPDATE_CHUNK = 500  # ids per UPDATE .in_() call
DEFAULT_BUDGET_SECONDS = 120.0
NIL_UUID = "00000000-0000-0000-0000-000000000000"  # leads.id is uuid; keyset cursor start
_SELECT_COLS = "id, state, zip, lng"


def _count_remaining(db) -> int:
    try:
        res = (
            db.table("leads")
            .select("id", count="exact")
            .eq("is_active", True)
            .not_.is_("state", "null")
            .or_("timezone.is.null,timezone.eq.")
            .limit(1)
            .execute()
        )
        return res.count or 0
    except Exception as exc:
        logger.warning(f"[tz_backfill] count query failed: {exc}")
        return -1


def _process_page(db, last_id: str) -> tuple[Optional[str], int, int, int]:
    """
    Fetch one keyset page (id > last_id) of un-timezoned rows, resolve and
    UPDATE. Returns (new_last_id, fetched, assigned, skipped); new_last_id is
    None when the page was empty. Raises on fetch failure.
    """
    rows = (
        db.table("leads")
        .select(_SELECT_COLS)
        .eq("is_active", True)
        .not_.is_("state", "null")
        .or_("timezone.is.null,timezone.eq.")
        .gt("id", last_id)
        .order("id")
        .limit(PAGE_SIZE)
        .execute()
        .data or []
    )
    if not rows:
        return None, 0, 0, 0

    tz_groups: dict[str, list[str]] = {}
    skipped = 0
    for row in rows:
        iana = _get_iana_tz(row.get("state"), row.get("zip"), row.get("lng"))
        if iana:
            tz_groups.setdefault(iana, []).append(row["id"])
        else:
            skipped += 1

    assigned = 0
    for iana_tz, ids in tz_groups.items():
        for i in range(0, len(ids), UPDATE_CHUNK):
            batch = ids[i : i + UPDATE_CHUNK]
            try:
                db.table("leads").update({"timezone": iana_tz}).in_("id", batch).execute()
                assigned += len(batch)
            except Exception as exc:
                logger.error(f"[tz_backfill] UPDATE tz={iana_tz} failed: {exc}")

    return rows[-1]["id"], len(rows), assigned, skipped


def run_tz_backfill(
    db,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    max_rows: Optional[int] = None,
) -> dict:
    """
    Keyset-paginate (by id) through un-timezoned active rows until the
    candidate set is exhausted, the wall-time budget is hit, or max_rows
    rows have been scanned. The cursor advances by id, not by rows leaving the
    NULL set, so unresolvable (non-US/dirty) rows never block progress.
    """
    t0 = time.monotonic()
    last_id = NIL_UUID
    assigned = skipped = pages = scanned = 0
    exhausted = False
    error: Optional[str] = None

    while True:
        if time.monotonic() - t0 >= budget_seconds:
            break
        if max_rows is not None and scanned >= max_rows:
            break
        try:
            new_last, fetched, a, s = _process_page(db, last_id)
        except Exception as exc:
            error = str(exc)
            logger.error(f"[tz_backfill] fetch failed: {exc}")
            break
        if new_last is None:
            exhausted = True
            break
        last_id = new_last
        pages += 1
        scanned += fetched
        assigned += a
        skipped += s
        if fetched < PAGE_SIZE:
            exhausted = True
            break

    elapsed = time.monotonic() - t0
    remaining = _count_remaining(db)
    logger.info(
        f"[tz_backfill] run done — assigned={assigned}, skipped={skipped}, pages={pages}, "
        f"scanned={scanned}, exhausted={exhausted}, elapsed={elapsed:.1f}s, remaining={remaining}"
    )
    result = {
        "assigned":        assigned,
        "skipped":         skipped,
        "pages":           pages,
        "scanned":         scanned,
        "exhausted":       exhausted,
        "remaining":       remaining,
        "elapsed_seconds": round(elapsed, 1),
    }
    if error:
        result["error"] = error
    return result


def run_tz_backfill_chunk(db, chunk_size: int = CHUNK_SIZE) -> dict:
    """Manual-trigger entrypoint: scan up to chunk_size rows (within the default time budget)."""
    return run_tz_backfill(db, max_rows=chunk_size)
