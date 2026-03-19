"""
KJLE — Prompt 30: Integration Hub Routes
File: api/routes/integration_hub.py
"""

import os
import logging
import httpx
from fastapi import APIRouter, HTTPException, Header
from supabase import create_client, Client

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")

router = APIRouter()


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


def get_admin_setting(supabase: Client, key: str) -> str | None:
    """Pull a value from the admin_settings table by key."""
    try:
        result = supabase.table("admin_settings").select("value").eq("key", key).single().execute()
        return result.data.get("value") if result.data else None
    except Exception:
        return None


def resolve_key(supabase: Client, env_var: str, admin_key: str) -> str | None:
    """Prefer environment variable, fall back to admin_settings table."""
    return os.environ.get(env_var) or get_admin_setting(supabase, admin_key)


# ─────────────────────────────────────────────────
# GET /kjle/v1/integrations — List all integrations + status
# ─────────────────────────────────────────────────

@router.get("/integrations")
async def list_integrations(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    integrations = [
        {
            "id": "reachinbox",
            "name": "ReachInbox",
            "description": "Email outreach export",
            "configured": bool(resolve_key(supabase, "REACHINBOX_API_KEY", "reachinbox_api_key")),
            "category": "outreach",
        },
        {
            "id": "truelist",
            "name": "Truelist.io",
            "description": "Email validation and cleaning",
            "configured": bool(resolve_key(supabase, "TRUELIST_API_KEY", "truelist_api_key")),
            "category": "enrichment",
        },
        {
            "id": "outscraper",
            "name": "Outscraper",
            "description": "Stage 3 enrichment — Google Maps + reviews",
            "configured": bool(resolve_key(supabase, "OUTSCRAPER_API_KEY", "outscraper_api_key")),
            "category": "enrichment",
        },
        {
            "id": "firecrawl",
            "name": "Firecrawl",
            "description": "Stage 4 enrichment — website crawl",
            "configured": bool(resolve_key(supabase, "FIRECRAWL_API_KEY", "firecrawl_api_key")),
            "category": "enrichment",
        },
        {
            "id": "yelp",
            "name": "Yelp Fusion API",
            "description": "Stage 2 enrichment — Yelp business data",
            "configured": bool(resolve_key(supabase, "YELP_API_KEY", "yelp_api_key")),
            "category": "enrichment",
        },
        {
            "id": "supabase",
            "name": "Supabase",
            "description": "Primary database — project dhzpwobfihrprlcxqjbq",
            "configured": bool(SUPABASE_URL and SUPABASE_SERVICE_KEY),
            "category": "infrastructure",
        },
    ]

    configured_count = sum(1 for i in integrations if i["configured"])

    return {
        "total": len(integrations),
        "configured": configured_count,
        "not_configured": len(integrations) - configured_count,
        "integrations": integrations,
    }


# ─────────────────────────────────────────────────
# POST /kjle/v1/integrations/reachinbox/test
# ─────────────────────────────────────────────────

@router.post("/integrations/reachinbox/test")
async def test_reachinbox(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    api_key = resolve_key(supabase, "REACHINBOX_API_KEY", "reachinbox_api_key")
    if not api_key:
        return {"integration": "reachinbox", "status": "not_configured", "message": "No API key found in environment or admin_settings"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.reachinbox.ai/api/v1/email-account/list",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            return {"integration": "reachinbox", "status": "ok", "http_status": resp.status_code}
        else:
            return {"integration": "reachinbox", "status": "error", "http_status": resp.status_code, "message": resp.text[:200]}
    except Exception as e:
        return {"integration": "reachinbox", "status": "error", "message": str(e)}


# ─────────────────────────────────────────────────
# POST /kjle/v1/integrations/truelist/test
# ─────────────────────────────────────────────────

@router.post("/integrations/truelist/test")
async def test_truelist(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    api_key = resolve_key(supabase, "TRUELIST_API_KEY", "truelist_api_key")
    if not api_key:
        return {"integration": "truelist", "status": "not_configured", "message": "No API key found in environment or admin_settings"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://truelist.io/api/v1/account",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "integration": "truelist",
                "status": "ok",
                "http_status": resp.status_code,
                "account": data,
            }
        else:
            return {"integration": "truelist", "status": "error", "http_status": resp.status_code, "message": resp.text[:200]}
    except Exception as e:
        return {"integration": "truelist", "status": "error", "message": str(e)}


# ─────────────────────────────────────────────────
# POST /kjle/v1/integrations/outscraper/test
# ─────────────────────────────────────────────────

@router.post("/integrations/outscraper/test")
async def test_outscraper(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    api_key = resolve_key(supabase, "OUTSCRAPER_API_KEY", "outscraper_api_key")
    if not api_key:
        return {"integration": "outscraper", "status": "not_configured", "message": "No API key found in environment or admin_settings"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.app.outscraper.com/account",
                headers={"X-API-KEY": api_key},
            )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "integration": "outscraper",
                "status": "ok",
                "http_status": resp.status_code,
                "account": data,
            }
        else:
            return {"integration": "outscraper", "status": "error", "http_status": resp.status_code, "message": resp.text[:200]}
    except Exception as e:
        return {"integration": "outscraper", "status": "error", "message": str(e)}


# ─────────────────────────────────────────────────
# POST /kjle/v1/integrations/firecrawl/test
# ─────────────────────────────────────────────────

@router.post("/integrations/firecrawl/test")
async def test_firecrawl(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    api_key = resolve_key(supabase, "FIRECRAWL_API_KEY", "firecrawl_api_key")
    if not api_key:
        return {"integration": "firecrawl", "status": "not_configured", "message": "No API key found in environment or admin_settings"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.firecrawl.dev/v1/account",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "integration": "firecrawl",
                "status": "ok",
                "http_status": resp.status_code,
                "account": data,
            }
        else:
            return {"integration": "firecrawl", "status": "error", "http_status": resp.status_code, "message": resp.text[:200]}
    except Exception as e:
        return {"integration": "firecrawl", "status": "error", "message": str(e)}


# ─────────────────────────────────────────────────
# POST /kjle/v1/integrations/yelp/test
# ─────────────────────────────────────────────────

@router.post("/integrations/yelp/test")
async def test_yelp(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    api_key = resolve_key(supabase, "YELP_API_KEY", "yelp_api_key")
    if not api_key:
        return {"integration": "yelp", "status": "not_configured", "message": "No API key found in environment or admin_settings"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.yelp.com/v3/businesses/search?location=San+Francisco&term=coffee&limit=1",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            return {"integration": "yelp", "status": "ok", "http_status": resp.status_code}
        else:
            return {"integration": "yelp", "status": "error", "http_status": resp.status_code, "message": resp.text[:200]}
    except Exception as e:
        return {"integration": "yelp", "status": "error", "message": str(e)}


# ─────────────────────────────────────────────────
# GET /kjle/v1/integrations/webhooks/summary
# ─────────────────────────────────────────────────

@router.get("/integrations/webhooks/summary")
async def webhooks_summary(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        total_res = supabase.table("webhooks").select("id", count="exact").execute()
        active_res = supabase.table("webhooks").select("id", count="exact").eq("is_active", True).execute()

        # Recent deliveries from export_log as proxy for webhook activity
        recent_res = supabase.table("export_log").select(
            "id, created_at, export_type, lead_count"
        ).order("created_at", desc=True).limit(5).execute()

        return {
            "total_webhooks": total_res.count or 0,
            "active_webhooks": active_res.count or 0,
            "recent_exports": recent_res.data or [],
        }
    except Exception as e:
        logger.error(f"webhooks_summary error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# GET /kjle/v1/integrations/push/summary
# ─────────────────────────────────────────────────

@router.get("/integrations/push/summary")
async def push_summary(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        # DemoEnginez eligible leads
        de_res = supabase.table("leads").select("id", count="exact").eq("fit_demoenginez", True).eq("is_active", True).execute()

        # VoiceDrop eligible leads
        vd_res = supabase.table("leads").select("id", count="exact").eq("fit_voicedrop", True).eq("is_active", True).execute()

        # Hot leads eligible for both
        hot_de_res = supabase.table("leads").select("id", count="exact").eq("fit_demoenginez", True).eq("segment_label", "hot").eq("is_active", True).execute()
        hot_vd_res = supabase.table("leads").select("id", count="exact").eq("fit_voicedrop", True).eq("segment_label", "hot").eq("is_active", True).execute()

        return {
            "demoenginez": {
                "eligible_leads": de_res.count or 0,
                "hot_leads": hot_de_res.count or 0,
            },
            "voicedrop": {
                "eligible_leads": vd_res.count or 0,
                "hot_leads": hot_vd_res.count or 0,
            },
        }
    except Exception as e:
        logger.error(f"push_summary error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
