-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Campaign Lead Attachments: ALTER migration
-- Date: 2026-06-20
-- Table already exists live with:
--   lead_id UUID NOT NULL, provider_campaign_id TEXT NOT NULL,
--   UNIQUE idx uq_cla_lead_campaign(lead_id, provider_campaign_id).
-- Adds new telemetry/lookup columns. All ADD COLUMN calls are IF NOT EXISTS.
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE campaign_lead_attachments
    ADD COLUMN IF NOT EXISTS reachinbox_campaign_id TEXT,
    ADD COLUMN IF NOT EXISTS email                  TEXT,
    ADD COLUMN IF NOT EXISTS phone                  TEXT,
    ADD COLUMN IF NOT EXISTS vertical               TEXT,
    ADD COLUMN IF NOT EXISTS campaign_name          TEXT,
    ADD COLUMN IF NOT EXISTS project                TEXT DEFAULT 'kjle',
    ADD COLUMN IF NOT EXISTS kje_product            TEXT,
    ADD COLUMN IF NOT EXISTS source                 TEXT,
    ADD COLUMN IF NOT EXISTS metadata               JSONB,
    ADD COLUMN IF NOT EXISTS attached_at            TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- email+time index is the primary lookup key for overlap checks
CREATE INDEX IF NOT EXISTS idx_cla_email_attached ON campaign_lead_attachments (email, attached_at DESC);
CREATE INDEX IF NOT EXISTS idx_cla_leadid         ON campaign_lead_attachments (lead_id);
CREATE INDEX IF NOT EXISTS idx_cla_ricid          ON campaign_lead_attachments (reachinbox_campaign_id);
CREATE INDEX IF NOT EXISTS idx_cla_product        ON campaign_lead_attachments (kje_product);

ALTER TABLE campaign_lead_attachments DISABLE ROW LEVEL SECURITY;
GRANT ALL ON campaign_lead_attachments TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;

-- Tunable cooldown (days) used by attachment-exclusion filter in
-- eligible_for_campaign and the /campaigns/lead-overlap endpoint.
INSERT INTO admin_settings (key, value, updated_at) VALUES
    ('campaign_attach_cooldown_days', '30', NOW())
ON CONFLICT (key) DO NOTHING;


-- ── Verification ─────────────────────────────────────────────────────────────
-- SELECT count(*) FROM campaign_lead_attachments;
-- SELECT key, value FROM admin_settings WHERE key = 'campaign_attach_cooldown_days';
