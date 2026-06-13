"""
ReachInbox Campaign Builder
KJLE - King James Lead Empire
Routes: /kjle/v1/reachinbox/*

Complete campaign creation from KJLE Lead Finder:
1. GET  /reachinbox/campaigns        — list all campaigns
2. GET  /reachinbox/accounts         — list all email accounts
3. POST /reachinbox/campaigns/create — create full campaign end-to-end
4. POST /reachinbox/campaigns/launch — launch existing campaign
5. POST /reachinbox/campaigns/pause  — pause campaign
6. GET  /reachinbox/campaigns/{id}/status — campaign status + stats
7. POST /reachinbox/leads/add        — add leads to existing campaign
"""

import logging
import httpx
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db
from ..config import settings

# Added for DELETE /reachinbox/campaigns/{id} (orphan cleanup endpoint)
import os
from fastapi import Depends, Header

logger = logging.getLogger(__name__)
router = APIRouter()

# Auth for destructive endpoints (delete_campaign). Other RI routes remain unauth'd.
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

RI_BASE = "https://api.reachinbox.ai/api/v1"


def _ri_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.REACHINBOX_API_KEY}",
        "Content-Type":  "application/json",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class EmailVariant(BaseModel):
    subject: str
    body:    str

class SequenceStep(BaseModel):
    variants:    List[EmailVariant]
    type:        str   = "initial"   # initial | follow-up
    delay:       int   = 0           # days before sending
    ccEnabled:   bool  = False
    bccEnabled:  bool  = False
    ccVariables:  List = []
    bccVariables: List = []

class ScheduleTiming(BaseModel):
    from_time: str = "09:00"   # field alias handled below
    to_time:   str = "17:00"

class ScheduleDays(BaseModel):
    monday:    bool = True
    tuesday:   bool = True
    wednesday: bool = True
    thursday:  bool = True
    friday:    bool = True
    saturday:  bool = False
    sunday:    bool = False

class CampaignLeadFilter(BaseModel):
    niche_slug:    Optional[str] = None
    state:         Optional[str] = None
    min_pain:      int           = 0
    segment_label: Optional[str] = None
    limit:         int           = 100

class CreateCampaignRequest(BaseModel):
    # Campaign basics
    name:           str
    daily_limit:    int  = 200
    max_new_leads:  int  = 200
    stop_on_reply:  bool = True
    track_opens:    bool = True
    track_links:    bool = True
    delivery_mode:  int  = 0        # 0=HTML, 1=Text only, 2=Hybrid

    # Email sequences
    sequences: List[SequenceStep]

    # Schedule
    timezone:   str = "America/Los_Angeles"
    start_hour: str = "09:00"
    end_hour:   str = "17:00"
    send_monday:    bool = True
    send_tuesday:   bool = True
    send_wednesday: bool = True
    send_thursday:  bool = True
    send_friday:    bool = True
    send_saturday:  bool = False
    send_sunday:    bool = False

    # Email accounts to use (list of account IDs)
    account_ids: List[int] = []

    # Lead filters — pulls from KJLE automatically
    lead_filter: CampaignLeadFilter

    # Auto-launch after creation
    auto_launch: bool = False

    # Auto-register metadata (consumed by campaign_performance tracking)
    domain_used: Optional[str] = None
    offer_type: Optional[str]  = None

class AddLeadsRequest(BaseModel):
    campaign_id:   int
    lead_filter:   CampaignLeadFilter


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def ri_get(path: str, params: dict = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{RI_BASE}{path}", headers=_ri_headers(), params=params or {})
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"ReachInbox error: {r.text[:200]}")
        return r.json()

async def ri_post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{RI_BASE}{path}", headers=_ri_headers(), json=body)
        if r.status_code not in [200, 201]:
            raise HTTPException(status_code=r.status_code, detail=f"ReachInbox error: {r.text[:200]}")
        return r.json()

