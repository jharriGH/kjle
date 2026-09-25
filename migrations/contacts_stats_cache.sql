-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — extend refresh_stats_cache() with contacts aggregates
-- Date: 2026-09-25
-- Run manually in Supabase SQL editor (CREATE OR REPLACE — idempotent, safe
-- to re-run). No new tables/columns; reuses the existing stats_cache table
-- and the existing pg_cron schedule ('refresh-stats-cache', every 30 min).
--
-- Adds four new stats_cache rows, written by the SAME function/cron that
-- already writes 'lead_stats' and 'segments_by_niche':
--   'contacts_email_status'   — {status: count} over contacts.email_status
--   'contacts_email_trust'    — {trust: count}  over contacts.email_trust
--   'contacts_email_provider' — {provider: count} over contacts.email_provider
--   'contacts_cleaned_recent' — {cleaned_total, cleaned_last_24h, cleaned_last_7d}
--                                derived from contacts.email_cleaned_at
--
-- Consumed by api/routes/contacts.py's new GET /contacts/category-breakdown
-- endpoint. Read-only fast path there falls back to {} on cache miss/stale —
-- never a live full-table scan.
-- ────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION refresh_stats_cache()
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_lead_stats              JSONB;
    v_segments_by_niche       JSONB;
    v_contacts_email_status   JSONB;
    v_contacts_email_trust    JSONB;
    v_contacts_email_provider JSONB;
    v_contacts_cleaned_recent JSONB;
BEGIN
    -- ── lead_stats (unchanged) ──────────────────────────────────────────────
    WITH
    totals AS (
        SELECT
            (SELECT c.reltuples::BIGINT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relname='leads' AND n.nspname='public') AS total,
            COUNT(*) FILTER (WHERE phone IS NOT NULL) AS has_phone,
            COUNT(*) FILTER (WHERE email IS NOT NULL) AS has_email,
            COUNT(*) FILTER (WHERE do_not_contact = TRUE) AS dnc_count
        FROM leads
    ),
    by_seg AS (
        SELECT jsonb_object_agg(seg, cnt) AS by_segment
        FROM (
            SELECT COALESCE(segment_label, 'unclassified') AS seg, COUNT(*) AS cnt
            FROM leads GROUP BY 1
        ) s
    ),
    by_es AS (
        SELECT jsonb_object_agg(es, cnt) AS by_email_status
        FROM (
            SELECT COALESCE(email_status, 'unknown') AS es, COUNT(*) AS cnt
            FROM leads GROUP BY 1
        ) s
    )
    SELECT jsonb_build_object(
        'total', t.total, 'by_segment', bs.by_segment,
        'by_email_status', be.by_email_status, 'dnc_count', t.dnc_count,
        'has_phone', t.has_phone, 'has_email', t.has_email)
    INTO v_lead_stats
    FROM totals t, by_seg bs, by_es be;

    -- ── segments_by_niche (unchanged) ───────────────────────────────────────
    SELECT jsonb_agg(row_to_json(r)::JSONB)
    INTO v_segments_by_niche
    FROM (
        SELECT
            COALESCE(niche_slug, 'unknown')                AS niche_slug,
            COUNT(*) FILTER (WHERE segment_label = 'hot')  AS hot_count,
            COUNT(*) FILTER (WHERE segment_label = 'warm') AS warm_count,
            COUNT(*) FILTER (WHERE segment_label = 'cold') AS cold_count,
            COUNT(*) FILTER (WHERE segment_label IS NULL)  AS unclassified_count,
            COUNT(*)                                       AS total
        FROM leads
        WHERE is_active = TRUE
        GROUP BY COALESCE(niche_slug, 'unknown')
        ORDER BY COUNT(*) DESC
    ) r;

    -- ── contacts.email_status breakdown (new) ───────────────────────────────
    SELECT jsonb_object_agg(es, cnt) INTO v_contacts_email_status
    FROM (
        SELECT COALESCE(email_status, 'unknown') AS es, COUNT(*) AS cnt
        FROM contacts GROUP BY 1
    ) s;

    -- ── contacts.email_trust breakdown (new) ────────────────────────────────
    SELECT jsonb_object_agg(et, cnt) INTO v_contacts_email_trust
    FROM (
        SELECT COALESCE(email_trust, 'unknown') AS et, COUNT(*) AS cnt
        FROM contacts GROUP BY 1
    ) s;

    -- ── contacts.email_provider breakdown (new) ─────────────────────────────
    SELECT jsonb_object_agg(ep, cnt) INTO v_contacts_email_provider
    FROM (
        SELECT COALESCE(email_provider, 'unknown') AS ep, COUNT(*) AS cnt
        FROM contacts GROUP BY 1
    ) s;

    -- ── contacts.email_cleaned_at recent-count (new) ────────────────────────
    SELECT jsonb_build_object(
        'cleaned_total',    COUNT(*) FILTER (WHERE email_cleaned_at IS NOT NULL),
        'cleaned_last_24h', COUNT(*) FILTER (WHERE email_cleaned_at > NOW() - INTERVAL '24 hours'),
        'cleaned_last_7d',  COUNT(*) FILTER (WHERE email_cleaned_at > NOW() - INTERVAL '7 days')
    )
    INTO v_contacts_cleaned_recent
    FROM contacts;

    -- ── upsert all six rows ──────────────────────────────────────────────────
    INSERT INTO stats_cache (key, data, refreshed_at) VALUES ('lead_stats', v_lead_stats, NOW())
        ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, refreshed_at = EXCLUDED.refreshed_at;

    INSERT INTO stats_cache (key, data, refreshed_at) VALUES ('segments_by_niche', v_segments_by_niche, NOW())
        ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, refreshed_at = EXCLUDED.refreshed_at;

    INSERT INTO stats_cache (key, data, refreshed_at) VALUES ('contacts_email_status', v_contacts_email_status, NOW())
        ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, refreshed_at = EXCLUDED.refreshed_at;

    INSERT INTO stats_cache (key, data, refreshed_at) VALUES ('contacts_email_trust', v_contacts_email_trust, NOW())
        ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, refreshed_at = EXCLUDED.refreshed_at;

    INSERT INTO stats_cache (key, data, refreshed_at) VALUES ('contacts_email_provider', v_contacts_email_provider, NOW())
        ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, refreshed_at = EXCLUDED.refreshed_at;

    INSERT INTO stats_cache (key, data, refreshed_at) VALUES ('contacts_cleaned_recent', v_contacts_cleaned_recent, NOW())
        ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, refreshed_at = EXCLUDED.refreshed_at;
END;
$$;

GRANT EXECUTE ON FUNCTION refresh_stats_cache() TO service_role;

-- ── Verification ─────────────────────────────────────────────────────────────
-- SELECT refresh_stats_cache();  -- run once manually to seed the four new keys immediately
-- SELECT key, refreshed_at, data FROM stats_cache WHERE key LIKE 'contacts_%' ORDER BY key;
-- Expected: 4 rows, all refreshed_at within the last 30 min going forward.
