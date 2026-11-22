"""
Set required env vars before any api.* imports so pydantic-settings doesn't
raise ValidationError for missing SUPABASE_URL / SUPABASE_SERVICE_KEY.
"""
import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("API_SECRET_KEY", "test-secret")
os.environ.setdefault("OUTREACH_API_KEY", "test-outreach-key")
