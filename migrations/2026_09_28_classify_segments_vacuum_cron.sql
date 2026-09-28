-- KJLE — Nightly VACUUM (ANALYZE) on leads via pg_cron
-- Date: 2026-09-28
-- WHY: classify_segments uses covering partial indexes (idx_leads_seg_*_cov).
-- Those indexes can be used for true Index Only Scans only when the
-- visibility map is up-to-date. A stale visibility map forces heap fetches,
-- which inflates per-chunk runtime and risks the ~8s statement_timeout (57014).
-- Autovacuum alone is not aggressive enough on a high-churn table like leads.
-- We schedule an explicit nightly VACUUM at 03:30 UTC so the visibility map
-- is always fresh before the 06:05 classify_segments cron run.
--
-- VACUUM cannot run inside a transaction block; pg_cron executes it as a
-- standalone statement outside any transaction, so this is safe.
--
-- Run via: session-mode pooler (port 5432) or direct connection — NOT the
-- transaction-mode pooler (port 6543), which wraps statements in a txn.

SELECT cron.unschedule(jobid)
    FROM cron.job
    WHERE jobname = 'vacuum-leads-nightly';

SELECT cron.schedule(
    'vacuum-leads-nightly',
    '30 3 * * *',
    'VACUUM (ANALYZE) public.leads;'
);
