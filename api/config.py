"""
KJLE API — Configuration
"""
from pydantic_settings import BaseSettings
from typing import List

class Settings(BaseSettings):
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    CORS_ORIGINS: List[str] = ["*"]
    API_SECRET_KEY: str = "change-me-in-production"
    YELP_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
