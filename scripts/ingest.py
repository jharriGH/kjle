"""
KJLE — King James Lead Empire
Ingestion Script v1.0
Prompt 2 of 25

PURPOSE:
  Reads any KJLE-format CSV/XLSX, normalizes all fields,
  deduplicates, computes preliminary Pain Score v1,
  and bulk-inserts to Supabase.

USAGE:
  python ingest.py --file path/to/leads.xlsx --niche hvac
  python ingest.py --folder path/to/csv_folder/ --niche hvac
  python ingest.py --folder ./all_csvs/ --auto-niche   (infer niche from category column)

REQUIREMENTS:
  pip install supabase pandas openpyxl phonenumbers python-dotenv rapidfuzz tqdm
"""

import os
import re
import sys
import uuid
import hashlib
import argparse
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import pandas as pd
import phonenumbers
from dotenv import load_dotenv
from supabase import create_client, Client
from rapidfuzz import fuzz
from tqdm import tqdm

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('kjle.ingest')

# ─── Supabase Connection ──────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in .env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ─── Column Mapping ───────────────────────────────────────────────────────────
# Maps every known CSV column variant → KJLE canonical field name
# Add new variants here as you encounter new CSV formats
COLUMN_MAP = {
    # Identity
    'name':                     'business_name',
    'business_name':            'business_name',
    'company':                  'business_name',
    'company_name':             'business_name',

    'phone':                    'phone_raw',
    'phone_number':             'phone_raw',
    'telephone':                'phone_raw',

    'email':                    'email',
    'email_address':            'email',
    'Email Address':            'email',
    'email_host':               'email_host',

    'website':                  'website',
    'url':                      'website',
    'website_url':              'website',

    'category':                 'category',
    'niche':                    'category',
    'business_type':            'category',
    'type':                     'category',

    # Location
    'address':                  'address',
    'street_address':           'address',
    'city':                     'city',
    'region':                   'state',
    'state':                    'state',
    'zip':                      'zip',
    'zip_code':                 'zip',
    'postal_code':              'zip',
    'country':                  'country',

    # Google / GBP
    'google_rank':              'google_rank',
    'googlestars':              'google_stars',
    'google_stars':             'google_stars',
    'googlereviewscount':       'google_review_count',
    'google_reviews':           'google_review_count',
    'google_review_count':      'google_review_count',
    'g_maps':                   'g_maps_url',
    'g_maps_claimed':           'g_maps_claimed',
    'google_maps_url':          'g_maps_url',

    # Yelp
    'yelpstars':                'yelp_stars',
    'yelp_stars':               'yelp_stars',
    'yelpreviewscount':         'yelp_review_count',
    'yelp_review_count':        'yelp_review_count',

    # Facebook
    'facebook':                 'facebook_url',
    'facebook_url':             'facebook_url',
    'facebookstars':            'facebook_stars',
    'facebook_stars':           'facebook_stars',
    'facebookreviewscount':     'facebook_review_count',
    'facebookpixel':            'facebook_pixel',
    'ads_facebook':             'ads_facebook',
    'ads_messenger':            'ads_messenger',

    # Instagram
    'instagram':                'instagram_url',
    'instagram_url':            'instagram_url',
    'instagram_name':           'instagram_name',
    'instagram_is_verified':    'instagram_verified',
    'instagram_is_business_account': 'instagram_is_business',
    'instagram_followers':      'instagram_followers',
    'instagram_following':      'instagram_following',
    'instagram_media_count':    'instagram_media_count',
    'instagram_average_likes':  'instagram_avg_likes',
    'instagram_average_comments': 'instagram_avg_comments',
    'ads_instagram':            'ads_instagram',

    # Other social
    'twitter':                  'twitter_url',
    'twitter_url':              'twitter_url',
    'linkedin':                 'linkedin_url',
    'linkedin_url':             'linkedin_url',
    'linkedinanalytics':        'linkedin_analytics',

    # Ads / pixels
    'ads_yelp':                 'ads_yelp',
    'ads_adwords':              'ads_adwords',
    'googlepixel':              'google_pixel',
    'google_pixel':             'google_pixel',
    'criteopixel':              'criteo_pixel',
    'googleanalytics':          'google_analytics',

    # Domain
    'domain_registration':      'domain_registered',
    'domain_expiration':        'domain_expires',
    'domain_registrar':         'domain_registrar',
    'domain_nameserver':        'domain_nameserver',

    # Tech stack
    'uses_wordpress':           'uses_wordpress',
    'uses_shopify':             'uses_shopify',
    'mobilefriendly':           'mobile_friendly',
    'mobile_friendly':          'mobile_friendly',
    'seo_schema':               'seo_schema_present',

    # Email validation
    'Email State':              'email_state',
    'email_state':              'email_state',
    'Email Sub-State':          'email_sub_state',
    'email_sub_state':          'email_sub_state',

    # Search metadata
    'search_keyword':           'search_keyword',
    'search_city':              'search_city',
}

