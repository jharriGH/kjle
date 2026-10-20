-- KJLE — Partial index for select_revalidation_leads (fixes 57014 timeout)
-- Date: 2026-10-11
-- WHY: select_revalidation_leads queries leads WHERE email_status='unknown'
-- AND email_truelist_batch_id IS NULL AND email_cleaned_at < cutoff
-- ORDER BY email_cleaned_at ASC. Without a supporting index Postgres does a
-- full-table seq scan on ~470K leads, reliably hitting the statement_timeout
-- (57014). A partial index covering only the unknown/unassigned subset and
-- pre-sorted on email_cleaned_at turns that into a fast index scan.
--
-- MUST run via session-mode pooler (port 5432) or direct connection — NOT the
-- transaction-mode pooler (port 6543). CONCURRENTLY cannot run inside a
-- transaction block.

SET statement_timeout = 0;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_leads_reval_unknown_cleaned_at
    ON public.leads (email_cleaned_at ASC)
    WHERE email_status = 'unknown'
      AND email_truelist_batch_id IS NULL
      AND is_active = true;

-- After creation, run to keep planner stats current:
--   VACUUM (ANALYZE) public.leads;
