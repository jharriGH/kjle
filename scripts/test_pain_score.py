#!/usr/bin/env python3
"""
KJLE — Pain score formula test fixtures (v2)
File: scripts/test_pain_score.py

Standalone fixture suite that exercises compute_pain_score_v1 across:
  - 5 known-bad fixtures      (expect score >= 70)
  - 5 known-good fixtures     (expect score <= 20)
  - 5 mid-quality fixtures    (expect score 30-60)
  - sparse-data fixtures      (NULL-only inputs MUST NOT inflate; expect <= 20)
  - rich-confirmed-absent     (Change 1 verification — explicit-False inputs
                               DO produce penalties; sub-score values verified)

Run:
    python scripts/test_pain_score.py

Exit code 0 if all pass, 1 if any fail.

This suite locks the v2 formula's distribution shape against regression. If a
future tweak breaks edge behaviour (e.g., known-bad leads dropping below 70),
this script catches it before deploy.
"""
from __future__ import annotations

import os
import sys
import types

# Make scripts/ingest.py importable when running this file directly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ingest.py has a module-level env check that sys.exit(1)s if these aren't set.
# Provide dummy values so import succeeds — compute_pain_score_v1 itself never
# uses these.
os.environ.setdefault("SUPABASE_URL", "https://test.invalid")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test_key")

# ── Lightweight stubs ────────────────────────────────────────────────────────
# scripts/ingest.py imports pandas, supabase, and phonenumbers at module load
# for its full ingest pipeline. compute_pain_score_v1 doesn't use any of them
# — but Python imports are evaluated eagerly. Stub anything missing so this
# test runs in a minimal environment (CI, fresh checkout, etc.) without
# requiring the full ingest dependency tree.
_STUB_MODULES = ["pandas", "supabase", "phonenumbers", "dotenv", "rapidfuzz", "tqdm"]
for _mod_name in _STUB_MODULES:
    if _mod_name not in sys.modules:
        try:
            __import__(_mod_name)
        except ImportError:
            _mod = types.ModuleType(_mod_name)
            # supabase needs a couple of attrs accessed at import time
            if _mod_name == "supabase":
                class _Client: pass
                _mod.Client = _Client
                _mod.create_client = lambda *a, **kw: _Client()
            # pandas: ingest.py uses pd.isna() check during transform_row
            if _mod_name == "pandas":
                _mod.isna = lambda v: v is None
            if _mod_name == "dotenv":
                _mod.load_dotenv = lambda *a, **kw: None
            if _mod_name == "rapidfuzz":
                _fuzz = types.ModuleType("rapidfuzz.fuzz")
                _mod.fuzz = _fuzz
            if _mod_name == "tqdm":
                _mod.tqdm = lambda x, *a, **kw: x  # passthrough iterator wrapper
            sys.modules[_mod_name] = _mod

from ingest import compute_pain_score_v1, _known_absent  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _assert_in(score: float, lo: float, hi: float, name: str) -> bool:
    """Print PASS/FAIL line. Return True on pass."""
    if lo <= score <= hi:
        print(f"  [OK]    {name:48s}  pain={score:6.2f}  (range {lo}-{hi})")
        return True
    print(f"  [FAIL]  {name:48s}  pain={score:6.2f}  (range {lo}-{hi})")
    return False


def _assert_eq(value, expected, name: str) -> bool:
    if value == expected:
        print(f"  [OK]    {name:48s}  value={value}  (expected {expected})")
        return True
    print(f"  [FAIL]  {name:48s}  value={value}  (expected {expected})")
    return False


def _run(fixture: dict, niche: str = "test") -> dict:
    """Wrapper: compute scores for a fixture and return the dict."""
    return compute_pain_score_v1(fixture, niche)


# ─────────────────────────────────────────────────────────────────────────────
# 5 KNOWN-BAD fixtures — must score >= 70
# ─────────────────────────────────────────────────────────────────────────────