# Boolean fields that come in as 'y'/'n' strings
BOOLEAN_YN_FIELDS = {
    'facebook_pixel', 'google_pixel', 'criteo_pixel', 'google_analytics',
    'linkedin_analytics', 'uses_wordpress', 'uses_shopify', 'mobile_friendly',
    'seo_schema_present', 'ads_yelp', 'ads_facebook', 'ads_instagram',
    'ads_messenger', 'ads_adwords', 'instagram_verified', 'instagram_is_business'
}


# ─── Niche Inference ─────────────────────────────────────────────────────────
# Category mappings are loaded from Supabase category_mappings table at startup.
# Falls back to DB-side fuzzy function if local lookup misses.
# This replaces the old hardcoded dict — now driven by 002_niche_taxonomy_v2.sql

_CATEGORY_CACHE: dict = {}   # raw_category (lower) → niche_slug

def load_category_cache() -> None:
    """Load all category_mappings from DB into memory once at startup."""
    global _CATEGORY_CACHE
    log.info("Loading category mappings from database...")
    result = supabase.table('category_mappings').select('raw_category, niche_slug, keywords').execute()
    for row in result.data:
        # Exact match key
        _CATEGORY_CACHE[row['raw_category'].lower()] = row['niche_slug']
        # Also index every keyword
        for kw in (row.get('keywords') or []):
            if kw and kw.lower() not in _CATEGORY_CACHE:
                _CATEGORY_CACHE[kw.lower()] = row['niche_slug']
    log.info(f"Loaded {len(_CATEGORY_CACHE):,} category/keyword mappings")


def infer_niche(category: str) -> Optional[str]:
    """
    Infer niche slug from raw category string.
    1. Exact match in cache
    2. Substring keyword match in cache
    3. DB-side fuzzy trigram match (fallback)
    4. Returns 'other' if nothing matches
    """
    if not category:
        return 'other'

    cat_lower = str(category).lower().strip()

    # 1. Exact match
    if cat_lower in _CATEGORY_CACHE:
        return _CATEGORY_CACHE[cat_lower]

    # 2. Substring — check if any cached keyword appears in the category
    for keyword, niche in _CATEGORY_CACHE.items():
        if keyword in cat_lower or cat_lower in keyword:
            return niche

    # 3. DB-side fuzzy fallback (only for cache misses — rare)
    try:
        result = supabase.rpc('infer_niche_from_category', {'raw_cat': category}).execute()
        if result.data:
            return result.data
    except Exception:
        pass

    return 'other'


# ─── Normalization Functions ──────────────────────────────────────────────────

def normalize_phone(raw: any) -> Optional[str]:
    """Return 10-digit US phone string or None."""
    if raw is None or str(raw).strip() == '':
        return None
    try:
        # Handle Excel numeric phone like 12125551234
        raw_str = str(int(float(str(raw)))) if str(raw).replace('.', '').isdigit() else str(raw)
        parsed = phonenumbers.parse(raw_str, "US")
        if phonenumbers.is_valid_number(parsed):
            return str(parsed.national_number)  # 10 digits
    except Exception:
        # Fallback: strip non-digits
        digits = re.sub(r'\D', '', str(raw))
        if len(digits) == 11 and digits[0] == '1':
            digits = digits[1:]
        if len(digits) == 10:
            return digits
    return None


