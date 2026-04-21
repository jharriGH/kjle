-- ────────────────────────────────────────────────────────────────────────────
-- KJLE — Hard Budget Guardrails + Cost Logging + Daily Report
-- Date: 2026-04-20
-- Run once in Supabase SQL editor (project: dhzpwobfihrprlcxqjbq).
--
-- Idempotent: safe to re-run. CREATE TABLE IF NOT EXISTS on tables,
-- ON CONFLICT DO UPDATE on admin_settings rows.
-- ────────────────────────────────────────────────────────────────────────────

-- ── Table 1: enrichment_cost_log ─────────────────────────────────────────────
-- Authoritative per-call cost ledger read by api/lib/cost_guard.check_budget().
-- Callers insert one row per paid API call AFTER a successful response.
CREATE TABLE IF NOT EXISTS enrichment_cost_log (
    id             BIGSERIAL PRIMARY KEY,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stage          TEXT NOT NULL,       -- stage1 | stage3 | stage4 | classifier | email_clean | commander | reachinbox
    service        TEXT NOT NULL,       -- outscraper | firecrawl | anthropic | truelist | reachinbox
    lead_id        TEXT,                -- TEXT (not BIGINT) because leads.id is UUID in this repo
    cost_usd       NUMERIC(10,5) NOT NULL,
    items_fetched  INT,
    tokens_used    INT,
    job_run_id     TEXT,
    metadata       JSONB
);

CREATE INDEX IF NOT EXISTS idx_ecl_occurred_at
    ON enrichment_cost_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_ecl_service_time
    ON enrichment_cost_log (service, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_ecl_stage_time
    ON enrichment_cost_log (stage, occurred_at DESC);


-- ── Table 2: budget_halt_log ─────────────────────────────────────────────────
-- Records every time cost_guard.check_budget() refused a call because a cap
-- would be exceeded. Read by the daily report anomaly section and by
-- GET /kjle/v1/budget/today.
CREATE TABLE IF NOT EXISTS budget_halt_log (
    id                       BIGSERIAL PRIMARY KEY,
    occurred_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    job_name                 TEXT NOT NULL,
    service                  TEXT NOT NULL,
    requested_operation      TEXT,
    current_daily_spend_usd  NUMERIC(10,5),
    cap_value_usd            NUMERIC(10,5),
    leads_affected           INT,
    metadata                 JSONB
);

CREATE INDEX IF NOT EXISTS idx_bhl_occurred_at
    ON budget_halt_log (occurred_at DESC);


-- ── Admin Settings: 7 new keys ───────────────────────────────────────────────
-- admin_settings schema: (key TEXT PK, value TEXT, updated_at TIMESTAMPTZ).
-- All values stored as TEXT; application casts on read.
INSERT INTO admin_settings (key, value, updated_at) VALUES
    ('daily_outscraper_cap_usd',    '30.00',                    NOW()),
    ('daily_anthropic_cap_usd',     '10.00',                    NOW()),
    ('daily_firecrawl_cap_usd',     '10.00',                    NOW()),
    ('daily_total_api_cap_usd',     '50.00',                    NOW()),
    ('monthly_total_api_cap_usd',  '500.00',                    NOW()),
    ('daily_cost_report_email',    'sales@mobilewebmds.com',    NOW()),
    ('daily_cost_report_enabled',  'true',                      NOW())
ON CONFLICT (key) DO UPDATE
    SET value = EXCLUDED.value,
        updated_at = NOW();


-- ── Verification queries (run manually to confirm) ───────────────────────────
-- SELECT * FROM enrichment_cost_log LIMIT 1;
-- SELECT * FROM budget_halt_log     LIMIT 1;
-- SELECT key, value FROM admin_settings
--   WHERE key IN (
--     'daily_outscraper_cap_usd','daily_anthropic_cap_usd','daily_firecrawl_cap_usd',
--     'daily_total_api_cap_usd','monthly_total_api_cap_usd',
--     'daily_cost_report_email','daily_cost_report_enabled'
--   )
--   ORDER BY key;
