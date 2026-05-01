-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — segments_by_niche() Postgres function
-- Date: 2026-04-30
-- Run once in Supabase SQL editor (project: dhzpwobfihrprlcxqjbq).
--
-- Replaces in-Python aggregation in /kjle/v1/segments/by-niche and
-- /kjle/v1/segments/summary (top_5_niches_by_hot section). The previous
-- implementation pulled raw rows and grouped client-side, which silently
-- truncated to ~590 rows out of 514K leads due to the PostgREST default
-- row cap.
--
-- Idempotent (CREATE OR REPLACE).
-- ────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION segments_by_niche(
    p_state                 TEXT    DEFAULT NULL,
    p_include_unclassified  BOOLEAN DEFAULT TRUE
)
RETURNS TABLE (
    niche_slug          TEXT,
    hot_count           BIGINT,
    warm_count          BIGINT,
    cold_count          BIGINT,
    unclassified_count  BIGINT,
    total               BIGINT
)
LANGUAGE SQL STABLE
AS $$
    SELECT
        COALESCE(leads.niche_slug, 'unknown')                          AS niche_slug,
        COUNT(*) FILTER (WHERE leads.segment_label = 'hot')            AS hot_count,
        COUNT(*) FILTER (WHERE leads.segment_label = 'warm')           AS warm_count,
        COUNT(*) FILTER (WHERE leads.segment_label = 'cold')           AS cold_count,
        COUNT(*) FILTER (WHERE leads.segment_label IS NULL)            AS unclassified_count,
        COUNT(*)                                                       AS total
    FROM leads
    WHERE leads.is_active = TRUE
      AND (p_state IS NULL OR leads.state = UPPER(p_state))
      AND (p_include_unclassified OR leads.segment_label IS NOT NULL)
    GROUP BY COALESCE(leads.niche_slug, 'unknown')
    ORDER BY total DESC;
$$;


-- ── Verification queries (safe to run any time) ─────────────────────────────
-- SELECT niche_count := COUNT(*) FROM segments_by_niche();
-- SELECT * FROM segments_by_niche() LIMIT 10;
-- SELECT * FROM segments_by_niche(p_state := 'CA') LIMIT 10;
-- SELECT * FROM segments_by_niche(p_include_unclassified := FALSE) LIMIT 10;