KNOWN_BAD = [
    ("bad_zombie_no_website", {
        # 1*/1, unclaimed, KNOWN no website + all KNOWN absent + expired/expiring
        "google_stars": 1.0, "google_review_count": 1, "g_maps_claimed": "unclaimed",
        "google_rank": None,
        "mobile_friendly": False, "seo_schema_present": False,
        "google_analytics": False, "google_pixel": False,
        "facebook_url": "", "instagram_url": "",
        "ads_facebook": False, "ads_adwords": False,
        "facebook_stars": None,
        "website": "",  # known empty
        "uses_wordpress": False, "uses_shopify": False,
        "domain_expired": True, "domain_expiring_soon": True,
        "email_state": "risky",
    }),
    ("bad_2star_with_website", {
        # 2*/3, unclaimed, has website, all KNOWN bad signals, expired
        "google_stars": 2.0, "google_review_count": 3, "g_maps_claimed": "unclaimed",
        "google_rank": None,
        "mobile_friendly": False, "seo_schema_present": False,
        "google_analytics": False, "google_pixel": False,
        "facebook_url": "", "instagram_url": "",
        "ads_facebook": False, "ads_adwords": False,
        "facebook_stars": None,
        "website": "https://biz.com",
        "uses_wordpress": False, "uses_shopify": False,
        "domain_expired": True, "domain_expiring_soon": True,
        "email_state": "ok",
    }),
    ("bad_3star_unclaimed_full_negatives", {
        # 3*/4, unclaimed, all KNOWN bad
        "google_stars": 3.0, "google_review_count": 4, "g_maps_claimed": "unclaimed",
        "google_rank": 50,
        "mobile_friendly": False, "seo_schema_present": False,
        "google_analytics": False, "google_pixel": False,
        "facebook_url": "", "instagram_url": "",
        "ads_facebook": False, "ads_adwords": False,
        "facebook_stars": None,
        "website": "https://biz.com",
        "uses_wordpress": False, "uses_shopify": False,
        "domain_expired": True, "domain_expiring_soon": True,
        "email_state": "ok",
    }),
    ("bad_low_rep_low_fb_stars_risky", {
        # 2*/3, unclaimed, has FB but low FB stars, all SEO known bad, expired, risky email
        "google_stars": 2.0, "google_review_count": 3, "g_maps_claimed": "unclaimed",
        "google_rank": 25,
        "mobile_friendly": False, "seo_schema_present": False,
        "google_analytics": False, "google_pixel": False,
        "facebook_url": "https://fb.com/biz", "instagram_url": "https://ig.com/biz",
        "ads_facebook": True, "ads_adwords": True,
        "facebook_stars": 2.5,  # low — adds +25 to social
        "website": "https://biz.com",
        "uses_wordpress": False, "uses_shopify": False,
        "domain_expired": True, "domain_expiring_soon": True,
        "email_state": "risky",
    }),
    ("bad_extreme_zero_reviews", {
        # 1*/0, unclaimed, has website but neglected
        "google_stars": 1.0, "google_review_count": 0, "g_maps_claimed": "unclaimed",
        "google_rank": None,
        "mobile_friendly": False, "seo_schema_present": False,
        "google_analytics": False, "google_pixel": False,
        "facebook_url": "", "instagram_url": "",
        "ads_facebook": False, "ads_adwords": False,
        "facebook_stars": None,
        "website": "https://biz.com",
        "uses_wordpress": False, "uses_shopify": False,
        "domain_expired": True, "domain_expiring_soon": True,
        "email_state": "ok",
    }),
]


# ─────────────────────────────────────────────────────────────────────────────
# 5 KNOWN-GOOD fixtures — must score <= 20
# ─────────────────────────────────────────────────────────────────────────────