def normalize_url(url: any) -> Optional[str]:
    if url is None or str(url).strip() in ('', 'nan'):
        return None
    url_str = str(url).strip()
    if not url_str.startswith('http'):
        url_str = 'https://' + url_str
    return url_str.rstrip('/')


def normalize_boolean_yn(val: any) -> Optional[bool]:
    if val is None:
        return None
    v = str(val).strip().lower()
    if v in ('y', 'yes', 'true', '1'):
        return True
    if v in ('n', 'no', 'false', '0'):
        return False
    return None


def normalize_date(val: any) -> Optional[str]:
    if val is None or str(val).strip() in ('', 'nan', 'None'):
        return None
    if isinstance(val, (datetime, date)):
        return val.strftime('%Y-%m-%d')
    try:
        return pd.to_datetime(val).strftime('%Y-%m-%d')
    except Exception:
        return None


def parse_coords_from_gmaps(url: str) -> tuple[Optional[float], Optional[float]]:
    """Extract lat/lng from Google Maps URL — free geolocation."""
    if not url:
        return None, None
    match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', str(url))
    if match:
        return float(match.group(1)), float(match.group(2))
    return None, None


def make_fingerprint(phone: Optional[str], name: str) -> str:
    """Canonical dedup key: phone (preferred) + normalized name."""
    norm_name = re.sub(r'[^a-z0-9]', '', name.lower()) if name else ''
    if phone:
        raw = f"{phone}:{norm_name}"
    else:
        raw = f"nophone:{norm_name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def compute_domain_age(registered: Optional[str]) -> Optional[int]:
    if not registered:
        return None
    try:
        reg_date = datetime.strptime(registered, '%Y-%m-%d').date()
        return (date.today() - reg_date).days
    except Exception:
        return None


def is_domain_expired(expires: Optional[str]) -> Optional[bool]:
    if not expires:
        return None
    try:
        exp_date = datetime.strptime(expires, '%Y-%m-%d').date()
        return exp_date < date.today()
    except Exception:
        return None


def is_domain_expiring_soon(expires: Optional[str], days: int = 90) -> Optional[bool]:
    if not expires:
        return None
    try:
        from datetime import timedelta
        exp_date = datetime.strptime(expires, '%Y-%m-%d').date()
        return exp_date < date.today() + timedelta(days=days)
    except Exception:
        return None


# ─── Pain Score v1 ────────────────────────────────────────────────────────────
# Computed from CSV data only — no enrichment needed
# Score: 0-100, higher = more pain = better prospect

