-- KJLE — DNC Whitelist
-- File: migrations/dnc_whitelist.sql
--
-- A whitelist hit overrides every downstream check in /kjle/v1/dnc/check
-- (suppressions, cache, budget guard, Searchbug). Use only for numbers
-- you have explicit consent to contact regardless of DNC status — e.g.,
-- existing customers, internal test lines, business partners.
--
-- Phone format: E.164 ("+1" + 10 digits). The check route normalizes
-- before lookup, so this column must store E.164 too.

CREATE TABLE IF NOT EXISTS dnc_whitelist (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone     TEXT UNIQUE NOT NULL,
    reason    TEXT,
    source    TEXT,
    notes     TEXT,
    added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dnc_whitelist_phone ON dnc_whitelist(phone);
