-- mail_suppressions: do-not-mail physical address suppression table
-- Applied manually by Jim. Do NOT auto-run.
-- Generated: 2026-07-12

CREATE TABLE IF NOT EXISTS mail_suppressions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    address_normalized  text NOT NULL,     -- normalized street+zip key (lowercased, alnum-only, "street:zip5")
    lead_id             uuid,              -- optional: suppress a specific lead
    reason              text,
    source              text,
    suppressed_at       timestamptz DEFAULT now(),
    notes               text,
    metadata            jsonb
);

CREATE INDEX IF NOT EXISTS idx_mail_suppressions_addr ON mail_suppressions(address_normalized);
CREATE INDEX IF NOT EXISTS idx_mail_suppressions_lead ON mail_suppressions(lead_id) WHERE lead_id IS NOT NULL;
