CREATE TABLE IF NOT EXISTS scan_jobs (
  id bigserial PRIMARY KEY,
  url text NOT NULL, lead_id uuid, client_id uuid,
  priority integer NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'queued',
  attempts integer NOT NULL DEFAULT 0,
  enqueued_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz, finished_at timestamptz,
  scan_result_id bigint, error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_scan_jobs_claim ON scan_jobs (status, priority DESC, enqueued_at ASC);
ALTER TABLE scan_jobs DISABLE ROW LEVEL SECURITY;
GRANT ALL ON scan_jobs TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;
ANALYZE scan_jobs;