KNOWN_GOOD = [
    ("good_premium_full_signals", {
        # 4.9*/1500, claimed, full positive signals across the board
        "google_stars": 4.9, "google_review_count": 1500, "g_maps_claimed": "claimed",
        "google_rank": 1,
        "mobile_friendly": True, "seo_schema_present": True,
        "google_analytics": True, "google_pixel": True,
        "facebook_url": "https://fb.com/biz", "instagram_url": "https://ig.com/biz",
        "ads_facebook": True, "ads_adwords": True,
        "facebook_stars": 4.9,
        "website": "https://biz.com",
        "uses_wordpress": True, "uses_shopify": False,
        "domain_expired": False, "domain_expiring_soon": False,
        "email_state": "ok",
    }),
    ("good_solid_local", {
        # 4.8*/200, claimed, all positive
        "google_stars": 4.8, "google_review_count": 200, "g_maps_claimed": "claimed",
        "google_rank": 3,
        "mobile_friendly": True, "seo_schema_present": True,
        "google_analytics": True, "google_pixel": True,
        "facebook_url": "https://fb.com/biz", "instagram_url": "https://ig.com/biz",
        "ads_facebook": True, "ads_adwords": True,
        "facebook_stars": 4.8,
        "website": "https://biz.com",
        "uses_wordpress": True, "uses_shopify": False,
        "domain_expired": False, "domain_expiring_soon": False,
        "email_state": "ok",
    }),
    ("good_active_advertiser", {
        # 4.7*/500, all positive, ads everywhere
        "google_stars": 4.7, "google_review_count": 500, "g_maps_claimed": "claimed",
        "google_rank": 2,
        "mobile_friendly": True, "seo_schema_present": True,
        "google_analytics": True, "google_pixel": True,
        "facebook_url": "https://fb.com/biz", "instagram_url": "https://ig.com/biz",
        "ads_facebook": True, "ads_adwords": True,
        "facebook_stars": 4.6,
        "website": "https://biz.com",
        "uses_wordpress": True, "uses_shopify": False,
        "domain_expired": False, "domain_expiring_soon": False,
        "email_state": "ok",
    }),
    ("good_partial_known_seo_gaps", {
        # 4.6*/80, claimed, schema TRUE, KNOWN no analytics/pixel
        # (small SEO penalty 25 from analytics+pixel absent)
        "google_stars": 4.6, "google_review_count": 80, "g_maps_claimed": "claimed",
        "google_rank": 4,
        "mobile_friendly": True, "seo_schema_present": True,
        "google_analytics": False, "google_pixel": False,  # KNOWN absent
        "facebook_url": "https://fb.com/biz", "instagram_url": "https://ig.com/biz",
        "ads_facebook": True, "ads_adwords": True,
        "facebook_stars": 4.5,
        "website": "https://biz.com",
        "uses_wordpress": True, "uses_shopify": False,
        "domain_expired": False, "domain_expiring_soon": False,
        "email_state": "ok",
    }),
    ("good_baseline", {
        # 4.5*/30, claimed, schema TRUE, others null (unknown)
        "google_stars": 4.5, "google_review_count": 30, "g_maps_claimed": "claimed",
        "google_rank": None,
        "mobile_friendly": True, "seo_schema_present": True,
        "google_analytics": None, "google_pixel": None,
        "facebook_url": None, "instagram_url": None,
        "ads_facebook": None, "ads_adwords": None,
        "facebook_stars": None,
        "website": "https://biz.com",
        "uses_wordpress": None, "uses_shopify": None,
        "domain_expired": False, "domain_expiring_soon": False,
        "email_state": "ok",
    }),
]


# ─────────────────────────────────────────────────────────────────────────────
# 5 MID-QUALITY fixtures — must score 30-60
# ─────────────────────────────────────────────────────────────────────────────

