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

logger = logging.getLogger(__name__)
router = APIRouter()

RI_BASE    = "https://api.reachinbox.ai/api/v1"
RI_HEADERS = {
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

class AddLeadsRequest(BaseModel):
    campaign_id:   int
    lead_filter:   CampaignLeadFilter


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def ri_get(path: str, params: dict = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{RI_BASE}{path}", headers=RI_HEADERS, params=params or {})
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"ReachInbox error: {r.text[:200]}")
        return r.json()

async def ri_post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{RI_BASE}{path}", headers=RI_HEADERS, json=body)
        if r.status_code not in [200, 201]:
            raise HTTPException(status_code=r.status_code, detail=f"ReachInbox error: {r.text[:200]}")
        return r.json()

async def ri_put(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.put(f"{RI_BASE}{path}", headers=RI_HEADERS, json=body)
        if r.status_code not in [200, 201]:
            raise HTTPException(status_code=r.status_code, detail=f"ReachInbox error: {r.text[:200]}")
        return r.json()

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

    query = query.order("pain_score", desc=True).limit(limit)
    result = query.execute()
    return result.data or []

def map_lead_to_ri(lead: dict) -> dict:
    """Map KJLE lead to ReachInbox lead format."""
    name_parts = (lead.get("business_name") or "").split()
    first_name = name_parts[0] if name_parts else "Business"
    company    = lead.get("business_name") or ""

    return {
        "email": lead.get("email") or "",
        "attributes": {
            "firstName":   first_name,
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
    Create a complete ReachInbox campaign in one call:
    1. Create campaign
    2. Add email sequences
    3. Set schedule
    4. Set email accounts
    5. Update campaign options
    6. Add KJLE leads
    7. Optionally launch
    """

    steps_completed = []
    campaign_id     = None

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

        # ── Step 5: Set email accounts ────────────────────────────────────────
        if body.account_ids:
            accounts_payload = {
                "campaignId": campaign_id,
                "emails":     body.account_ids,
            }
            await ri_put("/campaigns/set-accounts", accounts_payload)
            steps_completed.append(f"✅ {len(body.account_ids)} email account(s) assigned")

        # ── Step 6: Fetch KJLE leads and add to campaign ──────────────────────
        kjle_leads = fetch_kjle_leads(body.lead_filter)
        leads_added = 0
        leads_skipped = 0

        if kjle_leads:
            ri_leads = [map_lead_to_ri(lead) for lead in kjle_leads if lead.get("email")]

            # Add leads in batches of 100
            batch_size = 100
            for i in range(0, len(ri_leads), batch_size):
                batch = ri_leads[i:i+batch_size]
                add_payload = {
                    "campaignId": campaign_id,
                    "leads":      batch,
                }
                try:
                    await ri_post("/leads/addLeadsToCampaign", add_payload)
                    leads_added += len(batch)
                except Exception as e:
                    logger.warning(f"Batch {i} lead add failed: {e}")
                    leads_skipped += len(batch)

        steps_completed.append(f"✅ {leads_added} leads added ({leads_skipped} skipped)")

        # ── Step 7: Launch campaign (optional) ───────────────────────────────
        if body.auto_launch and body.account_ids:
            await ri_post("/campaigns/start", {"campaignId": campaign_id})
            steps_completed.append("🚀 Campaign launched!")

        return {
            "status":          "success",
            "campaign_id":     campaign_id,
            "campaign_name":   body.name,
            "steps_completed": steps_completed,
            "summary": {
                "leads_added":      leads_added,
                "sequences":        len(body.sequences),
                "accounts_assigned": len(body.account_ids),
                "auto_launched":    body.auto_launch,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Campaign creation failed at step {len(steps_completed)+1}: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "error":           str(e),
                "steps_completed": steps_completed,
                "campaign_id":     campaign_id,
            }
        )


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
        await ri_post("/leads/addLeadsToCampaign", {
            "campaignId": body.campaign_id,
            "leads":      batch,
        })
        leads_added += len(batch)

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
