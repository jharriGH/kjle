#!/usr/bin/env python3
"""READ-ONLY dedup preview for the 1.5M-lead leads table.

Shows how many rows collapse under the keep-rule, which survives, and which
fields merge up into the survivor — without writing anything.

Usage:
    python3 scripts/dedup_preview.py [--sample N]

Run on VPS where supabase-py + phonenumbers are installed.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from typing import Any, Optional

# ── repo root on sys.path ────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── load .env exactly like test_csv_dryrun.py ───────────────────────────────
def _load_dotenv() -> None:
    for candidate in (
        os.path.join(_REPO_ROOT, ".env"),
        os.path.join(_REPO_ROOT, "api", ".env"),
    ):
        if not os.path.isfile(candidate):
            continue
        with open(candidate, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)
        break


_load_dotenv()

import phonenumbers  # noqa: E402


# ── normalize_phone verbatim from scripts/ingest.py ─────────────────────────
def normalize_phone(raw: any) -> Optional[str]:
    """Return 10-digit US phone string or None."""
    if raw is None or str(raw).strip() == '':
        return None
    try:
        raw_str = str(int(float(str(raw)))) if str(raw).replace('.', '').isdigit() else str(raw)
        parsed = phonenumbers.parse(raw_str, "US")
        if phonenumbers.is_valid_number(parsed):
            return str(parsed.national_number)  # 10 digits
    except Exception:
        digits = re.sub(r'\D', '', str(raw))
        if len(digits) == 11 and digits[0] == '1':
            digits = digits[1:]
        if len(digits) == 10:
            return digits
    return None


# ── Supabase client ──────────────────────────────────────────────────────────
def _make_supabase():
    try:
        from supabase import create_client
    except ImportError:
        print("ERROR: supabase-py not installed — pip install supabase", file=sys.stderr)
        sys.exit(1)
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set", file=sys.stderr)
        sys.exit(1)
    return create_client(url, key)


# ── dedup key ────────────────────────────────────────────────────────────────
def _dedup_key(phone: Optional[str], name: Optional[str]) -> Optional[tuple]:
    norm_phone = normalize_phone(phone)
    norm_name = re.sub(r'[^a-z0-9]', '', (name or '').lower())
    if norm_phone is None and norm_name == '':
        return None  # no identifying info — skip
    return (norm_phone, norm_name)


# ── value score (all components ascending — higher = better survivor) ────────
def _value_score(row: dict, has_email_cleaned: bool) -> tuple:
    stage = row.get("enrichment_stage")
    stage_score = stage if stage is not None else -1
    completeness = sum(1 for v in row.values() if v is not None and v != "")
    email_bonus = 1 if (has_email_cleaned and row.get("email_cleaned_at")) else 0
    pain_bonus = 1 if row.get("pain_score") is not None else 0
    created = row.get("created_at") or ""  # ISO string — lexicographic tiebreak, newer wins
    return (stage_score, completeness, email_bonus, pain_bonus, created)


# ── fields that would merge up from losers into survivor ────────────────────
def _merge_preview(survivor: dict, losers: list[dict]) -> dict[str, Any]:
    """Return {col: first_non_null_value} for cols null in survivor but present in a loser."""
    merged: dict[str, Any] = {}
    for loser in losers:
        for col, val in loser.items():
            if val is None or val == "":
                continue
            if col in ("id", "fingerprint"):
                continue
            current = survivor.get(col)
            if (current is None or current == "") and col not in merged:
                merged[col] = val
    return merged


# ── step 1: probe email_cleaned_at column existence ─────────────────────────
def _probe_email_cleaned(sb) -> bool:
    try:
        res = (
            sb.table("leads")
            .select("id,email_cleaned_at")
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if rows and "email_cleaned_at" in rows[0]:
            return True
        return False
    except Exception as exc:
        print(f"  NOTE: email_cleaned_at probe failed ({exc}) — omitting from score.")
        return False


# ── step 1: paginated bulk fetch of scoring columns ─────────────────────────
def _fetch_all_leads(sb, has_email_cleaned: bool) -> list[dict]:
    cols = "id,phone,business_name,enrichment_stage,created_at,pain_score"
    if has_email_cleaned:
        cols += ",email_cleaned_at"

    PAGE = 1000
    all_rows: list[dict] = []
    offset = 0
    print(f"Fetching all active leads (page size {PAGE:,})...")

    while True:
        try:
            res = (
                sb.table("leads")
                .select(cols)
                .eq("is_active", True)
                .range(offset, offset + PAGE - 1)
                .execute()
            )
        except Exception as exc:
            print(f"  [fetch error at offset {offset:,}] {exc}", file=sys.stderr)
            break

        batch = res.data or []
        if not batch:
            break

        all_rows.extend(batch)
        offset += PAGE

        if offset % 100_000 == 0:
            print(f"  ... {offset:,} fetched")

        if len(batch) < PAGE:
            break

    print(f"Total fetched: {len(all_rows):,}")
    return all_rows


# ── step 2: group by dedup key ───────────────────────────────────────────────
def _group_leads(rows: list[dict]) -> tuple[dict, int]:
    """Return (groups, null_key_count).

    groups = {dedup_key: [row, ...]}  — only keys with >0 rows.
    null_key_count = rows with no identifying info (skipped from dedup).
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    null_count = 0
    for row in rows:
        key = _dedup_key(row.get("phone"), row.get("business_name"))
        if key is None:
            null_count += 1
        else:
            groups[key].append(row)
    return dict(groups), null_count


