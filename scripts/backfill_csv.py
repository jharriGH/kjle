#!/usr/bin/env python3
"""Backfill enrichment/signal columns onto existing leads from a source CSV.

Matches by fingerprint. Fill-gaps only — never overwrites non-null DB values.
Never inserts new leads. Default: dry-run; use --live to write.

Usage:
    python3 scripts/backfill_csv.py <csv_path> [--limit N] [--live]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
from typing import Any, Optional

# ── repo root on sys.path so api.lib is importable ──────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── load .env before anything that reads env vars ───────────────────────────
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

from api.lib import csv_field_map  # noqa: E402 — must come after sys.path + dotenv

import phonenumbers  # noqa: E402


# ── verbatim from test_csv_dryrun.py ────────────────────────────────────────

def normalize_phone(raw):
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(str(raw), "US")
        if phonenumbers.is_valid_number(parsed):
            return str(parsed.national_number)  # 10 digits
    except Exception:
        pass
    digits = re.sub(r'\D', '', str(raw))
    if len(digits) == 11 and digits[0] == '1':
        digits = digits[1:]
    if len(digits) == 10:
        return digits
    return None


def make_fingerprint(phone, name):
    norm_name = re.sub(r'[^a-z0-9]', '', name.lower()) if name else ''
    if phone:
        raw = f"{phone}:{norm_name}"
    else:
        raw = f"nophone:{norm_name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── constants ────────────────────────────────────────────────────────────────

TARGET_COLUMNS: list[str] = [
    "google_rank", "email_host", "yelp_stars", "yelp_review_count",
    "facebook_url", "facebook_stars", "facebook_review_count", "facebook_pixel",
    "ads_facebook", "ads_messenger", "ads_instagram", "ads_yelp", "ads_adwords",
    "instagram_url", "instagram_name", "instagram_verified", "instagram_is_business",
    "instagram_followers", "instagram_following", "instagram_media_count",
    "instagram_avg_likes", "instagram_avg_comments",
    "twitter_url", "linkedin_url", "linkedin_analytics",
    "google_pixel", "criteo_pixel", "google_analytics",
    "domain_registered", "domain_expires", "domain_registrar", "domain_nameserver",
    "domain_age_days", "domain_expired", "domain_expiring_soon",
    "uses_wordpress", "uses_shopify", "mobile_friendly", "seo_schema_present",
    "g_maps_url", "g_maps_claimed_bool", "search_keyword", "search_city",
]

_TARGET_SET: frozenset[str] = frozenset(TARGET_COLUMNS)


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
        print(
            "ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in env or .env",
            file=sys.stderr,
        )
        sys.exit(1)
    return create_client(url, key)


# ── CSV helpers ──────────────────────────────────────────────────────────────

def _parse_csv(path: str) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return headers, rows


def _build_col_map(headers: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for h in headers:
        norm = csv_field_map.normalize_header(h)
        if norm in csv_field_map.CSV_TO_DB:
            result[h] = csv_field_map.CSV_TO_DB[norm]
    return result


def _coerce_row(row: dict, col_map: dict[str, str]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for csv_col, db_col in col_map.items():
        val = csv_field_map.coerce(db_col, row.get(csv_col))
        if val is not None:
            record[db_col] = val

    # derived domain flags (may produce False booleans — intentionally written)
    flags = csv_field_map.derive_domain_flags(
        record.get("domain_registered"),
        record.get("domain_expires"),
    )
    for k, v in flags.items():
        if v is not None:
            record[k] = v

    # g_maps_claimed_bool — only derive when g_maps_url is present in this row;
    # derive_g_maps_bool(None) returns False which would wrongly fill nulls for
    # rows that simply have no maps data in the CSV.
    if record.get("g_maps_url") is not None:
        record["g_maps_claimed_bool"] = csv_field_map.derive_g_maps_bool(
            record["g_maps_url"]
        )

    return record


# ── fetch existing leads ─────────────────────────────────────────────────────

def _fetch_existing(sb, fingerprints: list[str]) -> dict[str, dict]:
    """Fetch leads by fingerprint in chunks of 100; return {fingerprint: row}."""
    select_cols = "id,fingerprint," + ",".join(TARGET_COLUMNS)
    result: dict[str, dict] = {}
    for i in range(0, len(fingerprints), 100):
        batch = fingerprints[i : i + 100]
        try:
            res = (
                sb.table("leads")
                .select(select_cols)
                .in_("fingerprint", batch)
                .execute()
            )
            for row in res.data or []:
                result[row["fingerprint"]] = row
        except Exception as exc:
            print(f"  [fetch error chunk {i // 100}] {exc}", file=sys.stderr)
    return result


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill enrichment columns onto existing leads from CSV."
    )
    parser.add_argument("csv_path", help="Path to source CSV file")
    parser.add_argument(
        "--limit", type=int, default=200,
        help="Max CSV rows to process (default: 200)",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Write to DB. Without this flag the script is a DRY RUN.",
    )
    args = parser.parse_args()

    mode = "LIVE WRITE" if args.live else "DRY RUN"
    print("=" * 60)
    print(f"BACKFILL CSV  —  {mode}")
    print("=" * 60)
    print(f"CSV    : {os.path.abspath(args.csv_path)}")
    print(f"Limit  : {args.limit}")
    print(f"Mode   : {mode}")
    print()

    # ── 1. read + coerce ─────────────────────────────────────────────────────
    headers, all_rows = _parse_csv(args.csv_path)
    rows = all_rows[: args.limit]
    col_map = _build_col_map(headers)
    print(f"Total CSV rows : {len(all_rows):,}")
    print(f"Processing     : {len(rows)}")
    print(f"Mapped columns : {len(col_map)} of {len(headers)}")

    coerced: list[dict] = [_coerce_row(r, col_map) for r in rows]

    # ── 2. compute fingerprints ──────────────────────────────────────────────
    fingerprints: list[str] = []
    for rec in coerced:
        norm = normalize_phone(rec.get("phone"))
        name = rec.get("business_name") or ""
        fingerprints.append(make_fingerprint(norm, name))

    # ── 3. fetch existing DB rows ────────────────────────────────────────────
    sb = _make_supabase()
    print(f"\nFetching existing leads ({len(fingerprints)} fingerprints, chunks of 100)...")
    db_rows = _fetch_existing(sb, fingerprints)
    matched_count = len(db_rows)
    unmatched_count = len(fingerprints) - matched_count
    print(f"Fingerprint matches : {matched_count}/{len(fingerprints)}")
    print(f"Unmatched (skipped) : {unmatched_count}")

    # ── 4. build update plans (fill-gaps only) ───────────────────────────────
    plans: list[dict] = []
    col_write_counts: dict[str, int] = {c: 0 for c in TARGET_COLUMNS}

    for fp, rec in zip(fingerprints, coerced):
        db_row = db_rows.get(fp)
        if db_row is None:
            continue
        update: dict[str, Any] = {}
        for col in TARGET_COLUMNS:
            csv_val = rec.get(col)
            db_val = db_row.get(col)
            if csv_val is not None and db_val is None:
                update[col] = csv_val
                col_write_counts[col] += 1
        if update:
            plans.append({"id": db_row["id"], "fp": fp, "update": update})

    rows_with_update = len(plans)
    print(f"Rows with non-empty update : {rows_with_update}/{matched_count}")

    # ── 5. per-column write counts ───────────────────────────────────────────
    print("\n--- Per-column write counts ---")
    any_writes = False
    for col in TARGET_COLUMNS:
        cnt = col_write_counts[col]
        if cnt > 0:
            print(f"  {col:<38} {cnt:>4}")
            any_writes = True
    if not any_writes:
        print("  (no columns have values to write)")
    zero_cols = [c for c in TARGET_COLUMNS if col_write_counts[c] == 0]
    if zero_cols:
        print(f"  (0 fills: {', '.join(zero_cols)})")

    # ── 6. before/after samples (up to 2 matched rows) ──────────────────────
    SAMPLE_COLS = [
        "yelp_stars", "yelp_review_count", "facebook_pixel", "google_pixel",
        "domain_age_days", "domain_expired", "domain_expiring_soon",
        "instagram_followers", "ads_facebook", "g_maps_claimed_bool",
        "uses_wordpress", "mobile_friendly",
    ]
    print("\n--- Before/After samples (up to 2 rows) ---")
    shown = 0
    for plan in plans:
        if shown >= 2:
            break
        db_row = db_rows[plan["fp"]]
        print(f"  lead_id={plan['id']}  fp={plan['fp'][:12]}...")
        for col in SAMPLE_COLS:
            if col in plan["update"]:
                print(f"    {col:<38} null  ->  {plan['update'][col]!r}")
        if not any(c in plan["update"] for c in SAMPLE_COLS):
            # show whatever columns are being updated
            for col, val in list(plan["update"].items())[:5]:
                print(f"    {col:<38} null  ->  {val!r}")
        shown += 1
    if not plans:
        print("  (no matched rows with updates)")

    # ── 7. write (live) or stop (dry run) ────────────────────────────────────
    if not args.live:
        print(f"\nDRY RUN - no writes")
        return

    print(f"\nWriting {rows_with_update} rows...")
    success = 0
    failures = 0
    for plan in plans:
        try:
            sb.table("leads").update(plan["update"]).eq("id", plan["id"]).execute()
            success += 1
        except Exception as exc:
            failures += 1
            print(f"  [UPDATE FAIL] id={plan['id']} — {exc}")

    print(f"\nUpdate successes : {success}")
    if failures:
        print(f"Update failures  : {failures}")
    print(f"\nLIVE - wrote {success} rows")


if __name__ == "__main__":
    main()
