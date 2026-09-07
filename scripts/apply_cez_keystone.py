#!/usr/bin/env python3
"""
Apply CampaignEnginez Keystone migrations to Supabase.
Requires: DATABASE_URL=postgresql://postgres:<password>@db.dhzpwobfihrprlcxqjbq.supabase.co:5432/postgres

Usage:
    DATABASE_URL=postgresql://postgres:PASS@db.dhzpwobfihrprlcxqjbq.supabase.co:5432/postgres \
        python3 scripts/apply_cez_keystone.py
"""

import os, sys

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set.")
    print("Get it from: https://app.supabase.com/project/dhzpwobfihrprlcxqjbq/settings/database")
    print("Or apply migrations/cez_keystone.sql manually in the Supabase SQL editor:")
    print("  https://app.supabase.com/project/dhzpwobfihrprlcxqjbq/editor")
    sys.exit(1)

SQL = """
ALTER TABLE leads ADD COLUMN IF NOT EXISTS enrichment jsonb DEFAULT '{}'::jsonb;

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
"""

try:
    import psycopg2
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

try:
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=15)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(SQL)
    conn.close()
    print("Migrations applied successfully.")
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)
