"""
KJLE API — Supabase Database Client
"""
from supabase import create_client, Client
from .config import settings
import logging

log = logging.getLogger("kjle")

_client: Client = None


async def init_db():
    global _client
    _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    log.info("Supabase client initialized")


def get_db() -> Client:
    if _client is None:
        raise RuntimeError("Database not initialized")
    return _client
