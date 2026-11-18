"""
KJLE — Idempotent startup migrator.

Runs DDL via psycopg2 on app startup when DATABASE_URL is set.
Every statement uses IF NOT EXISTS / CREATE OR REPLACE — safe to re-run.
If DATABASE_URL is not set the migrator skips and logs a warning (manual
apply the SQL in migrations/ instead).

Called from api/main.py lifespan() BEFORE the scheduler starts.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ── SQL blocks ────────────────────────────────────────────────────────────────
# Split into individual statements so we can run them one by one and log
# failures without aborting the whole startup.

_MIGRATIONS: list[tuple[str, str]] = [
    # ── Outreach signals columns (Task 2) ──────────────────────────────────
    (
        "signals: form_status",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS form_status TEXT;",
    ),
    (
        "signals: site_dead",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS site_dead BOOLEAN;",
    ),
    (
        "signals: site_copyright_year",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS site_copyright_year INTEGER;",
    ),
    (
        "signals: builder",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS builder TEXT;",
    ),
    (
        "signals: outreach_socials",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS outreach_socials JSONB;",
    ),
    # ── Timezone column (already exists; guard with IF NOT EXISTS) ──────────
    (
        "leads: timezone",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS timezone TEXT;",
    ),
    # ── Partial indexes for outreach signal filters ─────────────────────────
    (
        "index: form_status partial",
        """
        CREATE INDEX IF NOT EXISTS idx_leads_form_status
            ON leads(form_status)
            WHERE form_status IS NOT NULL;
        """,
    ),
    (
        "index: site_dead partial",
        """
        CREATE INDEX IF NOT EXISTS idx_leads_site_dead
            ON leads(id)
            WHERE site_dead = true;
        """,
    ),
    # ── Indexes for nightly timeout fixes (Task 4) ──────────────────────────
    # provider_classify_nightly: selects email_status='valid' AND email_provider='unknown'
    (
        "index: provider_classify candidate",
        """
        CREATE INDEX IF NOT EXISTS idx_leads_provider_classify_candidate
            ON leads(id)
            WHERE email_status = 'valid'
              AND email_provider = 'unknown';
        """,
    ),
    # rdap_domain_check: selects is_active AND website IS NOT NULL AND
    #   domain_expires IS NULL AND domain_age_days IS NULL ORDER BY pain_score DESC
    (
        "index: rdap fill candidate",
        """
        CREATE INDEX IF NOT EXISTS idx_leads_rdap_fill_candidate
            ON leads(pain_score DESC NULLS LAST)
            WHERE is_active = true
              AND website IS NOT NULL
              AND domain_expires IS NULL
              AND domain_age_days IS NULL;
        """,
    ),
    # domain-expiry counts (mentioned in timeout analysis as 110s / 91s queries)
    (
        "index: domain_expired active",
        """
        CREATE INDEX IF NOT EXISTS idx_leads_domain_expired_active
            ON leads(id)
            WHERE is_active = true AND domain_expired = true;
        """,
    ),
    (
        "index: domain_expiring_soon active",
        """
        CREATE INDEX IF NOT EXISTS idx_leads_domain_expiring_soon_active
            ON leads(id)
            WHERE is_active = true AND domain_expiring_soon = true;
        """,
    ),
    (
        "index: domain_age_days not null",
        """
        CREATE INDEX IF NOT EXISTS idx_leads_domain_age_days_not_null
            ON leads(id)
            WHERE is_active = true AND domain_age_days IS NOT NULL;
        """,
    ),
    # ── Outreach idempotency keys table (Task 3) ────────────────────────────
    (
        "table: outreach_idempotency_keys",
        """
        CREATE TABLE IF NOT EXISTS outreach_idempotency_keys (
            key            TEXT        NOT NULL,
            endpoint       TEXT        NOT NULL,
            response_body  JSONB       NOT NULL DEFAULT '{}'::jsonb,
            response_status INTEGER    NOT NULL DEFAULT 200,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at     TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '7 days',
            PRIMARY KEY (key, endpoint)
        );
        """,
    ),
    (
        "index: idempotency expiry",
        """
        CREATE INDEX IF NOT EXISTS idx_oikey_expires
            ON outreach_idempotency_keys(expires_at);
        """,
    ),
    # ── Timezone backfill index (Task 1) ────────────────────────────────────
    (
        "index: tz backfill candidate",
        """
        CREATE INDEX IF NOT EXISTS idx_leads_tz_backfill
            ON leads(id)
            WHERE is_active = true
              AND state IS NOT NULL
              AND (timezone IS NULL OR timezone = '');
        """,
    ),
]


def run_startup_migrations() -> None:
    """Run all idempotent DDL migrations on startup."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        logger.warning(
            "startup_migrations: DATABASE_URL not set — skipping DDL. "
            "Apply migrations/ SQL files manually in Supabase SQL editor."
        )
        return

    try:
        import psycopg2
    except ImportError:
        logger.error("startup_migrations: psycopg2 not installed — skipping DDL")
        return

    try:
        conn = psycopg2.connect(database_url, connect_timeout=15)
        conn.autocommit = True
        # Disable statement_timeout for DDL — CREATE INDEX can be slow on large tables
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 0")
    except Exception as exc:
        logger.error(f"startup_migrations: DB connect failed — {exc}. Skipping DDL.")
        return

    success = 0
    skipped = 0
    failed = 0

    for label, sql in _MIGRATIONS:
        sql = sql.strip()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            logger.debug(f"startup_migrations: OK  — {label}")
            success += 1
        except Exception as exc:
            err = str(exc).splitlines()[0][:120]
            # 42701 = column already exists; 42P07 = relation already exists — both are fine
            if any(c in str(exc) for c in ("42701", "42P07", "already exists")):
                logger.debug(f"startup_migrations: skip (already exists) — {label}")
                skipped += 1
            else:
                logger.error(f"startup_migrations: FAILED — {label}: {err}")
                failed += 1

    conn.close()
    logger.info(
        f"startup_migrations: done — {success} applied, {skipped} skipped, "
        f"{failed} failed"
    )
