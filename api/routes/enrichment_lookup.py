"""
Freeform Business Enrichment Lookup
KJLE - King James Lead Empire
Routes: /kjle/v1/enrichment/lookup

Caller-supplied (name, [company], [address]) -> cache check -> Outscraper Maps
search-v3 fallback. Designed for cross-product enrichment (e.g. KJPDE parsers
calling out to fill phone/website without owning the data flow themselves).

Read-only against admin_settings.outscraper_api_key (same source Stage 3 uses).
Writes to kjpde_enrichment_cache only. Never raises -- always returns the
{email, phone, website, source} shape.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

OUTSCRAPER_URL = "https://api.app.outscraper.com/maps/search-v3"
HTTP_TIMEOUT = 30.0
CACHE_TABLE = "kjpde_enrichment_cache"
CACHE_TTL_DAYS = 30
ZIP_REGEX = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


class LookupRequest(BaseModel):
    name: str
    company: Optional[str] = None
    address: Optional[str] = None


def _empty(source: str = "error") -> dict:
    return {"email": None, "phone": None, "website": None, "source": source}


def _extract_zip(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    m = ZIP_REGEX.search(address)
    return m.group(1) if m else None


def _cache_get(db, name: str, zip_code: Optional[str]) -> Optional[dict]:
    try:
        q = db.table(CACHE_TABLE).select("*").eq("borrower_name", name).limit(1)
        if zip_code is not None:
            q = q.eq("zip", zip_code)
        else:
            q = q.is_("zip", "null")
        res = q.execute()
        if not res.data:
            return None
        row = res.data[0]
        cached_at = row.get("cached_at")
        if not cached_at:
            return None
        try:
            ts = datetime.fromisoformat(str(cached_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - ts > timedelta(days=CACHE_TTL_DAYS):
            return None
        return row
    except Exception as e:
        logger.warning(f"enrichment_lookup cache_get error: {type(e).__name__}: {e}")
        return None


def _cache_put(db, name: str, zip_code: Optional[str], phone: Optional[str],
               website: Optional[str], full_address: Optional[str]) -> None:
    try:
        db.table(CACHE_TABLE).insert({
            "borrower_name": name,
            "zip": zip_code,
            "phone": phone,
            "website": website,
            "full_address": full_address,
            "source": "outscraper",
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"enrichment_lookup cache_put error: {type(e).__name__}: {e}")


def _get_outscraper_key(db) -> str:
    try:
        res = (
            db.table("admin_settings")
              .select("value")
              .eq("key", "outscraper_api_key")
              .execute()
        )
        return res.data[0]["value"] if res.data else ""
    except Exception:
        return ""


async def _outscraper_lookup(api_key: str, query: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(
                OUTSCRAPER_URL,
                headers={"X-API-KEY": api_key},
                params={"query": query, "limit": 1, "language": "en", "async": "false"},
            )
            r.raise_for_status()
            data = r.json() or {}
        outer = data.get("data") or []
        if not outer or not outer[0]:
            return {}
        place = outer[0][0] or {}
        return {
            "phone": place.get("phone") or place.get("phone_international"),
            "website": place.get("site") or place.get("website"),
            "full_address": place.get("full_address"),
        }
    except httpx.TimeoutException:
        logger.warning(f"enrichment_lookup Outscraper timeout for '{query}'")
    except httpx.HTTPStatusError as e:
        logger.warning(f"enrichment_lookup Outscraper HTTP {e.response.status_code} for '{query}'")
    except Exception as e:
        logger.warning(f"enrichment_lookup Outscraper error: {type(e).__name__}: {e}")
    return {}


@router.post("/lookup")
async def lookup(req: LookupRequest):
    try:
        name = (req.name or "").strip()
        if not name:
            return _empty("error")

        address = (req.address or "").strip() or None
        zip_code = _extract_zip(address)
        db = get_db()

        cached = _cache_get(db, name, zip_code)
        if cached:
            return {
                "email": None,
                "phone": cached.get("phone"),
                "website": cached.get("website"),
                "source": "cache",
            }

        api_key = _get_outscraper_key(db)
        if not api_key:
            logger.warning("enrichment_lookup: outscraper_api_key not configured")
            return _empty("error")

        query = " ".join(p for p in (name, address) if p).strip()
        if not query:
            return _empty("error")

        result = await _outscraper_lookup(api_key, query)
        if not result:
            return _empty("error")

        _cache_put(
            db, name, zip_code,
            result.get("phone"),
            result.get("website"),
            result.get("full_address"),
        )

        return {
            "email": None,
            "phone": result.get("phone"),
            "website": result.get("website"),
            "source": "outscraper",
        }

    except Exception as e:
        logger.warning(f"enrichment_lookup unexpected error: {type(e).__name__}: {e}")
        return _empty("error")
