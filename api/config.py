"""
KJLE API — Application Configuration
Reads from environment variables with sensible defaults.
Set secrets via Render Environment Variables or a local .env file.
"""

from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Supabase ──────────────────────────────────────────────────────────────
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str

    # ── App Security ──────────────────────────────────────────────────────────
    API_SECRET_KEY: str = "change-me-in-production"

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: List[str] = ["*"]

    # ── External APIs ─────────────────────────────────────────────────────────
    # Yelp Fusion — free tier, get key at https://fusion.yelp.com
    YELP_API_KEY: str = ""

    # Google API key — optional, increases PageSpeed rate limits
    GOOGLE_API_KEY: str = ""

    # Outscraper — $0.002/record, Stage 3 enrichment
    # https://app.outscraper.com/profile
    OUTSCRAPER_API_KEY: str = ""

    # Firecrawl — $0.005/record, Stage 4 deep enrichment
    # https://firecrawl.dev
    FIRECRAWL_API_KEY: str = ""

    # ReachInbox — email outreach export destination
    # https://app.reachinbox.ai → Settings → API Keys
    # Set via Render env var: REACHINBOX_API_KEY=your_key_here
    REACHINBOX_API_KEY: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
