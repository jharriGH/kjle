"""
KJLE API — Supabase Database Client
"""
from supabase import create_client, Client
from supabase.client import ClientOptions
from .config import settings
import logging

log = logging.getLogger("kjle")

_client: Client = None


async def init_db():
    """Initialize the Supabase client with explicit timeouts to prevent
    indefinite hangs under concurrent load. Timeouts apply per-request to
    PostgREST; the global Client itself is long-lived and reused.
    """
    global _client
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


def get_db() -> Client:
    if _client is None:
        raise RuntimeError("Database not initialized")
    return _client
