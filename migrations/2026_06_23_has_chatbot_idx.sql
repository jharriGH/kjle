-- KJLE — Backfill missing partial index on has_chatbot
-- Date: 2026-06-23
-- Run in Supabase SQL editor.
--
-- WHY: leads_website_quality_signals.sql (2026-06-20) added this column and its
-- index, but the index was never applied in production.  Without it, queries with
-- has_chatbot=false seq-scan all ~1.49M rows and hit statement_timeout (57014).
-- has_chatbot=true was fast enough to slip under the timeout; false was not.
--
-- The partial index covers only audited rows (IS NOT NULL), keeping it small.
-- PostgreSQL can satisfy both "= true" and "= false" predicates via this index
-- because each implies has_chatbot IS NOT NULL.
--
-- NOT CONCURRENTLY — required for Supabase SQL editor.
-- IF NOT EXISTS — safe to re-run if the migration is applied again later.

CREATE INDEX IF NOT EXISTS idx_leads_has_chatbot
    ON leads (has_chatbot)
    WHERE has_chatbot IS NOT NULL;
