"""
KJLE API — Application Configuration
Reads from environment variables with sensible defaults.
Set secrets via Render Environment Variables or a local .env file.
"""

from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Supabase (KJLE) ───────────────────────────────────────────────────────
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str

    # ── App Security ──────────────────────────────────────────────────────────
    API_SECRET_KEY: str = "change-me-in-production"

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: List[str] = ["*"]

    # ── External APIs ─────────────────────────────────────────────────────────
    YELP_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    OUTSCRAPER_API_KEY: str = ""
    FIRECRAWL_API_KEY: str = ""
    REACHINBOX_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""     # Commander still reads os.environ directly; typed here for visibility
    RESEND_API_KEY: str = ""        # Daily cost report email — kjle@kjreportz.com
    SEARCHBUG_API_KEY: str = ""     # DNC provider PASS field; CO_CODE sourced from admin_settings.dnc_searchbug_co_code
    REACHINBOX_WEBHOOK_SECRET: str = ""  # Shared secret for /dnc/webhooks/reachinbox?secret=... query-param auth

    # ── Truelist.io — email cleaning ──────────────────────────────────────────
    # Optional fallback — preferred source is admin_settings table key 'truelist_api_key'
    TRUELIST_API_KEY: str = ""
    # Shared secret for /webhooks/truelist/batch-complete?secret=... query-param auth.
    # Until Truelist's dashboard webhook is configured, the 30-min poller handles
    # ingestion; this env var only matters when the webhook is wired.
    TRUELIST_WEBHOOK_SECRET: str = ""

    # ── DemoEnginez Push ──────────────────────────────────────────────────────
    DEMOENGINEZ_SUPABASE_URL: str = ""
    DEMOENGINEZ_SUPABASE_KEY: str = ""

    # ── VoiceDrop OS Push ─────────────────────────────────────────────────────
    VOICEDROP_SUPABASE_URL: str = ""
    VOICEDROP_SUPABASE_KEY: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
