-- contacts.is_realtor — precomputed + indexed realtor-title flag
-- Supabase project: dhzpwobfihrprlcxqjbq
--
-- Fixes GET /kjle/v1/contacts/eligible-for-campaign?title_is_realtor=true
-- timing out (Postgres 57014, statement timeout) at deep offsets: the raw
-- `title ~* '<realtor pattern>'` regex over the 1.69M-row contacts table
-- had no index support, forcing a full scan+regex-evaluate before any
-- OFFSET could even be applied.
--
-- Backfill (run once, batched by a uuid hash bucket to stay under the
-- statement timeout on this table size -- NOT included here since it's a
-- one-time data operation, not a schema change; already run against the
-- live project for this fix):
--
--   UPDATE contacts SET is_realtor = true
--   WHERE is_realtor = false
--     AND mod(abs(hashtext(id::text)), 20) = <0..19>
--     AND title ~* '(realtor|real estate agent|listing agent|sales agent|
--                    associate broker|broker associate|real estate broker|
--                    sales associate)';

ALTER TABLE public.contacts ADD COLUMN IF NOT EXISTS is_realtor boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_contacts_realtor_eligible
    ON public.contacts (niche_slug, state, email_trust, id)
    WHERE is_realtor;