# ── step 3: fetch full rows by id list ───────────────────────────────────────
def _fetch_full_rows(sb, ids: list[str]) -> dict[str, dict]:
    """Fetch full rows for a list of lead IDs; return {id: row}."""
    result: dict[str, dict] = {}
    for i in range(0, len(ids), 100):
        batch = ids[i : i + 100]
        try:
            res = (
                sb.table("leads")
                .select("*")
                .in_("id", batch)
                .execute()
            )
            for row in res.data or []:
                result[str(row["id"])] = row
        except Exception as exc:
            print(f"  [full fetch error] {exc}", file=sys.stderr)
    return result


# ── select 20 sample dup groups (mixed sizes) ────────────────────────────────
def _pick_samples(dup_groups: dict[tuple, list[dict]], n: int = 20) -> list[list[dict]]:
    by_size: dict[int, list[list[dict]]] = defaultdict(list)
    for members in dup_groups.values():
        by_size[len(members)].append(members)

    sizes = sorted(by_size.keys())
    buckets = [2, 3, 4, 5]  # 5 = "5+"
    per_bucket = max(1, n // len(buckets))
    samples: list[list[dict]] = []

    for target in buckets:
        candidates = []
        if target == 5:
            for s in sizes:
                if s >= 5:
                    candidates.extend(by_size[s])
        else:
            candidates = by_size.get(target, [])
        # sort within bucket: prefer higher enrichment_stage for richer samples
        candidates.sort(
            key=lambda g: max(
                (r.get("enrichment_stage") or -1) for r in g
            ),
            reverse=True,
        )
        for grp in candidates[:per_bucket]:
            if len(samples) < n:
                samples.append(grp)

    # top up from any remaining dup groups if under n
    if len(samples) < n:
        for members in dup_groups.values():
            if members not in samples:
                samples.append(members)
            if len(samples) >= n:
                break

    return samples[:n]


# ── print one sample group ───────────────────────────────────────────────────
SHOW_COLS = [
    "id", "business_name", "phone", "email", "enrichment_stage", "pain_score",
    "email_cleaned_at", "created_at", "yelp_stars", "facebook_pixel",
    "google_pixel", "instagram_followers", "domain_age_days",
    "ads_facebook", "website", "niche_slug",
]

def _print_group(group_num: int, slim_members: list[dict], full_rows: dict[str, dict],
                 has_email_cleaned: bool) -> tuple[int, int]:
    """Print one sample group. Returns (survivors_count, archived_count)."""
    ids = [str(r["id"]) for r in slim_members]
    rows = [full_rows.get(rid) for rid in ids if full_rows.get(rid)]
    if not rows:
        return 0, 0

    scored = sorted(rows, key=lambda r: _value_score(r, has_email_cleaned), reverse=True)
    survivor = scored[0]
    losers = scored[1:]
    merge = _merge_preview(survivor, losers)

    print(f"\n  Group #{group_num}  ({len(rows)} rows)")
    print(f"  {'─' * 55}")

    for i, row in enumerate(scored):
        label = "SURVIVOR" if i == 0 else f"ARCHIVE  "
        score = _value_score(row, has_email_cleaned)
        stage = row.get("enrichment_stage")
        stage_str = str(stage) if stage is not None else "NULL"
        print(f"  [{label}] id={row.get('id')}  stage={stage_str}  "
              f"score={score[:4]}  created={str(row.get('created_at',''))[:10]}")
        for col in SHOW_COLS:
            val = row.get(col)
            if val is not None and val != "" and col not in ("id", "created_at"):
                print(f"            {col}: {str(val)[:80]}")

    if merge:
        print(f"  MERGE UP into survivor ({len(merge)} fields from losers):")
        for col, val in list(merge.items())[:15]:
            print(f"    + {col}: {str(val)[:80]}")
        if len(merge) > 15:
            print(f"    ... and {len(merge) - 15} more fields")
    else:
        print("  MERGE UP: (no additional fields from losers)")

    return 1, len(losers)


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only dedup preview for the leads table."
    )
    parser.add_argument(
        "--sample", type=int, default=20,
        help="Number of sample dup groups to show (default: 20)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("DEDUP PREVIEW  —  READ ONLY, NO WRITES")
    print("=" * 60)

    sb = _make_supabase()

    # ── probe email_cleaned_at ───────────────────────────────────────────────
    print("\nProbing email_cleaned_at column...")
    has_email_cleaned = _probe_email_cleaned(sb)
    print(f"  email_cleaned_at present: {has_email_cleaned}")
    if not has_email_cleaned:
        print("  -> Omitting email_cleaned_at from value score.")

    # ── step 1: fetch all active leads ───────────────────────────────────────
    print()
    all_rows = _fetch_all_leads(sb, has_email_cleaned)
    total_leads = len(all_rows)

    # ── step 2: group + stats ────────────────────────────────────────────────
    print("\nGrouping by dedup key...")
    groups, null_key_count = _group_leads(all_rows)

    singleton_groups = {k: v for k, v in groups.items() if len(v) == 1}
    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}

    total_rows_removed = sum(len(v) - 1 for v in dup_groups.values())
    total_would_keep = len(singleton_groups) + len(dup_groups) + null_key_count
    total_would_archive = total_rows_removed

    dist: dict[str, int] = {"2x": 0, "3x": 0, "4x": 0, "5x+": 0}
    for members in dup_groups.values():
        n = len(members)
        if n == 2:
            dist["2x"] += 1
        elif n == 3:
            dist["3x"] += 1
        elif n == 4:
            dist["4x"] += 1
        else:
            dist["5x+"] += 1

    print()
    print("=" * 60)
    print("STEP 2 — DEDUP STATISTICS")
    print("=" * 60)
    print(f"  Total leads scanned       : {total_leads:>12,}")
    print(f"  Null-key leads (skipped)  : {null_key_count:>12,}")
    print(f"  Leads with valid key      : {total_leads - null_key_count:>12,}")
    print(f"  Distinct keys             : {len(groups):>12,}")
    print(f"  Singleton groups          : {len(singleton_groups):>12,}")
    print(f"  Dup groups (>1 row)       : {len(dup_groups):>12,}")
    print(f"  Rows that would be REMOVED: {total_rows_removed:>12,}")
    print()
    print("  Distribution of dup groups:")
    for label, count in dist.items():
        print(f"    {label}  : {count:>8,}")
    print()
    print(f"  Would KEEP    : {total_would_keep:>12,}")
    print(f"  Would ARCHIVE : {total_would_archive:>12,}")

    # ── step 3: sample group detail ──────────────────────────────────────────
    if not dup_groups:
        print("\nNo duplicate groups found.")
    else:
        print()
        print("=" * 60)
        print(f"STEP 3 — {args.sample} SAMPLE DUP GROUPS (FULL ROW DETAIL)")
        print("=" * 60)

        samples = _pick_samples(dup_groups, args.sample)

        # Batch-fetch all full rows for all sample groups
        all_sample_ids: list[str] = []
        for grp in samples:
            all_sample_ids.extend(str(r["id"]) for r in grp)

        print(f"\nFetching full rows for {len(all_sample_ids)} leads across {len(samples)} groups...")
        full_rows = _fetch_full_rows(sb, all_sample_ids)

        total_sample_survivors = 0
        total_sample_archived = 0
        for i, grp in enumerate(samples, start=1):
            s, a = _print_group(i, grp, full_rows, has_email_cleaned)
            total_sample_survivors += s
            total_sample_archived += a

        print(f"\n  Sample summary: {total_sample_survivors} survivors, "
              f"{total_sample_archived} would be archived across {len(samples)} groups.")

    # ── final line ───────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print(
        f"PREVIEW ONLY — no writes. "
        f"Would keep {total_would_keep:,}, archive {total_would_archive:,}."
    )
    print("=" * 60)


if __name__ == "__main__":
    main()
