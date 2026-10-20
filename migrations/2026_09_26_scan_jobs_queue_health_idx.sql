-- Partial indexes to support GET /kjle/v1/scan/queue aggregate queries
-- against scan_jobs (~1.7M rows, polled every 60s).
--
-- IMPORTANT: CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
-- Run this file outside any BEGIN/COMMIT wrapper (e.g. psql -f, not a migration
-- runner that auto-wraps in a transaction). statement_timeout=0 prevents
-- long-running builds from being cancelled.
--
-- The five partial indexes below replace full-table scans that would otherwise
-- occur for each aggregate in the queue-health endpoint:
--   COUNT(*)           WHERE status='running'                  → idx_scan_jobs_running
--   COUNT(*)           WHERE status='queued'                   → idx_scan_jobs_queued
--   COUNT(*)           WHERE status='queued' AND priority>=9   → idx_scan_jobs_queued_priority
--   MIN(enqueued_at)   WHERE status='queued'                   → idx_scan_jobs_queued_enqueued
--   MAX(finished_at)   WHERE status='done'                     → idx_scan_jobs_done_finished

SET statement_timeout = 0;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scan_jobs_running
    ON scan_jobs (id)
    WHERE status = 'running';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scan_jobs_queued
    ON scan_jobs (id)
    WHERE status = 'queued';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scan_jobs_queued_priority
    ON scan_jobs (priority)
    WHERE status = 'queued';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scan_jobs_queued_enqueued
    ON scan_jobs (enqueued_at)
    WHERE status = 'queued';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scan_jobs_done_finished
    ON scan_jobs (finished_at)
    WHERE status = 'done';
