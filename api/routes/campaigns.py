"""
KJLE — Campaign Performance Tracking (project-aware)
Routes: /kjle/v1/campaigns/performance/*

Backs:
  - campaign_performance table (per-campaign stats across projects/servers)
  - project_domains lookup table (domain → project inference)

Routes registered by main.py with prefix=/kjle/v1:
  GET    /campaigns/performance                  — list (public)
  GET    /campaigns/performance/summary          — rollup (public)
  GET    /campaigns/performance/{id}             — detail (public)
  POST   /campaigns/performance/register         — upsert (auth)
  POST   /campaigns/performance/{id}/attribute   — demo/close events (auth)
  PATCH  /campaigns/performance/{id}             — notes/status/metadata (auth)

Also exposes two helpers used by other modules:
  - _auto_register_from_ri_create: called from reachinbox.py /campaigns/create
  - get_campaign_activity_summary_24h: STABLE signature for Prompt 2 daily_cost_report
"""

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Auth ─────────────────────────────────────────────────────────────────────
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Constants ────────────────────────────────────────────────────────────────
PERF_TABLE       = "campaign_performance"
DOMAIN_TABLE     = "project_domains"
FALLBACK_PROJECT = "kjle"


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class RegisterCampaignRequest(BaseModel):
    project:                Optional[str] = None   # auto-inferred from domain if None
    reachinbox_server:      str = "server_1"
    reachinbox_campaign_id: Optional[str] = None
    campaign_name:          str
    niche:                  Optional[str] = None
    domain_used:            Optional[str] = None
    offer_type:             Optional[str] = None
    leads_count:            int = 0
    status:                 str = "draft"
    started_at:             Optional[datetime] = None
    metadata:               Optional[dict] = None


class AttributeRequest(BaseModel):
    event_type:  Literal["demo_booked", "closed"]
    revenue_usd: Optional[float] = None   # required when event_type='closed'
    notes:       Optional[str]   = None


class PatchCampaignRequest(BaseModel):
    status:   Optional[str]  = None
    notes:    Optional[str]  = None
    metadata: Optional[dict] = None
    # Immutable identity fields — 422 if present in body:
    project:           Optional[str] = None
    reachinbox_server: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lookup_project_by_domain(domain: Optional[str]) -> Optional[str]:
    """SELECT project FROM project_domains WHERE domain = $1. Returns None if not found."""
    if not domain:
        return None
    db = get_db()
    try:
        res = (
            db.table(DOMAIN_TABLE)
            .select("project")
            .eq("domain", domain.strip().lower())
            .execute()
        )
        if res.data:
            return res.data[0]["project"]
    except Exception as e:
        logger.warning(f"_lookup_project_by_domain error for {domain}: {e}")
    return None


def _resolve_project(
    explicit_project: Optional[str],
    domain_used: Optional[str],
    ri_campaign_id: Optional[str],
) -> str:
    """
    Resolution order:
      1. explicit project (if truthy) → use it
      2. lookup project_domains by domain_used → use match
      3. fallback FALLBACK_PROJECT (logs a searchable warning)
    """
    if explicit_project:
        return explicit_project
    found = _lookup_project_by_domain(domain_used)
    if found:
        return found
    # Actionable fallback warning — greppable
    logger.warning(
        f"campaign_performance.register: domain='{domain_used}' not in project_domains, "
        f"defaulting to '{FALLBACK_PROJECT}' (campaign_id={ri_campaign_id or 'none'})"
    )
    return FALLBACK_PROJECT


def _serialize_row(row: dict) -> dict:
    """Normalize numeric/decimal fields for JSON serialization."""
    if row.get("revenue_attributed_usd") is not None:
        row["revenue_attributed_usd"] = float(row["revenue_attributed_usd"])
    return row


# ─────────────────────────────────────────────────────────────────────────────
# Internal helper — called by reachinbox.py /campaigns/create auto-register hook
# ─────────────────────────────────────────────────────────────────────────────

