-- One-shot backfill: harvest any TCPA-flagged phones already in dnc_cache
-- into tcpa_litigators. Idempotent (uses ON CONFLICT DO NOTHING).
INSERT INTO tcpa_litigators (phone, source, last_refreshed_at, metadata)
SELECT
  phone,
  'searchbug_harvest_backfill',
  fetched_at,
  jsonb_build_object(
    'harvested_from', 'dnc_cache_backfill',
    'carrier',        COALESCE(carrier, ''),
    'line_type',      COALESCE(line_type, 'unknown'),
    'dnc_reason',     COALESCE(dnc_reason, '')
  )
FROM dnc_cache
WHERE tcpa_litigator = true
ON CONFLICT (phone) DO NOTHING;
