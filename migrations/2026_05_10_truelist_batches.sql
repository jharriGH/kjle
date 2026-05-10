-- KJLE — Truelist batch architecture (Session 2B, 2026-05-10)
-- Adds the audit/state table for in-flight batches plus two lead columns
-- so we can preserve Truelist sub-state and back-reference the batch
-- that produced each classification.
--
-- Idempotent. Safe to re-run.

-- ─────────────────────────────────────────────────────────────────────────────
-- Table: truelist_batches
--   One row per submission. Audit trail + lets the poller find in-flight
--   batches without scanning the leads table.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS truelist_batches (
    id                  UUID PRIMARY KEY,                -- Truelist's batch id
    submitted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    email_count         INT  NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending', -- pending | processing | completed | failed | stale | ingested
    completed_at        TIMESTAMPTZ NULL,
    ingested_at         TIMESTAMPTZ NULL,                -- when annotated CSV was applied to leads
    annotated_csv_url   TEXT NULL,
    safest_bet_csv_url  TEXT NULL,
    highest_reach_csv_url TEXT NULL,
    only_invalid_csv_url  TEXT NULL,
    error               TEXT NULL,
    -- Per-row counts from Truelist (populated at completion)
    ok_count                  INT NULL,
    ok_for_all_count          INT NULL,
    role_count                INT NULL,
    disposable_count          INT NULL,
    failed_syntax_check_count INT NULL,
    failed_mx_check_count     INT NULL,
    failed_no_mailbox_count   INT NULL,
    -- Provenance/debug
    submitted_by        TEXT NULL,                       -- 'nightly_cron' | 'manual_submit' | 'backfill'
    notes               TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_truelist_batches_status
  ON truelist_batches(status)
  WHERE status IN ('pending', 'processing');

CREATE INDEX IF NOT EXISTS idx_truelist_batches_submitted_at
  ON truelist_batches(submitted_at DESC);


-- ─────────────────────────────────────────────────────────────────────────────
-- leads: new columns
--   email_sub_state           — Truelist sub_state preserved (e.g. is_role,
--                                accept_all, failed_mx_check) for downstream
--                                filtering even when email_state=risky.
--   email_truelist_batch_id   — back-reference to truelist_batches row.
--                                Used by CSV ingestion to scope updates and by
--                                the recovery path when a batch is marked stale.
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE leads
  ADD COLUMN IF NOT EXISTS email_sub_state TEXT NULL;

ALTER TABLE leads
  ADD COLUMN IF NOT EXISTS email_truelist_batch_id UUID NULL
    REFERENCES truelist_batches(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_leads_email_truelist_batch_id
  ON leads(email_truelist_batch_id)
  WHERE email_truelist_batch_id IS NOT NULL;

-- Partial index so the nightly submitter's "uncleaned" query stays fast even as
-- the table grows.  Selection predicate is: email IS NOT NULL AND email <> ''
-- AND email_cleaned_at IS NULL AND email_status IS DISTINCT FROM 'pending_batch'.
CREATE INDEX IF NOT EXISTS idx_leads_email_uncleaned
  ON leads(pain_score DESC, id)
  WHERE email IS NOT NULL
    AND email <> ''
    AND email_cleaned_at IS NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- Admin settings: new keys (idempotent — only inserts if missing)
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO admin_settings (key, value)
VALUES ('email_clean_max_batches_per_night', '4')
ON CONFLICT (key) DO NOTHING;

INSERT INTO admin_settings (key, value)
VALUES ('email_clean_batch_size_emails', '25000')
ON CONFLICT (key) DO NOTHING;