async def _auto_register_from_ri_create(
    ri_campaign_id: str,
    campaign_name: str,
    niche: Optional[str],
    domain_used: Optional[str],
    offer_type: Optional[str],
    leads_count: int,
    launched: bool,
) -> int:
    """
    Auto-register a campaign created via /reachinbox/campaigns/create.
    Idempotent: if (project, server, ri_campaign_id) already exists, updates it.

    Returns the campaign_performance row id.
    Raises on DB failure — caller should catch and log (non-fatal).
    """
    project = _resolve_project(None, domain_used, ri_campaign_id)
    server  = "server_1"
    status  = "active" if launched else "draft"
    started_at = _now_iso() if launched else None

    payload = {
        "project":                project,
        "reachinbox_server":      server,
        "reachinbox_campaign_id": ri_campaign_id,
        "campaign_name":          campaign_name,
        "niche":                  niche,
        "domain_used":            domain_used,
        "offer_type":             offer_type,
        "leads_count":            leads_count,
        "status":                 status,
        "started_at":             started_at,
    }

    db = get_db()

    existing = (
        db.table(PERF_TABLE)
        .select("id")
        .eq("project", project)
        .eq("reachinbox_server", server)
        .eq("reachinbox_campaign_id", ri_campaign_id)
        .execute().data or []
    )

    if existing:
        db.table(PERF_TABLE).update(payload).eq("id", existing[0]["id"]).execute()
        return existing[0]["id"]

    res = db.table(PERF_TABLE).insert(payload).execute()
    return res.data[0]["id"]


# ─────────────────────────────────────────────────────────────────────────────
# Helper exported for Prompt 2's daily_cost_report job
# ─────────────────────────────────────────────────────────────────────────────

