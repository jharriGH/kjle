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
    # Stage 2 gracefully skips Yelp enrichment if empty.
    YELP_API_KEY: str = ""

    # Google API key — optional, increases PageSpeed rate limits
    # Not required; PageSpeed Insights works unauthenticated.
    GOOGLE_API_KEY: str = ""

    # Outscraper — $0.002/record, required for Stage 3 enrichment
    # Get key at https://app.outscraper.com/profile
    # Set via Render env var: OUTSCRAPER_API_KEY=your_key_here
    # Stage 3 enrichment will warn and return null fields if empty.
    OUTSCRAPER_API_KEY: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
