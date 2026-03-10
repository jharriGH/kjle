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
    # Yelp Fusion — free tier, https://fusion.yelp.com
    YELP_API_KEY: str = ""

    # Google API key — optional, increases PageSpeed rate limits
    GOOGLE_API_KEY: str = ""

    # Outscraper — $0.002/record, Stage 3 enrichment
    OUTSCRAPER_API_KEY: str = ""

    # Firecrawl — $0.005/record, Stage 4 deep enrichment
    FIRECRAWL_API_KEY: str = ""

    # ReachInbox — email outreach export
    REACHINBOX_API_KEY: str = ""

    # ── DemoEnginez Push ──────────────────────────────────────────────────────
    # Shared Supabase project — leave empty to use shared KJLE client.
    # Set only if DemoEnginez moves to a dedicated Supabase project.
    DEMOENGINEZ_SUPABASE_URL: str = ""
    DEMOENGINEZ_SUPABASE_KEY: str = ""

    # ── VoiceDrop OS Push ─────────────────────────────────────────────────────
    # Shared Supabase project (voicedrop schema) — leave empty to use shared
    # KJLE client. Set only if VoiceDrop OS moves to a dedicated Supabase project.
    VOICEDROP_SUPABASE_URL: str = ""
    VOICEDROP_SUPABASE_KEY: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
