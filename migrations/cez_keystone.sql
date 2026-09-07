-- CampaignEnginez Keystone — apply all three migration items
-- Project: dhzpwobfihrprlcxqjbq
-- Run in Supabase SQL editor: https://app.supabase.com/project/dhzpwobfihrprlcxqjbq/editor
-- All statements are idempotent (IF NOT EXISTS / CREATE OR REPLACE).

-- ── ITEM 1: enrichment jsonb column ────────────────────────────────────────────
ALTER TABLE leads ADD COLUMN IF NOT EXISTS enrichment jsonb DEFAULT '{}'::jsonb;

-- ── ITEM 2: get_provider_breakdown RPC ─────────────────────────────────────────
CREATE OR REPLACE FUNCTION get_provider_breakdown(
    p_niche_slug    text DEFAULT NULL,
    p_segment_label text DEFAULT NULL,
    p_email_status  text DEFAULT NULL
)
RETURNS TABLE(provider text, lead_count bigint) AS $$
BEGIN
    RETURN QUERY
    SELECT
        COALESCE(l.email_provider, 'unknown') AS provider,
        COUNT(*)::bigint AS lead_count
    FROM leads l
    WHERE l.is_active = true
      AND (p_niche_slug    IS NULL OR l.niche_slug    = p_niche_slug)
      AND (p_segment_label IS NULL OR l.segment_label = p_segment_label)
      AND (p_email_status  IS NULL OR l.email_status  = p_email_status)
    GROUP BY COALESCE(l.email_provider, 'unknown');
END;
$$ LANGUAGE plpgsql STABLE;

-- ── ITEM 3: contact history + contact-tracking columns on leads ─────────────────
CREATE TABLE IF NOT EXISTS lead_campaign_history (
    id            bigserial PRIMARY KEY,
    lead_id       uuid REFERENCES leads(id) ON DELETE CASCADE,
    campaign_id   text NOT NULL,
    product       text NOT NULL,
    sequence_step int  NOT NULL DEFAULT 1,
    status        text NOT NULL DEFAULT 'sent',
    contacted_at  timestamptz NOT NULL DEFAULT now(),
    metadata      jsonb,
    UNIQUE (lead_id, campaign_id, sequence_step)
);

CREATE INDEX IF NOT EXISTS idx_lch_lead_id      ON lead_campaign_history(lead_id);
CREATE INDEX IF NOT EXISTS idx_lch_product      ON lead_campaign_history(product, contacted_at DESC);
CREATE INDEX IF NOT EXISTS idx_lch_lead_product ON lead_campaign_history(lead_id, product);
CREATE INDEX IF NOT EXISTS idx_lch_campaign_id  ON lead_campaign_history(campaign_id);

ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_contacted_at timestamptz;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS contact_count int DEFAULT 0;