def compute_pain_score_v1(row: dict, niche_slug: str) -> dict:
    """
    Pain Score v1 — uses only data available from the source CSV.
    Returns sub-scores and composite score.
    """
    # Niche weight defaults (will override with DB values later)
    weights = {
        'website':    0.25,
        'reputation': 0.30,
        'seo':        0.25,
        'social':     0.10,
        'bizintel':   0.10,
    }

    scores = {}

    # ── Safe numeric cast helper ────────────────────────────
    def to_float(val, default=None):
        try:
            return float(val) if val is not None and str(val).strip() not in ('', 'nan') else default
        except (ValueError, TypeError):
            return default

    def to_int(val, default=0):
        try:
            return int(float(val)) if val is not None and str(val).strip() not in ('', 'nan') else default
        except (ValueError, TypeError):
            return default

    # ── Reputation Sub-Score (0-100) ─────────────────────────
    rep = 0
    stars = to_float(row.get('google_stars'))
    reviews = to_int(row.get('google_review_count'), 0)
    claimed = row.get('g_maps_claimed')

    if stars is not None:
        if stars < 3.0:   rep += 40
        elif stars < 3.5: rep += 30
        elif stars < 4.0: rep += 20
        elif stars < 4.5: rep += 10
        else:             rep += 0

    if reviews < 5:    rep += 30
    elif reviews < 10: rep += 20
    elif reviews < 25: rep += 10
    elif reviews > 50: rep += 5

    if claimed == 'unclaimed': rep += 30
    rep = min(rep, 100)
    scores['pain_score_reputation'] = rep

    # ── SEO Sub-Score (0-100) ────────────────────────────────
    seo = 0
    google_rank = to_int(row.get('google_rank'), 0)
    if not row.get('seo_schema_present'):            seo += 40
    if not row.get('mobile_friendly'):               seo += 30
    if not row.get('google_analytics'):              seo += 15
    if not row.get('google_pixel'):                  seo += 10
    if google_rank and google_rank > 10:             seo += 20
    seo = min(seo, 100)
    scores['pain_score_seo'] = seo

    # ── Social Sub-Score (0-100) ─────────────────────────────
    social = 0
    fb_stars = to_float(row.get('facebook_stars'))
    if not row.get('facebook_url'):                  social += 25
    if not row.get('instagram_url'):                 social += 20
    if not row.get('ads_facebook'):                  social += 15
    if not row.get('ads_adwords'):                   social += 15
    if fb_stars is not None and fb_stars < 3.5:      social += 25
    social = min(social, 100)
    scores['pain_score_social'] = social

    # ── Website Sub-Score (from CSV only) ───────────────────
    web = 0
    if not row.get('website'):                  web += 50   # no website at all
    if not row.get('mobile_friendly'):          web += 20
    if row.get('uses_wordpress') is False and not row.get('uses_shopify'): web += 5
    domain_exp = row.get('domain_expired')
    if domain_exp:                              web += 25
    web = min(web, 100)
    scores['pain_score_website'] = web

    # ── Business Intelligence Sub-Score ─────────────────────
    biz = 0
    domain_soon = row.get('domain_expiring_soon')
    if domain_soon:                             biz += 30
    if not row.get('ads_adwords') and not row.get('ads_facebook'): biz += 20
    email_state = row.get('email_state', '')
    if email_state == 'risky':                  biz += 15
    biz = min(biz, 100)
    scores['pain_score_bizintel'] = biz

    # ── Composite Score ──────────────────────────────────────
    composite = (
        scores['pain_score_website']    * weights['website']    +
        scores['pain_score_reputation'] * weights['reputation'] +
        scores['pain_score_seo']        * weights['seo']        +
        scores['pain_score_social']     * weights['social']     +
        scores['pain_score_bizintel']   * weights['bizintel']
    )
    scores['pain_score'] = round(composite, 1)
    scores['pain_score_version'] = 1
    scores['pain_score_computed_at'] = datetime.utcnow().isoformat()

    return scores


def compute_product_fit(row: dict) -> dict:
    """Set product fit boolean flags based on available signals."""
    pain = row.get('pain_score', 0) or 0
    return {
        'fit_demoenginez': (
            pain >= 40 and (
                not row.get('mobile_friendly') or
                not row.get('website') or
                row.get('domain_expired') or
                not row.get('seo_schema_present')
            )
        ),
        'fit_reputation': (
            (row.get('g_maps_claimed') == 'unclaimed') or
            (row.get('google_stars') is not None and row['google_stars'] < 4.0) or
            (row.get('google_review_count', 0) or 0) < 15
        ),
        'fit_schema_ranker': not row.get('seo_schema_present'),
        'fit_voicedrop': pain >= 50 and bool(row.get('phone')),
    }


def compute_data_quality_score(row: dict) -> int:
    """0-100 score for how complete this record is."""
    fields = [
        'business_name', 'phone', 'email', 'website',
        'address', 'city', 'state', 'zip',
        'google_stars', 'google_review_count', 'g_maps_claimed',
        'seo_schema_present', 'mobile_friendly', 'domain_expires',
    ]
    filled = sum(1 for f in fields if row.get(f) is not None and str(row.get(f)).strip() not in ('', 'nan'))
    return round(filled / len(fields) * 100)


# ─── Row Transformation ───────────────────────────────────────────────────────