# NOTE: Prompt 2's daily_report integration consumes api/lib/campaign_activity.py
# (contract: get_kjle_campaign_activity_summary(since_utc) → List[Dict]).
# This helper is richer (per-project, totals, reply_rate, revenue) — kept for
# future UI / dashboard consumers. Signature is stable; don't change without review.
async def get_campaign_activity_summary_24h(project: str = "kjle") -> dict:
    """
    Returns a project-scoped 24h activity summary for daily digest rendering.

    STABLE CONTRACT (do not change field names / types without coordinating):

    Args:
        project: Project name (e.g., 'kjle', 'telehealth'). Default 'kjle'.

    Returns dict with keys:
        project            (str)
        window_hours       (int)    — always 24
        has_activity       (bool)   — False iff campaigns list is empty
        total_campaigns    (int)
        total_sent         (int)
        total_replied      (int)
        total_demos        (int)
        total_revenue_usd  (float)
        campaigns          (list of dict), each:
            name           (str)
            niche          (str|None)
            sent           (int)
            replied        (int)
            reply_rate     (float, 0-100, 2dp)
            demos          (int)
            revenue_usd    (float)

    "Activity" means: status IN ('active','paused') AND updated_at >= now() - 24h
    AND sent_count > 0.

    Empty-result shape (safe to render as "no activity"):
        {"project": ..., "window_hours": 24, "has_activity": False,
         "total_campaigns": 0, "total_sent": 0, "total_replied": 0,
         "total_demos": 0, "total_revenue_usd": 0.0, "campaigns": []}
    """
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    try:
        rows = (
            db.table(PERF_TABLE)
            .select("*")
            .eq("project", project)
            .in_("status", ["active", "paused"])
            .gte("updated_at", cutoff)
            .gt("sent_count", 0)
            .execute().data or []
        )
    except Exception as e:
        logger.error(f"get_campaign_activity_summary_24h({project}) query failed: {e}")
        rows = []

    campaigns = []
    total_sent    = 0
    total_replied = 0
    total_demos   = 0
    total_revenue = 0.0

    for r in rows:
        sent    = int(r.get("sent_count") or 0)
        replied = int(r.get("replied_count") or 0)
        demos   = int(r.get("demo_booked_count") or 0)
        revenue = float(r.get("revenue_attributed_usd") or 0)
        reply_rate = round(replied / sent * 100, 2) if sent else 0.0

        campaigns.append({
            "name":        r.get("campaign_name") or "(unnamed)",
            "niche":       r.get("niche"),
            "sent":        sent,
            "replied":     replied,
            "reply_rate":  reply_rate,
            "demos":       demos,
            "revenue_usd": revenue,
        })
        total_sent    += sent
        total_replied += replied
        total_demos   += demos
        total_revenue += revenue

    return {
        "project":           project,
        "window_hours":      24,
        "has_activity":      bool(campaigns),
        "total_campaigns":   len(campaigns),
        "total_sent":        total_sent,
        "total_replied":     total_replied,
        "total_demos":       total_demos,
        "total_revenue_usd": round(total_revenue, 2),
        "campaigns":         campaigns,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /campaigns/performance (list)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/campaigns/performance")
async def list_campaigns(
    project: str = Query("kjle"),
    status:  Optional[str] = Query(None),
    niche:   Optional[str] = Query(None),
    limit:   int = Query(50, ge=1, le=500),
    offset:  int = Query(0, ge=0),
):
    db = get_db()
    query = db.table(PERF_TABLE).select("*").eq("project", project)
    if status:
        query = query.eq("status", status)
    if niche:
        query = query.eq("niche", niche)
    rows = (
        query.order("started_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute().data or []
    )
    return {
        "status":    "success",
        "project":   project,
        "filters":   {"status": status, "niche": niche, "limit": limit, "offset": offset},
        "count":     len(rows),
        "campaigns": [_serialize_row(r) for r in rows],
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /campaigns/performance/summary   (⚠️ must register before /{id})
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/campaigns/performance/summary")
async def campaign_summary(project: str = Query("kjle")):
    db = get_db()
    rows = (
        db.table(PERF_TABLE)
        .select("*")
        .eq("project", project)
        .execute().data or []
    )

    total_campaigns = len(rows)
    total_sent    = sum(int(r.get("sent_count")        or 0) for r in rows)
    total_replied = sum(int(r.get("replied_count")     or 0) for r in rows)
    total_demos   = sum(int(r.get("demo_booked_count") or 0) for r in rows)
    total_revenue = round(
        sum(float(r.get("revenue_attributed_usd") or 0) for r in rows), 2
    )
    overall_reply_rate = round(total_replied / total_sent * 100, 2) if total_sent else 0.0

    def _bucket():
        return {"campaigns": 0, "sent": 0, "replied": 0, "demos": 0, "revenue_usd": 0.0}

    by_niche  = defaultdict(_bucket)
    by_domain = defaultdict(_bucket)

    for r in rows:
        nk = r.get("niche")       or "(none)"
        dk = r.get("domain_used") or "(none)"
        for bucket in (by_niche[nk], by_domain[dk]):
            bucket["campaigns"]   += 1
            bucket["sent"]        += int(r.get("sent_count")        or 0)
            bucket["replied"]     += int(r.get("replied_count")     or 0)
            bucket["demos"]       += int(r.get("demo_booked_count") or 0)
            bucket["revenue_usd"] += float(r.get("revenue_attributed_usd") or 0)

    def _finalize(d: dict) -> list:
        out = []
        for k, v in d.items():
            item = dict(v)
            item["key"]         = k
            item["reply_rate"]  = round(item["replied"] / item["sent"] * 100, 2) if item["sent"] else 0.0
            item["revenue_usd"] = round(item["revenue_usd"], 2)
            out.append(item)
        return sorted(out, key=lambda x: x["sent"], reverse=True)

    return {
        "status":  "success",
        "project": project,
        "totals": {
            "campaigns":   total_campaigns,
            "sent":        total_sent,
            "replied":     total_replied,
            "demos":       total_demos,
            "revenue_usd": total_revenue,
        },
        "overall_reply_rate": overall_reply_rate,
        "by_niche":           _finalize(by_niche),
        "by_domain":          _finalize(by_domain),
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /campaigns/performance/{id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/campaigns/performance/{id}")
async def get_campaign(id: int):
    db = get_db()
    res = db.table(PERF_TABLE).select("*").eq("id", id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Campaign {id} not found")
    return {"status": "success", "campaign": _serialize_row(res.data[0])}


# ─────────────────────────────────────────────────────────────────────────────
# POST /campaigns/performance/register
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/campaigns/performance/register")
async def register_campaign(body: RegisterCampaignRequest, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)

    project = _resolve_project(body.project, body.domain_used, body.reachinbox_campaign_id)
    started = body.started_at.isoformat() if body.started_at else None
    if not started and body.status == "active":
        started = _now_iso()

    payload = {
        "project":                project,
        "reachinbox_server":      body.reachinbox_server,
        "reachinbox_campaign_id": body.reachinbox_campaign_id,
        "campaign_name":          body.campaign_name,
        "niche":                  body.niche,
        "domain_used":            body.domain_used,
        "offer_type":             body.offer_type,
        "leads_count":            body.leads_count,
        "status":                 body.status,
        "started_at":             started,
        "metadata":               body.metadata,
    }

    db = get_db()

    existing = None
    if body.reachinbox_campaign_id:
        res = (
            db.table(PERF_TABLE)
            .select("id")
            .eq("project", project)
            .eq("reachinbox_server", body.reachinbox_server)
            .eq("reachinbox_campaign_id", body.reachinbox_campaign_id)
            .execute()
        )
        existing = res.data[0] if res.data else None

    if existing:
        db.table(PERF_TABLE).update(payload).eq("id", existing["id"]).execute()
        row = db.table(PERF_TABLE).select("*").eq("id", existing["id"]).execute().data[0]
        return {
            "status":           "success",
            "action":           "updated",
            "project_resolved": project,
            "campaign":         _serialize_row(row),
        }

    res = db.table(PERF_TABLE).insert(payload).execute()
    return {
        "status":           "success",
        "action":           "created",
        "project_resolved": project,
        "campaign":         _serialize_row(res.data[0]),
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /campaigns/performance/{id}/attribute
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/campaigns/performance/{id}/attribute")
async def attribute_event(id: int, body: AttributeRequest, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)

    if body.event_type == "closed" and body.revenue_usd is None:
        raise HTTPException(status_code=422, detail="revenue_usd is required for event_type='closed'")

    db = get_db()
    res = db.table(PERF_TABLE).select("*").eq("id", id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Campaign {id} not found")
    row = res.data[0]

    event = {
        "event_type":  body.event_type,
        "revenue_usd": body.revenue_usd,
        "notes":       body.notes,
        "at":          _now_iso(),
    }
    metadata = dict(row.get("metadata") or {})
    events = list(metadata.get("attribution_events") or [])
    events.append(event)
    metadata["attribution_events"] = events

    update_payload: dict = {"metadata": metadata}

    if body.event_type == "demo_booked":
        update_payload["demo_booked_count"] = int(row.get("demo_booked_count") or 0) + 1
    elif body.event_type == "closed":
        update_payload["closed_count"] = int(row.get("closed_count") or 0) + 1
        update_payload["revenue_attributed_usd"] = round(
            float(row.get("revenue_attributed_usd") or 0) + float(body.revenue_usd or 0), 2
        )

    db.table(PERF_TABLE).update(update_payload).eq("id", id).execute()
    updated = db.table(PERF_TABLE).select("*").eq("id", id).execute().data[0]
    return {"status": "success", "event": event, "campaign": _serialize_row(updated)}


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /campaigns/performance/{id}
# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/campaigns/performance/{id}")
async def patch_campaign(id: int, body: PatchCampaignRequest, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)

    if body.project is not None or body.reachinbox_server is not None:
        raise HTTPException(
            status_code=422,
            detail="project and reachinbox_server are immutable — cannot be changed via PATCH",
        )

    db = get_db()
    res = db.table(PERF_TABLE).select("*").eq("id", id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Campaign {id} not found")
    row = res.data[0]

    update_payload: dict = {}

    if body.status is not None:
        update_payload["status"] = body.status

    if body.metadata is not None:
        merged = dict(row.get("metadata") or {})
        merged.update(body.metadata)
        update_payload["metadata"] = merged

    if body.notes is not None:
        meta = update_payload.get("metadata") or dict(row.get("metadata") or {})
        meta["notes"] = body.notes
        update_payload["metadata"] = meta

    if not update_payload:
        raise HTTPException(status_code=422, detail="No updatable fields provided")

    db.table(PERF_TABLE).update(update_payload).eq("id", id).execute()
    updated = db.table(PERF_TABLE).select("*").eq("id", id).execute().data[0]
    return {"status": "success", "campaign": _serialize_row(updated)}
