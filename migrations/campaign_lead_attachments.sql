-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Campaign Lead Attachments
-- Date: 2026-06-20
-- Run AFTER migrations/campaign_performance.sql.
--
-- Records every lead→campaign attachment for dedup, overlap detection, and
-- cross-product scoping. Forward-only — no backfill (confirmed per Jim).
-- Status is NOT stored here; resolve live by joining reachinbox_campaign_id
-- → campaign_performance.status.
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS campaign_lead_attachments (
    id                     BIGSERIAL PRIMARY KEY,
    lead_id                UUID,                 -- soft ref to leads.id; nullable (email-only pushes)
    email                  TEXT,                 -- denormalized — canonical lookup key
    phone                  TEXT,                 -- denormalized, optional
    reachinbox_campaign_id TEXT NOT NULL,        -- RI campaign UUID; joins to campaign_performance
    campaign_name          TEXT,
    project                TEXT NOT NULL DEFAULT 'kjle',
    kje_product            TEXT,                 -- overlap scope (cfb, reviewbombz, ...) — null ok
    attached_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata               JSONB
);

-- email+time index is the primary lookup key for overlap checks
CREATE INDEX IF NOT EXISTS idx_cla_email_attached ON campaign_lead_attachments (email, attached_at DESC);
CREATE INDEX IF NOT EXISTS idx_cla_leadid         ON campaign_lead_attachments (lead_id);
CREATE INDEX IF NOT EXISTS idx_cla_ricid          ON campaign_lead_attachments (reachinbox_campaign_id);
CREATE INDEX IF NOT EXISTS idx_cla_product        ON campaign_lead_attachments (kje_product);

ALTER TABLE campaign_lead_attachments DISABLE ROW LEVEL SECURITY;
GRANT ALL ON campaign_lead_attachments TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;

-- Tunable cooldown (days) used by the attachment-exclusion filter in
-- eligible_for_campaign and the /campaigns/lead-overlap endpoint.
INSERT INTO admin_settings (key, value, updated_at) VALUES
    ('campaign_attach_cooldown_days', '30', NOW())
ON CONFLICT (key) DO NOTHING;


-- ── Verification ─────────────────────────────────────────────────────────────
-- SELECT count(*) FROM campaign_lead_attachments;
-- SELECT key, value FROM admin_settings WHERE key = 'campaign_attach_cooldown_days';
