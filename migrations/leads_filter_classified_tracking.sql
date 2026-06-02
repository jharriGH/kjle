-- KJLE — Phase 4 Layer 5B Stage 1 (test-fire backlog contactability scrub)
-- Add bookkeeping columns to track WHICH backlog leads have already been
-- run through the local-only lead_filters + phone_filters classifiers, and
-- by which run / label they were classified.
--
-- filter_classified_at → timestamp of the most recent classifier pass for
--                        this row. NULL means the row has never been
--                        scrubbed (i.e. it is still eligible for the
--                        backlog test-fire selector).
-- filter_classified_by → free-form label identifying the run that stamped
--                        this row (e.g. "stage1_test_2026_06_01"). Lets
--                        Jim filter / un-stamp a specific sweep.
--
-- Partial index on filter_classified_at IS NOT NULL keeps the index small:
-- once the backlog is fully scrubbed most rows have it set, but the
-- selector reads the IS NULL set which Postgres can satisfy via the
-- partial-index NULL exclusion plus a seq-scan fallback on the
-- (small, shrinking) unscrubbed remainder.
--
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS — safe
-- to re-run.

ALTER TABLE leads ADD COLUMN IF NOT EXISTS filter_classified_at timestamptz;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS filter_classified_by text;

CREATE INDEX IF NOT EXISTS leads_filter_classified_at_idx
  ON leads (filter_classified_at)
  WHERE filter_classified_at IS NOT NULL;

GRANT ALL ON leads TO service_role;