MID_QUALITY = [
    ("mid_unclaimed_with_seo_pain", {
        # 3.5*/8, unclaimed, has website, mobile=False known, SEO known false, expired
        "google_stars": 3.5, "google_review_count": 8, "g_maps_claimed": "unclaimed",
        "google_rank": None,
        "mobile_friendly": False, "seo_schema_present": False,
        "google_analytics": False, "google_pixel": False,
        "facebook_url": None, "instagram_url": None,
        "ads_facebook": None, "ads_adwords": None,
        "facebook_stars": None,
        "website": "https://biz.com",
        "uses_wordpress": False, "uses_shopify": False,
        "domain_expired": True, "domain_expiring_soon": False,
        "email_state": "ok",
    }),
    ("mid_bad_rep_otherwise_unknown", {
        # 2.9*/12, unclaimed, has website mobile, SEO/social null
        "google_stars": 2.9, "google_review_count": 12, "g_maps_claimed": "unclaimed",
        "google_rank": None,
        "mobile_friendly": True, "seo_schema_present": None,
        "google_analytics": None, "google_pixel": None,
        "facebook_url": None, "instagram_url": None,
        "ads_facebook": None, "ads_adwords": None,
        "facebook_stars": None,
        "website": "https://biz.com",
        "uses_wordpress": None, "uses_shopify": None,
        "domain_expired": False, "domain_expiring_soon": False,
        "email_state": "ok",
    }),
    ("mid_no_website_decent_rep", {
        # 4.0*/12, claimed, KNOWN no website, mobile=False known, SEO known false
        "google_stars": 4.0, "google_review_count": 12, "g_maps_claimed": "claimed",
        "google_rank": None,
        "mobile_friendly": False, "seo_schema_present": False,
        "google_analytics": False, "google_pixel": False,
        "facebook_url": None, "instagram_url": None,
        "ads_facebook": None, "ads_adwords": None,
        "facebook_stars": None,
        "website": "",  # KNOWN empty
        "uses_wordpress": None, "uses_shopify": None,
        "domain_expired": False, "domain_expiring_soon": False,
        "email_state": "ok",
    }),
    ("mid_decent_old_brand_known_no_ads", {
        # 4.5*/80, claimed, mobile=False known, SEO mostly known bad, KNOWN no ads, expired+expiring
        "google_stars": 4.5, "google_review_count": 80, "g_maps_claimed": "claimed",
        "google_rank": None,
        "mobile_friendly": False, "seo_schema_present": False,
        "google_analytics": False, "google_pixel": False,
        "facebook_url": None, "instagram_url": None,
        "ads_facebook": False, "ads_adwords": False,  # KNOWN false
        "facebook_stars": None,
        "website": "https://biz.com",
        "uses_wordpress": None, "uses_shopify": None,
        "domain_expired": True, "domain_expiring_soon": True,
        "email_state": "ok",
    }),
    ("mid_unclaimed_known_seo_bad_partial_social", {
        # 3.7*/15, unclaimed, has website mobile=False known, SEO/ads known bad
        "google_stars": 3.7, "google_review_count": 15, "g_maps_claimed": "unclaimed",
        "google_rank": None,
        "mobile_friendly": False, "seo_schema_present": False,
        "google_analytics": False, "google_pixel": False,
        "facebook_url": None, "instagram_url": None,  # unknown — no penalty
        "ads_facebook": None, "ads_adwords": None,
        "facebook_stars": None,
        "website": "https://biz.com",
        "uses_wordpress": False, "uses_shopify": False,
        "domain_expired": True, "domain_expiring_soon": False,
        "email_state": "ok",
    }),
]


# ─────────────────────────────────────────────────────────────────────────────
# SPARSE-DATA fixture — Change 1 verification (NULLs MUST NOT inflate)
# ─────────────────────────────────────────────────────────────────────────────

SPARSE_DATA = [
    ("sparse_almost_all_null", {
        # Only stars + reviews populated. EVERYTHING else null. Under v1 this
        # would have inflated to ~50+ via NULL-treated-as-negative penalties.
        # Under v2 it must score low because we don't know enough to penalize.
        "google_stars": 4.0, "google_review_count": 25, "g_maps_claimed": None,
        "google_rank": None,
        "mobile_friendly": None, "seo_schema_present": None,
        "google_analytics": None, "google_pixel": None,
        "facebook_url": None, "instagram_url": None,
        "ads_facebook": None, "ads_adwords": None,
        "facebook_stars": None,
        "website": None,
        "uses_wordpress": None, "uses_shopify": None,
        "domain_expired": None, "domain_expiring_soon": None,
        "email_state": None,
    }),
    ("sparse_only_reputation_signals", {
        # Same as above but mid-low reputation. Should still score low,
        # not inflated by null sub-scores.
        "google_stars": 3.5, "google_review_count": 12, "g_maps_claimed": None,
        "google_rank": None,
        "mobile_friendly": None, "seo_schema_present": None,
        "google_analytics": None, "google_pixel": None,
        "facebook_url": None, "instagram_url": None,
        "ads_facebook": None, "ads_adwords": None,
        "facebook_stars": None,
        "website": None,
        "uses_wordpress": None, "uses_shopify": None,
        "domain_expired": None, "domain_expiring_soon": None,
        "email_state": None,
    }),
]


# ─────────────────────────────────────────────────────────────────────────────
# RICH-CONFIRMED-ABSENT — Change 1 verification (KNOWN absences DO penalize)
# ─────────────────────────────────────────────────────────────────────────────

