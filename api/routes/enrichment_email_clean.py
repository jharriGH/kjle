"""
KJLE — Prompt 26B: Truelist.io Email Cleaning Integration
Route prefix handled in main.py: /kjle/v1/enrichment

Truelist API (confirmed working):
  Endpoint:  POST https://api.truelist.io/api/v1/verify_inline?email=address@domain.com
  Auth:      Authorization: Bearer TOKEN
  Response:  { "emails": [{ "address": "...", "email_state": "ok", "email_sub_state": "email_ok", ... }] }
  Note:      emails[] items are flat objects — NOT nested under an "email" key
  States:    ok | risky | unknown | bad
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from ..database import get_db
from ..config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

TRUELIST_URL     = "https://api.truelist.io/api/v1/verify_inline"
RATE_LIMIT_SLEEP = 0.5  # 2 requests/sec max


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_truelist_api_key(db) -> str:
    """Load Truelist API key from admin_settings, fall back to config env var."""
    try:
        res = db.table("admin_settings").select("value").eq("key", "truelist_api_key").execute()
        if res.data and res.data[0].get("value"):
            val = res.data[0]["value"].strip()
            if val:
                return val
    except Exception as e:
        logger.warning(f"Could not load truelist_api_key from admin_settings: {e}")

    if settings.TRUELIST_API_KEY:
        return settings.TRUELIST_API_KEY

    raise HTTPException(
        status_code=400,
        detail="Truelist API key not configured. Set via POST /admin/settings with key='truelist_api_key'.",
    )


def _parse_truelist_response(data: dict) -> tuple[Optional[bool], str, dict]:
    """
    Parse Truelist verify_inline response.

    Real response shape (confirmed):
      { "emails": [{ "address": "...", "email_state": "ok", "email_sub_state": "email_ok", ... }] }

    Note: items in emails[] are FLAT — not nested under an "email" key.

    email_state values:
      ok      → valid
      risky   → unknown (catch-all, accept-all)
      unknown → unknown
      bad     → invalid
    """
    try:
        email_obj = data["emails"][0]  # flat object — no nested "email" key
        state = email_obj.get("email_state", "unknown")
    except (KeyError, IndexError, TypeError):
        return None, "error", {}

    if state == "ok":
        return True, "valid", email_obj
    elif state == "bad":
        return False, "invalid", email_obj
    elif state in ("risky", "unknown"):
        return None, "unknown", email_obj
    else:
        return None, "unknown", email_obj


async def _verify_email(client: httpx.AsyncClient, api_key: str, email: str) -> dict:
    """
    Call Truelist verify_inline API.
    Method: POST with email as URL query param.
    """
    r = await client.post(
        TRUELIST_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        params={"email": email},
        timeout=15.0,
    )
    if r.status_code != 200:
        logger.error(f"[truelist] HTTP {r.status_code} for {email}: {r.text[:300]}")
    r.raise_for_status()
    return r.json()


async def _update_lead_email_status(
    db,
    lead_id: str,
    email_valid: Optional[bool],
    email_status: str,
) -> None:
    """Write email validation result back to the leads table."""
    db.table("leads").update(
        {
            "email_valid": email_valid,
            "email_status": email_status,
            "email_cleaned_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", lead_id).execute()


# ---------------------------------------------------------------------------
# POST /enrichment/email-clean  — Batch clean
# ---------------------------------------------------------------------------

@router.post("/email-clean")
async def batch_email_clean(
    limit: int = Query(default=100, ge=1, le=500),
    niche_slug: Optional[str] = Query(default=None),
    only_uncleaned: bool = Query(default=True),
    db=Depends(get_db),
):
    """
    Batch-validate lead emails via Truelist.io.
    Processes up to `limit` leads, rate-limited to 2 req/sec.
    """
    api_key = await _get_truelist_api_key(db)
    start_time = time.time()

    query = (
        db.table("leads")
        .select("id, email")
        .not_.is_("email", "null")
        .neq("email", "")
    )

    if only_uncleaned:
        query = query.is_("email_cleaned_at", "null")

    if niche_slug:
        query = query.eq("niche_slug", niche_slug)

    query = query.limit(limit)

    res   = query.execute()
    leads = res.data or []

    total         = len(leads)
    valid_count   = 0
    invalid_count = 0
    unknown_count = 0
    failed_count  = 0

    async with httpx.AsyncClient() as client:
        for lead in leads:
            lead_id = lead["id"]
            email   = lead["email"]

            try:
                data = await _verify_email(client, api_key, email)
                email_valid, email_status, _ = _parse_truelist_response(data)

                if email_status == "valid":
                    valid_count += 1
                elif email_status == "invalid":
                    invalid_count += 1
                else:
                    unknown_count += 1

                await _update_lead_email_status(db, lead_id, email_valid, email_status)
                logger.info(f"[email-clean] lead={lead_id} email={email} result={email_status}")

            except Exception as e:
                failed_count += 1
                logger.error(f"[email-clean] FAILED lead={lead_id} email={email} error={e}")
                try:
                    await _update_lead_email_status(db, lead_id, None, "error")
                except Exception as inner:
                    logger.error(f"[email-clean] Could not write error status for {lead_id}: {inner}")

            await asyncio.sleep(RATE_LIMIT_SLEEP)

    elapsed = round(time.time() - start_time, 2)

    return {
        "status": "complete",
        "total_processed": total,
        "valid": valid_count,
        "invalid": invalid_count,
        "unknown": unknown_count,
        "failed": failed_count,
        "elapsed_seconds": elapsed,
        "filter": {
            "limit": limit,
            "niche_slug": niche_slug,
            "only_uncleaned": only_uncleaned,
        },
    }


# ---------------------------------------------------------------------------
# GET /enrichment/email-clean/status  — Coverage stats
# ---------------------------------------------------------------------------

@router.get("/email-clean/status")
async def email_clean_status(db=Depends(get_db)):
    """Return email validation coverage stats across all leads."""

    total_res = (
        db.table("leads")
        .select("id", count="exact")
        .not_.is_("email", "null")
        .neq("email", "")
        .execute()
    )
    total_with_email = total_res.count or 0

    cleaned_res = (
        db.table("leads")
        .select("id", count="exact")
        .not_.is_("email", "null")
        .neq("email", "")
        .not_.is_("email_cleaned_at", "null")
        .execute()
    )
    total_cleaned = cleaned_res.count or 0

    valid_res = (
        db.table("leads")
        .select("id", count="exact")
        .eq("email_valid", True)
        .execute()
    )
    valid_count = valid_res.count or 0

    invalid_res = (
        db.table("leads")
        .select("id", count="exact")
        .eq("email_valid", False)
        .execute()
    )
    invalid_count = invalid_res.count or 0

    unknown_res = (
        db.table("leads")
        .select("id", count="exact")
        .not_.is_("email_cleaned_at", "null")
        .is_("email_valid", "null")
        .execute()
    )
    unknown_count = unknown_res.count or 0

    uncleaned    = total_with_email - total_cleaned
    coverage_pct = round((total_cleaned / total_with_email * 100), 1) if total_with_email > 0 else 0.0

    return {
        "total_leads_with_email": total_with_email,
        "total_cleaned": total_cleaned,
        "uncleaned": uncleaned,
        "valid": valid_count,
        "invalid": invalid_count,
        "unknown": unknown_count,
        "coverage_pct": coverage_pct,
    }


# ---------------------------------------------------------------------------
# POST /enrichment/email-clean/single/{lead_id}  — Clean one lead
# ---------------------------------------------------------------------------

@router.post("/email-clean/single/{lead_id}")
async def single_email_clean(lead_id: str, db=Depends(get_db)):
    """Validate a single lead's email via Truelist.io. Returns full detail."""
    api_key = await _get_truelist_api_key(db)

    res = db.table("leads").select("id, email").eq("id", lead_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Lead {lead_id} not found.")

    lead  = res.data[0]
    email = lead.get("email", "")

    if not email or not email.strip():
        raise HTTPException(status_code=400, detail=f"Lead {lead_id} has no email address.")

    try:
        async with httpx.AsyncClient() as client:
            data = await _verify_email(client, api_key, email)

        email_valid, email_status, email_obj = _parse_truelist_response(data)
        await _update_lead_email_status(db, lead_id, email_valid, email_status)

        logger.info(f"[email-clean/single] lead={lead_id} email={email} result={email_status}")

        return {
            "lead_id":         lead_id,
            "email":           email,
            "result":          email_status,
            "email_valid":     email_valid,
            "email_status":    email_status,
            "email_state":     email_obj.get("email_state"),
            "email_sub_state": email_obj.get("email_sub_state"),
            "mx_record":       email_obj.get("mx_record"),
            "domain":          email_obj.get("domain"),
            "did_you_mean":    email_obj.get("did_you_mean"),
            "cleaned_at":      datetime.now(timezone.utc).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[email-clean/single] FAILED lead={lead_id} error={e}")
        try:
            await _update_lead_email_status(db, lead_id, None, "error")
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"Truelist API error: {str(e)}")