def transform_row(raw: dict, niche_slug: str, source_file: str) -> dict:
    """Transform a raw CSV row into a KJLE lead record."""

    # Map columns to canonical names
    row = {}
    for raw_col, value in raw.items():
        canonical = COLUMN_MAP.get(raw_col, raw_col)
        row[canonical] = value if not (isinstance(value, float) and pd.isna(value)) else None

    # Safe-cast all numeric fields that might come in as strings
    for int_field in ['google_rank','google_review_count','yelp_review_count',
                      'facebook_review_count','instagram_followers','instagram_following',
                      'instagram_media_count','instagram_highlight_reel_count','zip']:
        val = row.get(int_field)
        if val is not None:
            try:
                row[int_field] = int(float(str(val))) if str(val).strip() not in ('','nan','None') else None
            except (ValueError, TypeError):
                row[int_field] = None

    for float_field in ['google_stars','yelp_stars','facebook_stars',
                        'instagram_avg_likes','instagram_avg_comments']:
        val = row.get(float_field)
        if val is not None:
            try:
                row[float_field] = float(str(val)) if str(val).strip() not in ('','nan','None') else None
            except (ValueError, TypeError):
                row[float_field] = None

    # Normalize phone
    row['phone'] = normalize_phone(row.get('phone_raw'))

    # Normalize website
    row['website'] = normalize_url(row.get('website'))

    # Normalize social URLs
    for url_field in ('facebook_url', 'instagram_url', 'twitter_url', 'linkedin_url', 'g_maps_url'):
        row[url_field] = normalize_url(row.get(url_field))

    # Parse coordinates from Google Maps URL
    lat, lng = parse_coords_from_gmaps(row.get('g_maps_url'))
    row['lat'] = lat
    row['lng'] = lng

    # Normalize boolean y/n fields
    for field in BOOLEAN_YN_FIELDS:
        if field in row:
            row[field] = normalize_boolean_yn(row[field])

    # Normalize dates
    row['domain_registered'] = normalize_date(row.get('domain_registered'))
    row['domain_expires']    = normalize_date(row.get('domain_expires'))

    # Computed domain fields
    row['domain_age_days']      = compute_domain_age(row.get('domain_registered'))
    row['domain_expired']       = is_domain_expired(row.get('domain_expires'))
    row['domain_expiring_soon'] = is_domain_expiring_soon(row.get('domain_expires'))

    # Niche
    row['niche_slug'] = niche_slug or infer_niche(row.get('category'))
    row['niche_raw']  = row.get('category')

    # Source tracking
    row['source_file'] = source_file

    # Fingerprint for dedup
    row['fingerprint'] = make_fingerprint(row.get('phone'), row.get('business_name', ''))

    # Pain Score v1
    pain_scores = compute_pain_score_v1(row, row.get('niche_slug', ''))
    row.update(pain_scores)

    # Product fit flags
    fit_flags = compute_product_fit(row)
    row.update(fit_flags)

    # Data quality score
    row['data_quality_score'] = compute_data_quality_score(row)

    # Clean up fields not in schema
    row.pop('phone_raw', None)
    row.pop('Did you mean', None)

    # Enrichment stage starts at 0
    row['enrichment_stage'] = 0
    row['is_active'] = True
    row['is_duplicate'] = False
    row['export_count'] = 0

    return row


# ─── Deduplication ───────────────────────────────────────────────────────────

def get_existing_fingerprints() -> set:
    """Load all existing fingerprints from DB for dedup check."""
    log.info("Loading existing fingerprints from database...")
    existing = set()
    page_size = 1000
    start = 0
    while True:
        result = (
            supabase.table('leads')
            .select('fingerprint')
            .range(start, start + page_size - 1)
            .execute()
        )
        batch = result.data
        if not batch:
            break
        for record in batch:
            if record.get('fingerprint'):
                existing.add(record['fingerprint'])
        if len(batch) < page_size:
            break
        start += page_size
    log.info(f"Loaded {len(existing):,} existing fingerprints")
    return existing


# ─── Main Ingestion ───────────────────────────────────────────────────────────

def load_file(filepath: Path) -> pd.DataFrame:
    """Load CSV or XLSX into DataFrame."""
    if filepath.suffix.lower() in ('.xlsx', '.xls'):
        return pd.read_excel(filepath, dtype=str)
    else:
        return pd.read_csv(filepath, dtype=str)


