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
    RESEND_API_KEY: str = ""        # Daily cost report email — reports@freereputationaudits.com

    # ── Truelist.io — email cleaning ──────────────────────────────────────────
    # Optional fallback — preferred source is admin_settings table key 'truelist_api_key'
    TRUELIST_API_KEY: str = ""

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