# Hand-computed expected sub-scores for the rich-confirmed-absent fixture:
#   reputation: 0  (4.5★, 50 reviews → falls between 25 and 50, no review-count branch fires)
#   seo:        0  (all SEO True/positive)
#   social:     75 (FB, IG empty → +25+20; ads False → +15+15; fb_stars null → 0)
#   website:    0  (has website, mobile, WP true)
#   bizintel:   20 (no domain issues; both ads KNOWN false → +20)
RICH_CONFIRMED_ABSENT = {
    "google_stars": 4.5, "google_review_count": 50, "g_maps_claimed": "claimed",
    "google_rank": 5,
    "mobile_friendly": True, "seo_schema_present": True,
    "google_analytics": True, "google_pixel": True,
    "facebook_url": "", "instagram_url": "",        # KNOWN empty (not None)
    "ads_facebook": False, "ads_adwords": False,    # KNOWN false (not None)
    "facebook_stars": None,
    "website": "https://biz.com",
    "uses_wordpress": True, "uses_shopify": False,
    "domain_expired": False, "domain_expiring_soon": False,
    "email_state": "ok",
}


# ─────────────────────────────────────────────────────────────────────────────
# _known_absent helper unit tests
# ─────────────────────────────────────────────────────────────────────────────

def test_known_absent_helper() -> int:
    print("=== _known_absent helper unit tests ===")
    fails = 0
    cases = [
        (None,  False, "None is unknown"),
        (False, True,  "explicit False is known absent"),
        (True,  False, "True is present"),
        ("",    True,  "empty string is known absent"),
        ("  ",  True,  "whitespace string is known absent"),
        ("x",   False, "non-empty string is present"),
        (0,     True,  "0 is known absent"),
        (1,     False, "non-zero int is present"),
        (0.0,   True,  "0.0 is known absent"),
        (1.5,   False, "non-zero float is present"),
    ]
    for val, expected, desc in cases:
        got = _known_absent(val)
        status = "OK" if got == expected else "FAIL"
        if got != expected:
            fails += 1
        print(f"  [{status:4s}]  _known_absent({val!r:8s})  -> {got}  ({desc})")
    return fails


# ─────────────────────────────────────────────────────────────────────────────
# Run the suite
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    fails = 0
    fails += test_known_absent_helper()

    print()
    print("=== KNOWN-BAD fixtures (must score >= 70) ===")
    for name, fix in KNOWN_BAD:
        scores = _run(fix)
        if not _assert_in(scores["pain_score"], 70, 100, name):
            fails += 1

    print()
    print("=== KNOWN-GOOD fixtures (must score <= 20) ===")
    for name, fix in KNOWN_GOOD:
        scores = _run(fix)
        if not _assert_in(scores["pain_score"], 0, 20, name):
            fails += 1

    print()
    print("=== MID-QUALITY fixtures (must score 30-60) ===")
    for name, fix in MID_QUALITY:
        scores = _run(fix)
        if not _assert_in(scores["pain_score"], 30, 60, name):
            fails += 1

    print()
    print("=== SPARSE-DATA fixtures (NULLs MUST NOT inflate; expect <= 20) ===")
    for name, fix in SPARSE_DATA:
        scores = _run(fix)
        if not _assert_in(scores["pain_score"], 0, 20, name):
            fails += 1

    print()
    print("=== RICH-CONFIRMED-ABSENT (Change 1 verification) ===")
    print("Verifies that KNOWN-False / KNOWN-empty inputs DO produce penalties,")
    print("even though NULL inputs do not. Critical regression guard for v2.")
    scores = _run(RICH_CONFIRMED_ABSENT)
    print(f"  composite pain_score = {scores['pain_score']}")
    if not _assert_eq(scores["pain_score_social"], 75,
                      "social subscore (FB+IG known empty + ads known false)"):
        fails += 1
    if not _assert_eq(scores["pain_score_bizintel"], 20,
                      "bizintel subscore (both ads known false; +20)"):
        fails += 1
    # Composite for this fixture:
    #   web 0*0.20 + rep 0*0.40 + seo 0*0.10 + social 75*0.10 + biz 20*0.20
    #   = 0 + 0 + 0 + 7.5 + 4.0 = 11.5
    if not _assert_in(scores["pain_score"], 11.0, 12.0,
                      "composite (rich-confirmed-absent)"):
        fails += 1

    print()
    print(f"=== {'ALL TESTS PASS' if fails == 0 else f'{fails} FAILURE(S)'} ===")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
