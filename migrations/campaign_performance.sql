-- ============================================================
-- KJLE — Campaign Performance Tracking (project-aware)
-- Creates: project_domains, campaign_performance
-- Idempotent. Safe to re-run.
-- ============================================================

-- ── project_domains ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project_domains (
    id          BIGSERIAL PRIMARY KEY,
    project     TEXT NOT NULL,
    domain      TEXT NOT NULL UNIQUE,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_project_domains_project ON project_domains(project);

-- Seed the 15 KJLE domains (idempotent — ON CONFLICT skips existing)
INSERT INTO project_domains (project, domain) VALUES
    ('kjle', 'reviewsbringcustomers.com'),
    ('kjle', 'freereputationaudits.com'),
    ('kjle', 'focusonreviews.com'),
    ('kjle', 'chatassistpro.com'),
    ('kjle', 'chatassist247.com'),
    ('kjle', 'chatassistproz.com'),
    ('kjle', 'bizreply247.com'),
    ('kjle', 'bizreplyainow.com'),
    ('kjle', 'smartwebsites24x7.com'),
    ('kjle', 'smarterwebsitesai.com'),
    ('kjle', 'smartwebsites247.com'),
    ('kjle', 'smarterwebsitesnow.com'),
    ('kjle', 'receptionistainow.com'),
    ('kjle', 'myaisubscriptionbox.com'),
    ('kjle', 'businesswebsitedesignai.com')
ON CONFLICT (domain) DO NOTHING;


-- ── campaign_performance ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS campaign_performance (
    id                       BIGSERIAL PRIMARY KEY,
    project                  TEXT NOT NULL DEFAULT 'kjle',
    reachinbox_server        TEXT NOT NULL DEFAULT 'server_1',
    reachinbox_campaign_id   TEXT,
    campaign_name            TEXT,
    niche                    TEXT,
    domain_used              TEXT,
    offer_type               TEXT,
    leads_count              INT DEFAULT 0,
    sent_count               INT DEFAULT 0,
    opened_count             INT DEFAULT 0,
    replied_count            INT DEFAULT 0,
    bounced_count            INT DEFAULT 0,
    unsubscribed_count       INT DEFAULT 0,
    demo_booked_count        INT DEFAULT 0,
    closed_count             INT DEFAULT 0,
    revenue_attributed_usd   NUMERIC(10, 2) DEFAULT 0,
    status                   TEXT,
    started_at               TIMESTAMPTZ,
    last_synced_at           TIMESTAMPTZ,
    metadata                 JSONB,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Identity uniqueness: a campaign_id within a (project, server) pair is unique.
-- Partial index allows NULL campaign_ids (e.g., manual registrations before RI returns ID).
CREATE UNIQUE INDEX IF NOT EXISTS ux_campaign_identity
    ON campaign_performance (project, reachinbox_server, reachinbox_campaign_id)
    WHERE reachinbox_campaign_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_campperf_project_status ON campaign_performance(project, status);
CREATE INDEX IF NOT EXISTS idx_campperf_project_niche  ON campaign_performance(project, niche);
CREATE INDEX IF NOT EXISTS idx_campperf_started_at     ON campaign_performance(started_at DESC);


-- ── updated_at auto-touch trigger ───────────────────────────
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_campperf_updated_at ON campaign_performance;
CREATE TRIGGER trg_campperf_updated_at
    BEFORE UPDATE ON campaign_performance
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ── verification queries (safe to run any time) ─────────────
-- SELECT count(*) FROM project_domains WHERE project = 'kjle';  -- expect: 15
-- SELECT count(*) FROM campaign_performance;                     -- initial: 0