async def ri_put(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.put(f"{RI_BASE}{path}", headers=_ri_headers(), json=body)
        if r.status_code not in [200, 201]:
            raise HTTPException(status_code=r.status_code, detail=f"ReachInbox error: {r.text[:200]}")
        return r.json()

async def ri_delete(path: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.delete(f"{RI_BASE}{path}", headers=_ri_headers())
        if r.status_code not in [200, 201, 202, 204]:
            raise HTTPException(status_code=r.status_code, detail=f"ReachInbox error: {r.text[:200]}")
        return r.json() if r.text else {}

def fetch_kjle_leads(filters: CampaignLeadFilter) -> list:
    """Pull leads from KJLE database based on filters."""
    db = get_db()
    limit = min(filters.limit, 2000)

    query = (
        db.table("leads")
        .select("id, business_name, email, phone, city, state, niche_slug, pain_score")
        .eq("is_active", True)
        .eq("email_valid", True)
        .not_.is_("email", "null")
        .gte("pain_score", filters.min_pain)
    )

    if filters.niche_slug:
        query = query.eq("niche_slug", filters.niche_slug)
    if filters.state:
        query = query.eq("state", filters.state.upper())
    if filters.segment_label:
        query = query.eq("segment_label", filters.segment_label)

    # Deterministic ordering so LIMIT cuts at the same boundary every call,
    # and the partial-index planner (leads_campaign_eligible_idx) gets a
    # stable sort key — prevents statement_timeout on the 596K-row table.
    query = query.order("pain_score", desc=True).order("id").limit(limit)
    result = query.execute()
    return result.data or []

def map_lead_to_ri(lead: dict) -> dict:
    """Map KJLE lead to ReachInbox lead format.

    RI docs (docs.reachinbox.ai/lead) require firstName/lastName as
    top-level fields. Custom attributes go under "attributes".
    """
    name_parts = (lead.get("business_name") or "").split()
    first_name = name_parts[0] if name_parts else "Business"
    last_name  = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
    company    = lead.get("business_name") or ""

    return {
        "email":     lead.get("email") or "",
        "firstName": first_name,
        "lastName":  last_name,
        "attributes": {
            "companyName": company,
            "phone":       lead.get("phone") or "",
            "city":        lead.get("city") or "",
            "state":       lead.get("state") or "",
            "niche":       lead.get("niche_slug") or "",
            "painScore":   str(lead.get("pain_score") or 0),
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /reachinbox/campaigns
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/reachinbox/campaigns")
async def list_campaigns(
    limit:  int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List all ReachInbox campaigns with stats."""
    data = await ri_get("/campaigns/all", {"limit": limit, "offset": offset})
    campaigns = data.get("data", {}).get("rows", [])

    return {
        "status":      "success",
        "total":       data.get("data", {}).get("totalCount", 0),
        "campaigns":   [
            {
                "id":           c["id"],
                "name":         c["name"],
                "status":       c["status"],
                "isActive":     c["isActive"],
                "hasStarted":   c["hasStarted"],
                "completed":    c["completed"],
                "dailyLimit":   c["dailyLimit"],
                "leadCount":    c["leadAddedCount"],
                "emailsSent":   c["totalEmailSent"],
                "uniqueOpens":  c["totalUniqueEmailOpened"],
                "replies":      c["totalEmailReplied"],
                "progress":     c["progressPercentage"],
                "createdAt":    c["createdAt"],
            }
            for c in campaigns
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /reachinbox/campaigns/{campaign_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/reachinbox/campaigns/{campaign_id}", dependencies=[Depends(verify_api_key)])
async def delete_campaign(campaign_id: int):
    """
    Delete a RI campaign by ID. Thin wrapper around ri_delete.

    Used for orphan cleanup of test/draft campaigns. Returns RI's response.
    Caller is responsible for verifying the campaign is safe to delete
    (Draft + hasStarted=False) before calling.
    """
    logger.info(f"[reachinbox.delete_campaign] DELETE /campaigns/{campaign_id}")
    try:
        result = await ri_delete(f"/campaigns/{campaign_id}")
        logger.info(f"[reachinbox.delete_campaign] success: campaign_id={campaign_id} result={result!r}")
        return {
            "status":      "success",
            "campaign_id": campaign_id,
            "ri_response": result,
        }
    except HTTPException as e:
        logger.error(f"[reachinbox.delete_campaign] FAILED: campaign_id={campaign_id} status={e.status_code} detail={str(e.detail)[:200]}")
        raise
    except Exception as e:
        logger.error(f"[reachinbox.delete_campaign] UNEXPECTED ERROR: campaign_id={campaign_id} error={e!r}")
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)[:200]}")


# ─────────────────────────────────────────────────────────────────────────────
# GET /reachinbox/accounts
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/reachinbox/accounts")
async def list_accounts(
    limit:  int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List all connected ReachInbox email accounts."""
    data = await ri_get("/account/all", {"limit": limit, "offset": offset})
    accounts = data.get("data", {}).get("emailsConnected", [])

    return {
        "status":  "success",
        "total":   data.get("data", {}).get("totalCount", 0),
        "accounts": [
            {
                "id":           a["id"],
                "email":        a["email"],
                "firstName":    a.get("firstName") or "",
                "isActive":     a["isActive"],
                "dailyLimit":   a["limits"],
                "sentToday":    a["mailsSentToday"],
                "warmupScore":  a.get("warmupHealthScore") or 0,
                "warmupEnabled": a.get("warmupEnabled") or False,
                "domain":       a["email"].split("@")[-1] if "@" in a["email"] else "",
            }
            for a in accounts
            if a.get("isActive")
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /reachinbox/campaigns/create
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/reachinbox/campaigns/create")
async def create_full_campaign(body: CreateCampaignRequest):
    """
    Create a complete ReachInbox campaign in one call.

    Pre-flight (fail fast, no RI write):
      P1. Fetch KJLE leads → 400 if zero match the filter (prevents orphan campaign).
      P2. Resolve account_ids (int) → emails via /account/all → 400 if any unresolvable
          (RI /campaigns/set-accounts expects an email-address array, NOT account IDs).

    RI write sequence (rollback-protected):
      1. Create campaign
      2. Add email sequences
      3. Set schedule
      4. Update campaign options
      5. Set email accounts (emails, translated from int IDs)
      6. Add KJLE leads
      7. Optionally launch
      8. Register in campaign_performance (non-fatal, outside rollback)
    """

    steps_completed = []
    campaign_id     = None

    # ── Pre-flight P1: Fetch leads BEFORE any RI call ─────────────────────────
    # Prior shape created the campaign first, then queried leads — when the
    # 596K-row leads table hit Postgres statement_timeout 57014, the RI
    # campaign was already created and the request returned an error,
    # leaving an orphan draft. Fetching first lets us 400 before any write.
    kjle_leads = fetch_kjle_leads(body.lead_filter)
    if not kjle_leads:
        raise HTTPException(status_code=400, detail="no_eligible_leads_match_filter")

    # ── Pre-flight P2: Translate account_ids → emails (Bug 1) ─────────────────
    # RI /campaigns/set-accounts "emails" field expects email-address strings,
    # not the integer account IDs the KJLE sender contract uses. Resolve them
    # via the same /account/all helper that GET /reachinbox/accounts uses.
    account_emails: List[str] = []
    if body.account_ids:
        accounts_resp = await ri_get("/account/all", {"limit": 500, "offset": 0})
        connected = accounts_resp.get("data", {}).get("emailsConnected", []) or []
        id_email_map = {a["id"]: a["email"] for a in connected if a.get("id") and a.get("email")}

        missing_ids = [aid for aid in body.account_ids if aid not in id_email_map]
        if missing_ids:
            raise HTTPException(
                status_code=400,
                detail=f"unresolvable_account_ids: {missing_ids}",
            )

        account_emails = [id_email_map[aid] for aid in body.account_ids]
        logger.info(
            f"[reachinbox.create] resolved {len(account_emails)}/{len(body.account_ids)} "
            f"account_ids to emails"
        )

    leads_added = 0
    leads_skipped = 0

    try:
        # ── Step 1: Create campaign ───────────────────────────────────────────
        create_resp = await ri_post("/campaigns/create", {"name": body.name})
        campaign_id = create_resp["data"]["id"]
        steps_completed.append(f"✅ Campaign created (ID: {campaign_id})")

        # ── Step 2: Add sequences ─────────────────────────────────────────────
        sequences_payload = {
            "campaignId": campaign_id,
            "sequences": [
                {
                    "steps": [
                        {
                            "variants":    [{"subject": v.subject, "body": v.body} for v in step.variants],
                            "type":        step.type,
                            "delay":       step.delay,
                            "ccEnabled":   step.ccEnabled,
                            "bccEnabled":  step.bccEnabled,
                            "ccVariables":  step.ccVariables,
                            "bccVariables": step.bccVariables,
                        }
                        for step in body.sequences
                    ]
                }
            ],
        }
        await ri_post("/campaigns/add-sequence", sequences_payload)
        steps_completed.append(f"✅ {len(body.sequences)} sequence step(s) added")

        # ── Step 3: Set schedule ──────────────────────────────────────────────
        from datetime import datetime, timedelta
        start_date = datetime.utcnow().strftime("%Y-%m-%dT00:00:00.000Z")
        end_date   = (datetime.utcnow() + timedelta(days=365)).strftime("%Y-%m-%dT00:00:00.000Z")

        schedule_payload = {
            "campaignId": campaign_id,
            "startDate":  start_date,
            "endDate":    end_date,
            "schedules": [
                {
                    "name": "KJE Campaign Schedule",
                    "timing": {
                        "from": body.start_hour,
                        "to":   body.end_hour,
                    },
                    "days": {
                        "1": body.send_monday,
                        "2": body.send_tuesday,
                        "3": body.send_wednesday,
                        "4": body.send_thursday,
                        "5": body.send_friday,
                        "6": body.send_saturday,
                        "0": body.send_sunday,
                    },
                    "timezone": body.timezone,
                }
            ],
        }
        await ri_put("/campaigns/set-schedule", schedule_payload)
        steps_completed.append("✅ Schedule set")

        # ── Step 4: Update campaign options ───────────────────────────────────
        options_payload = {
            "campaignId":           campaign_id,
            "dailyLimit":           body.daily_limit,
            "maxNewLeads":          body.max_new_leads,
            "stopOnReply":          body.stop_on_reply,
            "tracking":             body.track_opens,
            "linkTracking":         body.track_links,
            "deliveryOptimizations": body.delivery_mode,
            "globalUnsubscribe":    True,
            "unsubscribeHeader":    True,
            "prioritizeNewLeads":   True,
        }
        await ri_post("/campaigns/update-details", options_payload)
        steps_completed.append("✅ Campaign options updated")

       # ── Step 5: Set email accounts (emails, not int IDs) ──────────────────
        if account_emails:
            accounts_payload = {
                "campaignId":    campaign_id,
                "accountsToUse": account_emails,
            }
            logger.info(
                f"[reachinbox.set_accounts] sending payload: "
                f"campaignId={campaign_id} emails={account_emails!r}"
            )
            try:
                set_accounts_result = await ri_put("/campaigns/set-accounts", accounts_payload)
                logger.info(
                    f"[reachinbox.set_accounts] success response: {set_accounts_result!r}"
                )
            except Exception as e_setacct:
                logger.error(
                    f"[reachinbox.set_accounts] FAILED — campaignId={campaign_id} "
                    f"emails={account_emails!r} error={e_setacct!r}"
                )
                raise
            steps_completed.append(
                f"✅ {len(account_emails)} email account(s) assigned "
                f"({len(body.account_ids)} id(s) translated)"
            )

        # ── Step 6: Add pre-fetched KJLE leads to campaign ────────────────────
        ri_leads = [map_lead_to_ri(lead) for lead in kjle_leads if lead.get("email")]

        batch_size = 100
        for i in range(0, len(ri_leads), batch_size):
            batch = ri_leads[i:i+batch_size]
            add_payload = {
                "campaignId": campaign_id,
                "leads":      batch,
            }
            try:
                add_result = await ri_post("/leads/add", add_payload)
                logger.info(
                    f"[reachinbox.add_leads] batch {i}: sent {len(batch)} leads, "
                    f"result={str(add_result)[:200]}"
                )
                leads_added += len(batch)
            except Exception as e:
                logger.warning(
                    f"[reachinbox.add_leads] batch {i} FAILED: "
                    f"sample_payload={str(add_payload)[:300]!r} error={e!r}"
                )
                leads_skipped += len(batch)

        steps_completed.append(f"✅ {leads_added} leads added ({leads_skipped} skipped)")

        # ── Step 7: Launch campaign (optional) ───────────────────────────────
        if body.auto_launch and account_emails:
            await ri_post("/campaigns/start", {"campaignId": campaign_id})
            steps_completed.append("🚀 Campaign launched!")

    except HTTPException as e:
        # DIAG 2.1: confirm we reached the rollback path
        logger.error(
            f"[reachinbox.create] HTTPException caught at rollback path — "
            f"campaign_id={campaign_id} steps_done={len(steps_completed)} "
            f"status={e.status_code} detail={str(e.detail)[:200]}"
        )
        # Steps 1-7 fault: roll back the RI campaign we just created so the
        # caller doesn't accumulate orphan drafts on retry.
        if campaign_id is not None:
            logger.warning(
                f"[reachinbox.create] rollback: attempting DELETE /campaigns/{campaign_id}"
            )
            try:
                delete_result = await ri_delete(f"/campaigns/{campaign_id}")
                logger.warning(
                    f"[reachinbox.create] rollback: deleted orphan campaign "
                    f"{campaign_id} after failure at step {len(steps_completed)} "
                    f"result={delete_result!r}"
                )
            except Exception as del_err:
                logger.error(
                    f"[reachinbox.create] rollback DELETE failed for campaign "
                    f"{campaign_id}: {del_err!r}"
                )
        else:
            logger.error(
                f"[reachinbox.create] rollback SKIPPED — campaign_id is None "
                f"(failure happened before campaign was created)"
            )
        raise
    except Exception as e:
        if campaign_id is not None:
            # ReachInbox exposes NO campaign-delete endpoint (verified against
            # their API docs + live 404s on every delete variant). Best-effort
            # rollback = PAUSE the orphan draft so it can never send, then leave
            # it for manual deletion in the RI web UI.
            try:
                await ri_post("/campaigns/pause", {"campaignId": campaign_id})
                logger.warning(
                    f"[reachinbox.create] rollback: paused orphan campaign "
                    f"{campaign_id} after failure at step {len(steps_completed)+1} "
                    f"(RI has no delete API - delete it manually in the RI web UI)"
                )
            except Exception as pause_err:
                logger.error(
                    f"[reachinbox.create] rollback pause failed for campaign "
                    f"{campaign_id}: {pause_err} "
                    f"(orphan left as draft - delete it manually in the RI web UI)"
                )
        logger.error(f"Campaign creation failed at step {len(steps_completed)+1}: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "error":           str(e),
                "steps_completed": steps_completed,
                "campaign_id":     campaign_id,
                "rollback":        "attempted" if campaign_id is not None else "not_needed",
            }
        )

    # ── Step 8: Auto-register in campaign_performance (non-fatal) ────────────
    try:
        from .campaigns import _auto_register_from_ri_create
        launched = bool(body.auto_launch and account_emails)
        await _auto_register_from_ri_create(
            ri_campaign_id = str(campaign_id),
            campaign_name  = body.name,
            niche          = body.lead_filter.niche_slug,
            domain_used    = body.domain_used,
            offer_type     = body.offer_type,
            leads_count    = leads_added,
            launched       = launched,
        )
        steps_completed.append("📊 Registered in campaign_performance")
    except Exception as e:
        logger.warning(f"[auto-register] non-fatal (campaign_id={campaign_id}): {e}")
        steps_completed.append(f"⚠️ auto-register skipped: {str(e)[:100]}")

    return {
        "status":          "success",
        "campaign_id":     campaign_id,
        "campaign_name":   body.name,
        "steps_completed": steps_completed,
        "summary": {
            "leads_added":      leads_added,
            "sequences":        len(body.sequences),
            "accounts_assigned": len(account_emails),
            "auto_launched":    body.auto_launch,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /reachinbox/leads/add
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/reachinbox/leads/add")
async def add_leads_to_campaign(body: AddLeadsRequest):
    """Add KJLE leads to an existing ReachInbox campaign."""
    kjle_leads = fetch_kjle_leads(body.lead_filter)

    if not kjle_leads:
        return {"status": "success", "added": 0, "message": "No matching leads found"}

    ri_leads    = [map_lead_to_ri(lead) for lead in kjle_leads if lead.get("email")]
    leads_added = 0

    batch_size = 100
    for i in range(0, len(ri_leads), batch_size):
        batch = ri_leads[i:i+batch_size]
        add_payload = {
            "campaignId": body.campaign_id,
            "leads":      batch,
        }
        try:
            add_result = await ri_post("/leads/add", add_payload)
            logger.info(
                f"[reachinbox.add_leads] batch {i}: sent {len(batch)} leads, "
                f"result={str(add_result)[:200]}"
            )
            leads_added += len(batch)
        except Exception as e:
            logger.warning(
                f"[reachinbox.add_leads] batch {i} FAILED: "
                f"sample_payload={str(add_payload)[:300]!r} error={e!r}"
            )

    return {
        "status":      "success",
        "campaign_id": body.campaign_id,
        "added":       leads_added,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /reachinbox/campaigns/launch
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/reachinbox/campaigns/launch")
async def launch_campaign(campaign_id: int):
    """Launch a campaign that's ready to send."""
    await ri_post("/campaigns/start", {"campaignId": campaign_id})
    return {"status": "success", "campaign_id": campaign_id, "message": "Campaign launched 🚀"}


# ─────────────────────────────────────────────────────────────────────────────
# POST /reachinbox/campaigns/pause
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/reachinbox/campaigns/pause")
async def pause_campaign(campaign_id: int):
    """Pause a running campaign."""
    await ri_post("/campaigns/pause", {"campaignId": campaign_id})
    return {"status": "success", "campaign_id": campaign_id, "message": "Campaign paused"}


# ─────────────────────────────────────────────────────────────────────────────
# GET /reachinbox/campaigns/{campaign_id}/status
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/reachinbox/campaigns/{campaign_id}/status")
async def get_campaign_status(campaign_id: int):
    """Get detailed stats for a specific campaign."""
    data = await ri_get(f"/campaigns/{campaign_id}/status")
    return {
        "status":      "success",
        "campaign_id": campaign_id,
        "data":        data.get("data", {}),
    }
