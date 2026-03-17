"""
KJLE API — Configuration
Reads from environment variables (set in Render dashboard).
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Supabase ──────────────────────────────────────────────────────────────
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_KEY: str = ""

    # ── App ───────────────────────────────────────────────────────────────────
    APP_ENV: str = "production"
    SECRET_KEY: str = "kjle-secret-change-in-prod"
    ADMIN_API_KEY: str = ""

    # ── External APIs ─────────────────────────────────────────────────────────
    OUTSCRAPER_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""

    # ── Outreach ──────────────────────────────────────────────────────────────
    REACHINBOX_API_KEY: str = ""
    INSTANTLY_API_KEY: str = ""

    # ── Truelist.io ───────────────────────────────────────────────────────────
    # Optional fallback — preferred source is admin_settings table key 'truelist_api_key'
    TRUELIST_API_KEY: str = ""

    # ── Webhooks / Integrations ───────────────────────────────────────────────
    WEBHOOK_SECRET: str = ""
    SLACK_WEBHOOK_URL: str = ""

    # ── Cost tracking ─────────────────────────────────────────────────────────
    COST_TRACKING_ENABLED: bool = True

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