def ingest_file(filepath: Path, niche_slug: str, existing_fps: set) -> dict:
    """
    Ingest a single file. Returns stats dict.
    """
    filename = filepath.name
    log.info(f"Ingesting: {filename}")

    # Log import start
    import_record = supabase.table('csv_imports').insert({
        'filename': filename,
        'niche_slug': niche_slug,
        'status': 'running',
        'started_at': datetime.utcnow().isoformat(),
    }).execute()
    import_id = import_record.data[0]['id']

    df = load_file(filepath)
    total_rows = len(df)
    log.info(f"  {total_rows:,} rows in file")

    transformed = []
    duplicates  = 0
    failures    = 0

    for _, raw_row in tqdm(df.iterrows(), total=total_rows, desc=f"  Processing", unit="rows"):
        try:
            row = transform_row(raw_row.to_dict(), niche_slug, filename)

            # Skip if fingerprint already in DB or in this batch
            fp = row.get('fingerprint')
            if fp and fp in existing_fps:
                duplicates += 1
                continue

            if fp:
                existing_fps.add(fp)  # add to in-memory set for within-batch dedup

            transformed.append(row)

        except Exception as e:
            failures += 1
            log.warning(f"  Row failed: {e}")

    imported = 0
    if transformed:
        # Bulk insert in batches of 500
        batch_size = 500
        for i in range(0, len(transformed), batch_size):
            batch = transformed[i:i + batch_size]
            try:
                supabase.table('leads').insert(batch).execute()
                imported += len(batch)
            except Exception as e:
                log.error(f"  Batch insert failed at row {i}: {e}")
                failures += len(batch)

    avg_quality = round(
        sum(r.get('data_quality_score', 0) for r in transformed) / len(transformed)
        if transformed else 0, 1
    )

    # Update import log
    supabase.table('csv_imports').update({
        'rows_in_file':      total_rows,
        'rows_imported':     imported,
        'rows_deduplicated': duplicates,
        'rows_failed':       failures,
        'avg_data_quality':  avg_quality,
        'status':            'complete',
        'completed_at':      datetime.utcnow().isoformat(),
    }).eq('id', import_id).execute()

    stats = {
        'file':        filename,
        'total':       total_rows,
        'imported':    imported,
        'duplicates':  duplicates,
        'failures':    failures,
        'avg_quality': avg_quality,
    }
    log.info(f"  ✓ Imported: {imported:,} | Dupes: {duplicates:,} | Failed: {failures:,} | Avg Quality: {avg_quality}")
    return stats


def ingest_folder(folder: Path, niche_slug: str) -> None:
    """Ingest all CSV/XLSX files in a folder."""
    files = list(folder.glob('*.csv')) + list(folder.glob('*.xlsx')) + list(folder.glob('*.xls'))
    if not files:
        log.error(f"No CSV/XLSX files found in {folder}")
        return

    log.info(f"Found {len(files)} files to ingest")

    # Load existing fingerprints ONCE for the whole folder run
    existing_fps = get_existing_fingerprints()

    totals = {'total': 0, 'imported': 0, 'duplicates': 0, 'failures': 0}

    for filepath in files:
        try:
            stats = ingest_file(filepath, niche_slug, existing_fps)
            for k in totals:
                totals[k] += stats[k]
        except Exception as e:
            log.error(f"File failed entirely: {filepath.name} — {e}")

    log.info("\n" + "="*50)
    log.info("INGESTION COMPLETE")
    log.info(f"  Files processed:  {len(files)}")
    log.info(f"  Total rows:       {totals['total']:,}")
    log.info(f"  Imported:         {totals['imported']:,}")
    log.info(f"  Duplicates:       {totals['duplicates']:,}")
    log.info(f"  Failures:         {totals['failures']:,}")
    log.info("="*50)


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Load category → niche mappings from DB once at startup
    load_category_cache()

    parser = argparse.ArgumentParser(description='KJLE Lead Ingestion Script')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--file',   type=Path, help='Single CSV or XLSX file to ingest')
    group.add_argument('--folder', type=Path, help='Folder of CSV/XLSX files to ingest')
    parser.add_argument('--niche', type=str, default=None,
                        help='Niche slug (e.g. hvac, plumber). If omitted, inferred from category column.')
    args = parser.parse_args()

    if args.file:
        if not args.file.exists():
            log.error(f"File not found: {args.file}")
            sys.exit(1)
        existing_fps = get_existing_fingerprints()
        ingest_file(args.file, args.niche, existing_fps)

    elif args.folder:
        if not args.folder.exists():
            log.error(f"Folder not found: {args.folder}")
            sys.exit(1)
        ingest_folder(args.folder, args.niche)
