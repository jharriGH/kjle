"""CSV header → DB column mapping and value coercion for the 1.4M-lead source CSVs.

Exposes:
  CSV_TO_DB               — normalized CSV header → DB column (53 mapped fields).
  coerce(col, raw)        — typed coercion by DB column; returns None for empty/invalid.
  derive_domain_flags     — compute domain_expired / domain_expiring_soon / domain_age_days.
  derive_g_maps_bool      — classify g_maps raw value as claimed (True) or unclaimed (False).
  normalize_header        — canonical header normalization shared with csv_import._suggest_mappings.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Header normalization
# ---------------------------------------------------------------------------

def normalize_header(raw: str) -> str:
    """Lowercase + strip + collapse spaces and hyphens to underscores."""
    return raw.strip().lower().replace(" ", "_").replace("-", "_")


# ---------------------------------------------------------------------------
# CSV header (normalized) → DB column
# ---------------------------------------------------------------------------

CSV_TO_DB: dict[str, str] = {
    # core identity
    "name":                          "business_name",
    "phone":                         "phone",
    "email":                         "email",
    "email_host":                    "email_host",
    "website":                       "website",
    "category":                      "category",
    "address":                       "address",
    "region":                        "state",
    "city":                          "city",
    "zip":                           "zip",
    "country":                       "country",
    # Google
    "google_rank":                   "google_rank",
    "googlestars":                   "google_stars",
    "googlereviewscount":            "google_review_count",
    # social URLs
    "facebook":                      "facebook_url",
    "instagram":                     "instagram_url",
    "twitter":                       "twitter_url",
    "linkedin":                      "linkedin_url",
    # Yelp
    "yelpstars":                     "yelp_stars",
    "yelpreviewscount":              "yelp_review_count",
    # Facebook reviews
    "facebookstars":                 "facebook_stars",
    "facebookreviewscount":          "facebook_review_count",
    # pixels / analytics
    "facebookpixel":                 "facebook_pixel",
    "googlepixel":                   "google_pixel",
    "criteopixel":                   "criteo_pixel",
    "seo_schema":                    "seo_schema_present",
    "googleanalytics":               "google_analytics",
    "linkedinanalytics":             "linkedin_analytics",
    # tech stack
    "uses_wordpress":                "uses_wordpress",
    "mobilefriendly":                "mobile_friendly",
    "uses_shopify":                  "uses_shopify",
    # domain
    "domain_registration":           "domain_registered",
    "domain_expiration":             "domain_expires",
    "domain_registrar":              "domain_registrar",
    "domain_nameserver":             "domain_nameserver",
    # Instagram profile
    "instagram_name":                "instagram_name",
    "instagram_is_verified":         "instagram_verified",
    "instagram_is_business_account": "instagram_is_business",
    "instagram_media_count":         "instagram_media_count",
    "instagram_followers":           "instagram_followers",
    "instagram_following":           "instagram_following",
    "instagram_average_likes":       "instagram_avg_likes",
    "instagram_average_comments":    "instagram_avg_comments",
    # ads
    "ads_yelp":                      "ads_yelp",
    "ads_facebook":                  "ads_facebook",
    "ads_instagram":                 "ads_instagram",
    "ads_messenger":                 "ads_messenger",
    "ads_adwords":                   "ads_adwords",
    # maps / search
    "g_maps":                        "g_maps_url",
    "search_keyword":                "search_keyword",
    "search_city":                   "search_city",
    # email deliverability
    "email_state":                   "email_state",       # "Email State" header
    "email_sub_state":               "email_sub_state",   # "Email Sub-State" header
}


# ---------------------------------------------------------------------------
# Type classification sets (DB column name → coercion type)
# ---------------------------------------------------------------------------

_BOOL_COLS: frozenset[str] = frozenset({
    "facebook_pixel", "google_pixel", "criteo_pixel", "seo_schema_present",
    "google_analytics", "linkedin_analytics", "uses_wordpress", "mobile_friendly",
    "uses_shopify", "instagram_verified", "instagram_is_business",
    "ads_yelp", "ads_facebook", "ads_instagram", "ads_messenger", "ads_adwords",
})

_INT_COLS: frozenset[str] = frozenset({
    "google_rank", "google_review_count", "yelp_review_count", "facebook_review_count",
    "instagram_media_count", "instagram_followers", "instagram_following",
    "domain_age_days",
})

_NUM_COLS: frozenset[str] = frozenset({
    "google_stars", "yelp_stars", "facebook_stars",
    "instagram_avg_likes", "instagram_avg_comments",
})

_DATE_COLS: frozenset[str] = frozenset({
    "domain_registered", "domain_expires",
})


# ---------------------------------------------------------------------------
# Primitive coercers (internal)
# ---------------------------------------------------------------------------

def _coerce_bool(s: str) -> Optional[bool]:
    lower = s.lower()
    if lower in ("y", "yes", "true", "1"):
        return True
    if lower in ("n", "no", "false", "0"):
        return False
    return None


def _coerce_int(s: str) -> Optional[int]:
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _coerce_num(s: str) -> Optional[float]:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _coerce_date(s: str) -> Optional[date]:
    """Parse M/D/YYYY, MM/DD/YYYY, or ISO date strings."""
    try:
        parts = s.split("/")
        if len(parts) == 3:
            return date(int(parts[2]), int(parts[0]), int(parts[1]))
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def coerce(db_column: str, raw: Any) -> Any:
    """Return a typed Python value for the given DB column and raw CSV string.

    Returns None for empty, missing, or unparseable values so callers can
    drop the key rather than writing a blank string or 0 to the DB.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if db_column in _BOOL_COLS:
        return _coerce_bool(s)
    if db_column in _INT_COLS:
        return _coerce_int(s)
    if db_column in _NUM_COLS:
        return _coerce_num(s)
    if db_column in _DATE_COLS:
        return _coerce_date(s)
    return s  # string field — already stripped


def derive_domain_flags(
    registered: Optional[date],
    expires: Optional[date],
) -> dict[str, Any]:
    """Compute domain_expired, domain_expiring_soon, and domain_age_days.

    All inputs must be already-coerced date objects (or None). Returns a
    dict with None values for any flag that cannot be computed (e.g. missing
    dates), so callers can filter with `if v is not None`.
    """
    today = date.today()
    flags: dict[str, Any] = {
        "domain_expired": None,
        "domain_expiring_soon": None,
        "domain_age_days": None,
    }
    if expires is not None:
        expired = expires < today
        flags["domain_expired"] = expired
        flags["domain_expiring_soon"] = (not expired) and ((expires - today).days <= 90)
    if registered is not None:
        flags["domain_age_days"] = (today - registered).days
    return flags


def derive_g_maps_bool(raw: Any) -> bool:
    """Return True if the g_maps raw value indicates a claimed / linked listing.

    "claimed" (case-insensitive) or any HTTP/HTTPS URL → True.
    "unclaimed", "false", empty, or None → False.
    This is a dry-run diagnostic value only — there is no g_maps_claimed_bool
    DB column; callers must not write this field to the database.
    """
    if not raw:
        return False
    s = str(raw).strip()
    if s.lower() == "claimed" or s.startswith("http"):
        return True
    return False
