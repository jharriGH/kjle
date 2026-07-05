#!/usr/bin/env python3
"""Dry-run validator for the 1.4M-lead source CSVs.

Usage:
    python scripts/test_csv_dryrun.py <path-to-one-csv>

Reads the first 200 data rows, coerces every mapped column through
csv_field_map, prints sample records and a fill-rate summary, then does a
read-only Supabase match-key check (fingerprint + phone). Writes nothing to
the database.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
from datetime import date
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


def normalize_phone(raw):
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(str(raw), "US")
        if phonenumbers.is_valid_number(parsed):
            return str(parsed.national_number)  # 10 digits
    except Exception:
        pass
    # Fallback: strip non-digits
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


# ── CSV helpers ──────────────────────────────────────────────────────────────

def _parse_csv(path: str) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return headers, rows


def _build_col_map(headers: list[str]) -> dict[str, str]:
    """Return {csv_header: db_column} for every header present in CSV_TO_DB."""
    result: dict[str, str] = {}
    for h in headers:
        norm = csv_field_map.normalize_header(h)
        if norm in csv_field_map.CSV_TO_DB:
            result[h] = csv_field_map.CSV_TO_DB[norm]
    return result


def _coerce_row(row: dict, col_map: dict[str, str]) -> dict[str, Any]:
    """Build a fully coerced record dict for one CSV row."""
    record: dict[str, Any] = {}
    for csv_col, db_col in col_map.items():
        val = csv_field_map.coerce(db_col, row.get(csv_col))
        if val is not None:
            record[db_col] = val

    # derived domain flags
    flags = csv_field_map.derive_domain_flags(
        record.get("domain_registered"),
        record.get("domain_expires"),
    )
    for k, v in flags.items():
        if v is not None:
            record[k] = v

    # g_maps_claimed_bool — print-only; no DB column, never written
    record["g_maps_claimed_bool"] = csv_field_map.derive_g_maps_bool(
        record.get("g_maps_url")
    )

    return record


def _json_default(obj: Any) -> Any:
    if isinstance(obj, date):
        return obj.isoformat()
    return str(obj)


# ── Supabase client (read-only) ──────────────────────────────────────────────

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


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_csv_dryrun.py <path-to-csv>", file=sys.stderr)
        sys.exit(1)

    csv_path = os.path.abspath(sys.argv[1])

    print("=" * 60)
    print("CSV DRY-RUN")
    print("=" * 60)
    print(f"Path       : {csv_path}")

    # ── a) read + count ──────────────────────────────────────────────────────
    headers, all_rows = _parse_csv(csv_path)
    total = len(all_rows)
    print(f"Total rows : {total:,}")
    print(f"CSV cols   : {len(headers)}")

    col_map = _build_col_map(headers)
    print(f"Mapped     : {len(col_map)} of {len(headers)} CSV columns")
    unmapped_cols = [h for h in headers if csv_field_map.normalize_header(h) not in csv_field_map.CSV_TO_DB]
    if unmapped_cols:
        print(f"Unmapped   : {unmapped_cols}")

    # ── b) coerce first 200 rows ─────────────────────────────────────────────
    sample_rows = all_rows[:200]
    coerced: list[dict] = [_coerce_row(r, col_map) for r in sample_rows]
    n = len(coerced)
    print(f"Coerced    : {n} rows")

    # ── c) 3 sample records — prefer rows with yelp/pixel/ads/domain populated
    SHOWCASE = {
        "business_name", "phone", "email",
        "yelp_stars", "yelp_review_count",
        "facebook_pixel", "google_pixel", "criteo_pixel",
        "ads_yelp", "ads_facebook", "ads_instagram", "ads_adwords",
        "domain_registered", "domain_expires",
        "domain_expired", "domain_expiring_soon", "domain_age_days",
        "instagram_followers", "instagram_avg_likes",
        "facebook_url", "linkedin_url", "twitter_url",
        "g_maps_url", "g_maps_claimed_bool",
        "email_state", "email_sub_state",
    }
    print("\n--- Sample records (3) ---")
    shown = 0
    # first pass: rows that have several interesting fields populated
    for rec in coerced:
        if shown >= 3:
            break
        rich = sum(1 for k in SHOWCASE if rec.get(k) not in (None, False, ""))
        if rich >= 4:
            print(json.dumps(
                {k: v for k, v in rec.items() if k in SHOWCASE},
                indent=2, default=_json_default,
            ))
            print()
            shown += 1
    # fallback: just take the first rows
    for rec in coerced:
        if shown >= 3:
            break
        print(json.dumps(
            {k: v for k, v in rec.items() if k in SHOWCASE},
            indent=2, default=_json_default,
        ))
        print()
        shown += 1

    # ── d) fill summary ──────────────────────────────────────────────────────
    all_db_cols = sorted(set(
        list(csv_field_map.CSV_TO_DB.values())
        + ["domain_expired", "domain_expiring_soon", "domain_age_days", "g_maps_claimed_bool"]
    ))
    fill: dict[str, int] = {col: 0 for col in all_db_cols}
    for rec in coerced:
        for col in all_db_cols:
            v = rec.get(col)
            if v is not None and v != "":
                fill[col] += 1

    print("--- Fill summary (non-None across 200 rows) ---")
    for col in all_db_cols:
        count = fill[col]
        bar = "#" * (count // 10)
        pct = f"{count / n * 100:.0f}%"
        print(f"  {col:<38} {count:>3}/{n}  {pct:>4}  {bar}")

    # ── e) match-key test (read-only) ────────────────────────────────────────
    print("\n--- Match-key test ---")
    sb = _make_supabase()

    phones: list[str] = []
    fingerprints: list[str] = []
    for rec in coerced:
        raw_phone = rec.get("phone")
        name = rec.get("business_name") or ""
        norm_phone = normalize_phone(raw_phone)
        if norm_phone:
            phones.append(norm_phone)
            fingerprints.append(make_fingerprint(norm_phone, name))
            continue
        fingerprints.append(make_fingerprint(None, name))

    # fingerprint match
    fp_hits = 0
    try:
        for i in range(0, len(fingerprints), 500):
            batch = fingerprints[i:i + 500]
            res = (
                sb.table("leads")
                .select("fingerprint")
                .in_("fingerprint", batch)
                .execute()
            )
            fp_hits += len(res.data or [])
    except Exception as exc:
        print(f"  [fingerprint query error] {exc}")
        fp_hits = -1

    # phone match
    phone_hits = 0
    try:
        for i in range(0, len(phones), 500):
            batch = phones[i:i + 500]
            res = (
                sb.table("leads")
                .select("phone")
                .in_("phone", batch)
                .execute()
            )
            phone_hits += len(res.data or [])
    except Exception as exc:
        print(f"  [phone query error] {exc}")
        phone_hits = -1

    fp_label = f"{fp_hits}/{n}" if fp_hits >= 0 else "query failed"
    ph_label = f"{phone_hits}/{len(phones)}" if phone_hits >= 0 else "query failed"
    print(f"  Fingerprint matches : {fp_label} rows already in leads")
    print(f"  Phone matches       : {ph_label} (of {len(phones)} rows that have a phone)")
    print("\nDone. No writes performed.")


if __name__ == "__main__":
    main()
