"""
KJLE API — Supabase Database Client
"""
from supabase import create_client, Client
from supabase.client import ClientOptions
from .config import settings
import logging

log = logging.getLogger("kjle")

_client: Client = None
_batch_client: Client = None


async def init_db():
    """Initialize the Supabase client with explicit timeouts to prevent
    indefinite hangs under concurrent load. Timeouts apply per-request to
    PostgREST; the global Client itself is long-lived and reused.
    """
    global _client, _batch_client
    options = ClientOptions(
        # PostgREST request timeout (seconds). Was unset (could hang forever).
        # 30s is generous for our heaviest reads; anything longer suggests a
        # query-shape problem we should fix, not paper over.
        postgrest_client_timeout=30,
        # Storage/Auth timeouts — same rationale; we don't use these heavily
        # but the defaults are also unset.
        storage_client_timeout=30,
    )
    _client = create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_KEY,
        options=options,
    )
    log.info("Supabase client initialized with 30s PostgREST timeout")

    # 2026-11-19 (KJLE_SCHEDULER_FIX): a handful of nightly batch jobs scan
    # leads (1.26M rows) and were hitting Postgres 57014 (statement_timeout).
    # service_role has no role-level statement_timeout override (confirmed via
    # pg_roles.rolconfig) and was raised to 600s at the DB layer (see empire
    # migration note / card). This second client exists ONLY so the 30s
    # client-side PostgREST timeout above doesn't itself abort those same
    # long-running requests before the DB-side 600s ceiling ever applies.
    # Separate client, NOT a global timeout bump — every other call path
    # keeps the stricter 30s client via get_db().
    batch_options = ClientOptions(
        postgrest_client_timeout=650,
        storage_client_timeout=650,
    )
    _batch_client = create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_KEY,
        options=batch_options,
    )
    log.info("Supabase batch client initialized with 650s PostgREST timeout")


def get_db() -> Client:
    if _client is None:
        raise RuntimeError("Database not initialized")
    return _client


def get_db_batch() -> Client:
    """Long-timeout (650s) client for nightly batch jobs whose statements can
    legitimately run past 30s on the 1.26M-row leads table (provider_classify_
    nightly, classify_segments, select_revalidation_leads). See init_db()."""
    if _batch_client is None:
        raise RuntimeError("Database not initialized")
    return _batch_client
