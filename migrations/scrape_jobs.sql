-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Scrape Job Queue (Phase 4.2 — Local Scraper Worker Integration)
-- Date: 2026-06-04
-- Run once in Supabase SQL editor (project: dhzpwobfihrprlcxqjbq).
--
-- Creates:
--   scrape_jobs — server-of-truth queue for Local Scraper jobs that
--                 stateless worker daemons (z820, vps) poll every ~15s.
--
-- Worker flow:
--   1. POST /kjle/v1/scrape/start          → operator/n8n inserts row (queued)
--   2. GET  /kjle/v1/scrape/jobs/poll      → worker claims (CAS-locked)
--   3. POST /kjle/v1/scrape/jobs/{id}/started   → worker reports start
--   4. POST /kjle/v1/scrape/jobs/{id}/complete  → worker reports result
--
-- Idempotent. Safe to re-run.
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.scrape_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Job parameters (mapped to Local Scraper CLI args)
    target          TEXT NOT NULL,             -- e.g. "Google Maps Quick"
    keyword         TEXT,                       -- single keyword (or null if using list)
    location        TEXT,                       -- single location (or null if using list)
    keyword_list    TEXT,                       -- path string sent to worker
    location_list   TEXT,
    custom_url      TEXT,
    max_listings    INTEGER,                    -- optional cap
    find_emails     BOOLEAN DEFAULT FALSE,

    -- Routing
    requested_worker TEXT NOT NULL,             -- 'z820' | 'vps' | 'auto'
    assigned_worker  TEXT,                      -- set on claim
    status           TEXT NOT NULL DEFAULT 'queued',
                     -- queued | claimed | running | complete | failed | cancelled

    -- Lifecycle tracking
    queued_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at       TIMESTAMPTZ,
    started_at       TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ,
    failed_at        TIMESTAMPTZ,

    -- Results (set by worker via complete endpoint)
    result_run_id          TEXT,                -- run_id from LocalScraper webhook
    result_total_records   INTEGER,
    result_inserted        INTEGER,
    result_duplicates      INTEGER,
    result_filtered        INTEGER,
    error_message          TEXT,

    -- Metadata for n8n / operator tracking
    requested_by     TEXT,                     -- 'operator' | 'n8n' | other
    correlation_id   TEXT,                     -- for n8n round-trip tracking
    metadata         JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS scrape_jobs_status_queued_idx
  ON public.scrape_jobs (queued_at)
  WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS scrape_jobs_assigned_worker_status_idx
  ON public.scrape_jobs (assigned_worker, status);

CREATE INDEX IF NOT EXISTS scrape_jobs_created_at_desc_idx
  ON public.scrape_jobs (created_at DESC);

-- Disable RLS; service_role full access
ALTER TABLE public.scrape_jobs DISABLE ROW LEVEL SECURITY;
GRANT ALL ON public.scrape_jobs TO service_role;

NOTIFY pgrst, 'reload schema';
