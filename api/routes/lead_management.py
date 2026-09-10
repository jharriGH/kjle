"""
KJLE — Prompt 28: Lead Management Routes
File: api/routes/lead_management.py
"""

import os
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Header, Query, Request
from pydantic import BaseModel
from supabase import create_client, Client

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")

router = APIRouter()

_PROVIDER_BUCKETS = [
    "gmail_consumer", "google_workspace", "office365", "ms_consumer",
    "yahoo", "apple", "other", "self_hosted", "unknown",
]


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


def _reattach_cutoff_iso(supabase) -> Optional[str]:
    """ISO cutoff for reattach-cooldown dedup, or None when disabled."""
    try:
        res = (
            supabase.table("admin_settings")
            .select("value")
            .eq("key", "campaign_reattach_cooldown_days")
            .limit(1)
            .execute()
        )
        rows = res.data or []
        days = int(rows[0]["value"]) if rows else 30
    except Exception:
        days = 30
    if days <= 0:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def score_to_segment(pain_score) -> str:
    """v2 thresholds: HOT >= 30 | WARM 15-29 | COLD < 15."""
    if pain_score is None:
        return "unclassified"
    try:
        score = float(pain_score)
    except (ValueError, TypeError):
        return "unclassified"
    if score >= 30:
        return "hot"
    elif score >= 15:
        return "warm"
    else:
        return "cold"


# ─────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────

class BulkActionRequest(BaseModel):
    lead_ids: List[str]
    action: str  # delete | reclassify | dnc | push_demoenginez | push_voicedrop


class EnrichmentUpdate(BaseModel):
    demo_url: str


class BatchEnrichmentItem(BaseModel):
    lead_id: str
    demo_url: str


class BatchEnrichmentRequest(BaseModel):
    updates: List[BatchEnrichmentItem]


class EnrichmentBulkItem(BaseModel):
    lead_id: str
    fields: Dict[str, Any]


class EnrichmentBulkRequest(BaseModel):
    updates: List[EnrichmentBulkItem]


class MarkContactedItem(BaseModel):
    lead_id: str
    campaign_id: str
    product: str
    sequence_step: int = 1
    status: str = "sent"
    contacted_at: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class MarkContactedRequest(BaseModel):
    # Single-item form
    lead_id: Optional[str] = None
    campaign_id: Optional[str] = None
    product: Optional[str] = None
    sequence_step: int = 1
    status: str = "sent"
    contacted_at: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    # Batch form
    updates: Optional[List[MarkContactedItem]] = None


# ─────────────────────────────────────────────────
# GET /kjle/v1/leads/stats
# ─────────────────────────────────────────────────

_stats_cache: dict = {}
_CACHE_TTL = 60


@router.get("/leads/stats")
async def lead_stats(x_api_key: str = Header(...)):
    verify_api_key(x_api_key)

    now = time.monotonic()
    if _stats_cache.get("ts") and now - _stats_cache["ts"] < _CACHE_TTL:
        return _stats_cache["data"]

    supabase = get_supabase()

    try:
        res = supabase.rpc("get_lead_stats").execute()
        data = res.data
        if not data:
            raise ValueError("get_lead_stats returned no data")
        _stats_cache.update({"data": data, "ts": now})
        return data
    except Exception as e:
        logger.error(f"lead_stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/leads/bulk
# ─────────────────────────────────────────────────

@router.post("/leads/bulk")
async def bulk_action(payload: BulkActionRequest, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)

    if len(payload.lead_ids) > 500:
        raise HTTPException(status_code=400, detail="Max 500 lead_ids per bulk request")

    valid_actions = {"delete", "reclassify", "dnc", "push_demoenginez", "push_voicedrop"}
    if payload.action not in valid_actions:
        raise HTTPException(status_code=400, detail=f"Invalid action. Must be one of: {valid_actions}")

    supabase = get_supabase()
    processed = 0
    errors = []

    try:
        if payload.action == "delete":
            supabase.table("leads").delete().in_("id", payload.lead_ids).execute()
            processed = len(payload.lead_ids)

        elif payload.action == "dnc":
            try:
                supabase.table("leads").update({"do_not_contact": True}).in_("id", payload.lead_ids).execute()
                processed = len(payload.lead_ids)
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"DNC update failed — do_not_contact column may not exist. Run migration first. Error: {e}"
                )

        elif payload.action == "reclassify":
            leads_res = supabase.table("leads").select("id, pain_score").in_("id", payload.lead_ids).execute()
            now = datetime.now(timezone.utc).isoformat()
            for lead in (leads_res.data or []):
                try:
                    new_label = score_to_segment(lead.get("pain_score"))
                    supabase.table("leads").update({
                        "segment_label": new_label,
                        "segmented_at": now,
                    }).eq("id", lead["id"]).execute()
                    processed += 1
                except Exception as e:
                    errors.append({"lead_id": lead["id"], "error": str(e)})

        elif payload.action in ("push_demoenginez", "push_voicedrop"):
            raise HTTPException(
                status_code=400,
                detail="push_demoenginez and push_voicedrop bulk actions must be triggered via the dedicated push routes."
            )

        return {
            "action": payload.action,
            "processed": processed,
            "errors": errors,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"bulk_action error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# GET /kjle/v1/leads/eligible-for-campaign
# ─────────────────────────────────────────────────

_DNC_STATUS_BLOCKED = (
    "fed_dnc_flagged",
    "tcpa_litigator_flagged",
    "searchbug_dnc",
    "internal_suppression",
    "leadcrap_filtered",
)

_ELIGIBLE_KNOWN_PARAMS = frozenset({
    "vertical", "niche", "segment_id", "pain_min", "limit", "offset",
    "require_email_valid", "require_name_verified", "audited_after",
    "min_word_count", "min_internal_pages", "email_provider", "email_trust",
    "product", "exclude_already_contacted", "cooldown_days",
})


@router.get("/leads/eligible-for-campaign")
async def eligible_for_campaign(
    request: Request,
    vertical: Optional[str] = Query(None, description="Vertical (alias for niche)"),
    niche: Optional[str] = Query(None, description="niche_slug filter; takes precedence over `vertical`"),
    segment_id: Optional[str] = Query(None, description="Saved segment id; applies its stored filters"),
    pain_min: Optional[int] = Query(None, description="Minimum pain_score (inclusive)"),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    require_email_valid: bool = Query(True, description="When true, restrict to email_status='valid' + email_valid=true"),
    require_name_verified: bool = Query(False, description="When true, restrict to name_website_verified=true"),
    audited_after: Optional[str] = Query(None, description="ISO timestamp; restrict to last_audited_at > this value"),
    min_word_count: Optional[int] = Query(None, description="Minimum website_word_count"),
    min_internal_pages: Optional[int] = Query(None, description="Minimum website_internal_page_count"),
    email_provider: Optional[str] = Query(None, description="Filter by email provider bucket(s). Single value or comma-separated."),
    email_trust: Optional[str] = Query(None, description="Filter by email trust value(s). Single value or comma-separated. Values: valid|catch_all|role|unconfirmable|invalid"),
    product: Optional[str] = Query(None, description="Product slug (e.g. compliancemds, bizreply). Used with exclude_already_contacted."),
    exclude_already_contacted: bool = Query(False, description="When true and product is set, exclude leads with a lead_campaign_history row for that product."),
    cooldown_days: Optional[int] = Query(None, description="Exclude leads whose last_contacted_at is within this many days."),
    x_api_key: str = Header(...),
):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    skipped_filters: List[str] = []

    for _qk in request.query_params:
        if _qk not in _ELIGIBLE_KNOWN_PARAMS:
            skipped_filters.append(f"unrecognized:{_qk}")

    # ── segment_id passthrough ─────────────────────────────────────────────────
    seg_niche: Optional[str] = None
    seg_pain_min: Optional[int] = None
    seg_label: Optional[str] = None
    if segment_id:
        try:
            seg_res = (
                supabase.table("segments")
                .select("id, segment_label, niche_slug, filters")
                .eq("id", segment_id)
                .limit(1)
                .execute()
            )
            seg_rows = seg_res.data or []
            if not seg_rows:
                raise HTTPException(status_code=404, detail=f"Segment '{segment_id}' not found")
            seg = seg_rows[0]
            seg_filters = seg.get("filters") or {}
            if not isinstance(seg_filters, dict):
                seg_filters = {}
            seg_niche = seg.get("niche_slug") or seg_filters.get("niche_slug")
            seg_label = seg.get("segment_label") or seg_filters.get("segment_label")
            seg_pain_min = seg_filters.get("min_pain")
        except HTTPException:
            raise
        except Exception as e:
            skipped_filters.append(f"segment_lookup_failed:{e}")

    effective_niche = niche or vertical or seg_niche
    effective_pain_min = pain_min if pain_min is not None else seg_pain_min

    # ── base query ─────────────────────────────────────────────────────────────
    select_cols = (
        "id, business_name, email, phone, niche_slug, pain_score, dnc_status, "
        "website, name_website_verified, name_match_score, last_audited_at, "
        "website_word_count, website_internal_page_count, city, state, "
        "website_reachable, enrichment, email_provider, email_trust"
    )
    query = supabase.table("leads").select(select_cols).eq("is_active", True)
    count_query = supabase.table("leads").select("id", count="estimated").eq("is_active", True)

    if effective_niche:
        query = query.eq("niche_slug", effective_niche)
        count_query = count_query.eq("niche_slug", effective_niche)

    if seg_label and seg_label != "custom":
        query = query.eq("segment_label", seg_label)
        count_query = count_query.eq("segment_label", seg_label)

    if effective_pain_min is not None:
        query = query.gte("pain_score", int(effective_pain_min))
        count_query = count_query.gte("pain_score", int(effective_pain_min))

    # ── email filters ──────────────────────────────────────────────────────────
    query = query.neq("email", None)
    count_query = count_query.neq("email", None)
    if require_email_valid:
        query = query.eq("email_status", "valid").eq("email_valid", True)
        count_query = count_query.eq("email_status", "valid").eq("email_valid", True)

    # ── dnc_status ─────────────────────────────────────────────────────────────
    blocked_csv = ",".join(_DNC_STATUS_BLOCKED)
    dnc_or = f"dnc_status.is.null,dnc_status.not.in.({blocked_csv})"
    query = query.or_(dnc_or)
    count_query = count_query.or_(dnc_or)

    if require_name_verified:
        query = query.eq("name_website_verified", "true")
        count_query = count_query.eq("name_website_verified", "true")

    if audited_after is not None:
        try:
            datetime.fromisoformat(audited_after.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="audited_after must be an ISO timestamp")
        query = query.gt("last_audited_at", audited_after)
        count_query = count_query.gt("last_audited_at", audited_after)

    if min_word_count is not None:
        query = query.gte("website_word_count", min_word_count)
        count_query = count_query.gte("website_word_count", min_word_count)
    if min_internal_pages is not None:
        query = query.gte("website_internal_page_count", min_internal_pages)
        count_query = count_query.gte("website_internal_page_count", min_internal_pages)
    if email_provider:
        providers = [p.strip() for p in email_provider.split(",") if p.strip()]
        if len(providers) == 1:
            query = query.eq("email_provider", providers[0])
            count_query = count_query.eq("email_provider", providers[0])
        elif len(providers) > 1:
            query = query.in_("email_provider", providers)
            count_query = count_query.in_("email_provider", providers)
    if email_trust:
        trusts = [t.strip() for t in email_trust.split(",") if t.strip()]
        if len(trusts) == 1:
            query = query.eq("email_trust", trusts[0])
            count_query = count_query.eq("email_trust", trusts[0])
        elif len(trusts) > 1:
            query = query.in_("email_trust", trusts)
            count_query = count_query.in_("email_trust", trusts)

    # ── cooldown_days: exclude leads contacted within N days ───────────────────
    if cooldown_days is not None and cooldown_days > 0:
        try:
            cd_cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
            cd_or = f"last_contacted_at.is.null,last_contacted_at.lt.{cd_cutoff}"
            query = query.or_(cd_or)
            count_query = count_query.or_(cd_or)
        except Exception as e:
            skipped_filters.append(f"cooldown_days_failed:{e}")

    # ── reattach-cooldown exclusion ────────────────────────────────────────────
    cutoff = _reattach_cutoff_iso(supabase)
    if cutoff:
        attached_or = f"last_campaign_attached_at.is.null,last_campaign_attached_at.lt.{cutoff}"
        query = query.or_(attached_or)
        count_query = count_query.or_(attached_or)

    # ── count ──────────────────────────────────────────────────────────────────
    try:
        count_result = count_query.execute()
        total = count_result.count if count_result.count is not None else 0
    except Exception as e:
        logger.error(f"eligible_for_campaign count failed: {e}")
        raise HTTPException(status_code=500, detail=f"count_query_failed: {e}")

    # ── fetch page ─────────────────────────────────────────────────────────────
    try:
        page = (
            query.order("pain_score", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        rows = page.data or []
    except Exception as e:
        # enrichment column might not exist yet — retry without it
        if "enrichment" in str(e):
            logger.warning("enrichment column missing, retrying without it")
            select_cols_fallback = select_cols.replace(", enrichment", "")
            try:
                page = (
                    supabase.table("leads").select(select_cols_fallback).eq("is_active", True)
                    .order("pain_score", desc=True)
                    .range(offset, offset + limit - 1)
                    .execute()
                )
                rows = page.data or []
                skipped_filters.append("enrichment_col_missing:apply_cez_keystone.sql")
            except Exception as e2:
                logger.error(f"eligible_for_campaign fetch failed: {e2}")
                raise HTTPException(status_code=500, detail=f"lead_query_failed: {e2}")
        else:
            logger.error(f"eligible_for_campaign fetch failed: {e}")
            raise HTTPException(status_code=500, detail=f"lead_query_failed: {e}")

    # ── dnc_suppressions (phone-based) ─────────────────────────────────────────
    if rows:
        from ..lib import phone_utils
        phone_norm_by_row: dict = {}
        unique_norms: set = set()
        for r in rows:
            np = phone_utils.normalize_phone(r.get("phone"))
            if np:
                phone_norm_by_row[r["id"]] = np
                unique_norms.add(np)

        suppressed: set = set()
        if unique_norms:
            try:
                sup_res = (
                    supabase.table("dnc_suppressions")
                    .select("phone")
                    .in_("phone", list(unique_norms))
                    .execute()
                )
                suppressed = {row["phone"] for row in (sup_res.data or []) if row.get("phone")}
            except Exception as e:
                logger.warning(f"eligible_for_campaign dnc_suppressions lookup failed: {e}")
                skipped_filters.append(f"dnc_suppressions_lookup_failed:{e}")

        if suppressed:
            rows = [r for r in rows if phone_norm_by_row.get(r["id"]) not in suppressed]

    # ── campaign_lead_attachments exclusion ────────────────────────────────────
    if rows:
        unique_emails = {r.get("email") for r in rows if r.get("email")}
        if unique_emails:
            try:
                cooldown_res = (
                    supabase.table("admin_settings")
                    .select("value")
                    .eq("key", "campaign_attach_cooldown_days")
                    .limit(1)
                    .execute()
                )
                cooldown_rows = cooldown_res.data or []
                cooldown_days_attach = int(cooldown_rows[0]["value"]) if cooldown_rows else 30
                attach_cutoff = (
                    datetime.now(timezone.utc) - timedelta(days=cooldown_days_attach)
                ).isoformat()

                cp_res = (
                    supabase.table("campaign_performance")
                    .select("reachinbox_campaign_id")
                    .in_("status", ["active", "paused"])
                    .execute()
                )
                active_ids = {
                    r["reachinbox_campaign_id"]
                    for r in (cp_res.data or [])
                    if r.get("reachinbox_campaign_id")
                }

                attached_emails: set = set()
                if active_ids:
                    cla_res = (
                        supabase.table("campaign_lead_attachments")
                        .select("email")
                        .in_("email", list(unique_emails))
                        .gte("attached_at", attach_cutoff)
                        .in_("reachinbox_campaign_id", list(active_ids))
                        .execute()
                    )
                    attached_emails = {
                        r["email"] for r in (cla_res.data or []) if r.get("email")
                    }

                if attached_emails:
                    rows = [r for r in rows if r.get("email") not in attached_emails]

            except Exception as e:
                logger.warning(f"eligible_for_campaign campaign_attach lookup failed: {e}")
                skipped_filters.append(f"campaign_attach_lookup_failed:{e}")

    # ── exclude_already_contacted: filter via lead_campaign_history ────────────
    if exclude_already_contacted and product and rows:
        try:
            row_ids = [r["id"] for r in rows]
            # Chunk to avoid URL-length limits
            already_contacted_ids: set = set()
            for i in range(0, len(row_ids), 500):
                chunk = row_ids[i:i + 500]
                lch_res = (
                    supabase.table("lead_campaign_history")
                    .select("lead_id")
                    .in_("lead_id", chunk)
                    .eq("product", product)
                    .execute()
                )
                for row in (lch_res.data or []):
                    already_contacted_ids.add(row["lead_id"])
            if already_contacted_ids:
                rows = [r for r in rows if r["id"] not in already_contacted_ids]
        except Exception as e:
            logger.warning(f"eligible_for_campaign exclude_already_contacted failed: {e}")
            skipped_filters.append(f"exclude_already_contacted_failed:{e}")

    leads_out = [
        {
            "id": r.get("id"),
            "business_name": r.get("business_name"),
            "email": r.get("email"),
            "phone": r.get("phone"),
            "niche": r.get("niche_slug"),
            "pain_score": r.get("pain_score"),
            "dnc_status": r.get("dnc_status"),
            "website": r.get("website"),
            "name_website_verified": r.get("name_website_verified"),
            "name_match_score": r.get("name_match_score"),
            "last_audited_at": r.get("last_audited_at"),
            "website_word_count": r.get("website_word_count"),
            "website_internal_page_count": r.get("website_internal_page_count"),
            "city": r.get("city"),
            "state": r.get("state"),
            "website_reachable": r.get("website_reachable"),
            "enrichment": r.get("enrichment") or {},
            "email_provider": r.get("email_provider"),
            "email_trust": r.get("email_trust"),
        }
        for r in rows
    ]

    return {
        "total": total,
        "count": len(leads_out),
        "leads": leads_out,
        "skipped_filters": skipped_filters,
        "segment_id": segment_id,
    }


# ─────────────────────────────────────────────────
# POST /kjle/v1/leads/enrichment/batch
# Legacy demo_url batch writer (backward compat).
# ─────────────────────────────────────────────────

@router.post("/leads/enrichment/batch")
async def batch_enrich_leads(payload: BatchEnrichmentRequest, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)

    if len(payload.updates) > 1000:
        raise HTTPException(status_code=400, detail="Max 1000 updates per batch request")

    supabase = get_supabase()
    updated = 0
    not_found: List[str] = []

    for item in payload.updates:
        try:
            res = supabase.table("leads").update({"demo_url": item.demo_url}).eq("id", item.lead_id).execute()
            if res.data:
                updated += 1
            else:
                not_found.append(item.lead_id)
        except Exception as e:
            logger.error(f"batch_enrich_leads update error lead_id={item.lead_id}: {e}")
            not_found.append(item.lead_id)

    return {"updated": updated, "not_found": not_found}


# ─────────────────────────────────────────────────
# POST /kjle/v1/leads/enrichment
# Generic jsonb-merge bulk enrichment writer.
# Merges arbitrary key:value pairs into leads.enrichment
# without clobbering existing keys.
# Body: {"updates": [{"lead_id": "...", "fields": {...}}]}
# ─────────────────────────────────────────────────

_ENRICH_CHUNK = 500


@router.post("/leads/enrichment")
async def bulk_enrich_leads(payload: EnrichmentBulkRequest, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)

    if not payload.updates:
        raise HTTPException(status_code=400, detail="updates list is empty")
    if len(payload.updates) > 1000:
        raise HTTPException(status_code=400, detail="Max 1000 updates per call")

    supabase = get_supabase()
    results = []
    updated = 0
    failed = []

    # Process per-lead (each is an independent jsonb merge)
    for item in payload.updates:
        if not item.fields:
            results.append({"lead_id": item.lead_id, "ok": False, "error": "fields is empty"})
            failed.append(item.lead_id)
            continue
        try:
            # Read current enrichment, merge, write back
            cur = supabase.table("leads").select("id, enrichment").eq("id", item.lead_id).execute()
            if not cur.data:
                results.append({"lead_id": item.lead_id, "ok": False, "error": "not_found"})
                failed.append(item.lead_id)
                continue
            existing = cur.data[0].get("enrichment") or {}
            merged = {**existing, **item.fields}
            supabase.table("leads").update({"enrichment": merged}).eq("id", item.lead_id).execute()
            results.append({"lead_id": item.lead_id, "ok": True})
            updated += 1
        except Exception as e:
            logger.error(f"bulk_enrich_leads lead_id={item.lead_id}: {e}")
            results.append({"lead_id": item.lead_id, "ok": False, "error": str(e)})
            failed.append(item.lead_id)

    return {
        "updated": updated,
        "failed": failed,
        "results": results,
    }


# ─────────────────────────────────────────────────
# GET /kjle/v1/leads/provider-breakdown
# Returns lead counts by email_provider bucket (9 fixed buckets).
# ─────────────────────────────────────────────────

@router.get("/leads/provider-breakdown")
async def provider_breakdown(
    niche_slug: Optional[str] = Query(None),
    segment_label: Optional[str] = Query(None),
    email_status: Optional[str] = Query(None),
    x_api_key: str = Header(...),
):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    # RPC-only: server-side GROUP BY. No client-side fallback (it would paginate 1.25M rows and wedge the API).
    raw_counts: dict = {}
    try:
        rpc_params = {
            "p_niche_slug": niche_slug,
            "p_segment_label": segment_label,
            "p_email_status": email_status,
        }
        rpc_res = supabase.rpc("get_provider_breakdown", rpc_params).execute()
        for row in (rpc_res.data or []):
            raw_counts[row["provider"]] = row["lead_count"]
    except Exception as e:
        logger.error(f"provider_breakdown RPC failed: {e}")
        raise HTTPException(status_code=503, detail="provider-breakdown temporarily unavailable")

    # Normalize to the 9 canonical buckets (0 for absent)
    breakdown = {bucket: raw_counts.get(bucket, 0) for bucket in _PROVIDER_BUCKETS}
    # Any unexpected bucket values merge into "other"
    for bucket, count in raw_counts.items():
        if bucket not in breakdown:
            breakdown["other"] = breakdown.get("other", 0) + count

    return {
        "breakdown": breakdown,
        "total": sum(breakdown.values()),
        "filters": {
            "niche_slug": niche_slug,
            "segment_label": segment_label,
            "email_status": email_status,
        },
    }


# ─────────────────────────────────────────────────
# POST /kjle/v1/leads/mark-contacted
# Records a contact event in lead_campaign_history and
# increments leads.contact_count + sets last_contacted_at.
# Accepts single-item or batch (updates list).
# ─────────────────────────────────────────────────

@router.post("/leads/mark-contacted")
async def mark_contacted(payload: MarkContactedRequest, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    # Normalize single-item or batch
    items: List[MarkContactedItem] = []
    if payload.updates:
        items = payload.updates
    elif payload.lead_id and payload.campaign_id and payload.product:
        items = [MarkContactedItem(
            lead_id=payload.lead_id,
            campaign_id=payload.campaign_id,
            product=payload.product,
            sequence_step=payload.sequence_step,
            status=payload.status,
            contacted_at=payload.contacted_at,
            metadata=payload.metadata,
        )]
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either (lead_id + campaign_id + product) or updates list"
        )

    if len(items) > 500:
        raise HTTPException(status_code=400, detail="Max 500 items per call")

    recorded = 0
    skipped = 0
    errors = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for item in items:
        ts = item.contacted_at or now_iso
        try:
            # Insert history row (ON CONFLICT DO NOTHING via unique constraint)
            hist_row = {
                "lead_id": item.lead_id,
                "campaign_id": item.campaign_id,
                "product": item.product,
                "sequence_step": item.sequence_step,
                "status": item.status,
                "contacted_at": ts,
            }
            if item.metadata:
                hist_row["metadata"] = item.metadata

            ins_res = (
                supabase.table("lead_campaign_history")
                .upsert(hist_row, on_conflict="lead_id,campaign_id,sequence_step", ignore_duplicates=True)
                .execute()
            )
            inserted = bool(ins_res.data)

            if inserted:
                # Increment contact_count and set last_contacted_at
                # Read current count first (Supabase doesn't support atomic increment via REST)
                cur = (
                    supabase.table("leads")
                    .select("contact_count")
                    .eq("id", item.lead_id)
                    .execute()
                )
                cur_count = 0
                if cur.data:
                    cur_count = cur.data[0].get("contact_count") or 0
                supabase.table("leads").update({
                    "last_contacted_at": ts,
                    "contact_count": cur_count + 1,
                }).eq("id", item.lead_id).execute()
                recorded += 1
            else:
                skipped += 1

        except Exception as e:
            logger.error(f"mark_contacted lead_id={item.lead_id}: {e}")
            errors.append({"lead_id": item.lead_id, "error": str(e)})

    return {
        "recorded": recorded,
        "skipped": skipped,
        "errors": errors,
    }


# ─────────────────────────────────────────────────
# GET /kjle/v1/leads/{lead_id}
# ─────────────────────────────────────────────────

@router.get("/leads/{lead_id}")
async def get_lead(lead_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        result = supabase.table("leads").select("*").eq("id", lead_id).single().execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Lead not found")
        return result.data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"get_lead error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/leads/{lead_id}/reclassify
# ─────────────────────────────────────────────────

@router.post("/leads/{lead_id}/reclassify")
async def reclassify_lead(lead_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        lead_res = supabase.table("leads").select("id, pain_score").eq("id", lead_id).single().execute()
        if not lead_res.data:
            raise HTTPException(status_code=404, detail="Lead not found")

        new_label = score_to_segment(lead_res.data.get("pain_score"))
        now = datetime.now(timezone.utc).isoformat()

        supabase.table("leads").update({
            "segment_label": new_label,
            "segmented_at": now,
        }).eq("id", lead_id).execute()

        return {"lead_id": lead_id, "segment_label": new_label, "segmented_at": now}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"reclassify_lead error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/leads/{lead_id}/dnc
# ─────────────────────────────────────────────────

@router.post("/leads/{lead_id}/dnc")
async def mark_dnc(lead_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        lead_res = supabase.table("leads").select("id").eq("id", lead_id).single().execute()
        if not lead_res.data:
            raise HTTPException(status_code=404, detail="Lead not found")

        supabase.table("leads").update({"do_not_contact": True}).eq("id", lead_id).execute()
        return {"success": True, "lead_id": lead_id}

    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        if "do_not_contact" in error_msg or "column" in error_msg.lower():
            raise HTTPException(
                status_code=500,
                detail="do_not_contact column does not exist. Run: ALTER TABLE leads ADD COLUMN IF NOT EXISTS do_not_contact BOOLEAN DEFAULT FALSE;"
            )
        logger.error(f"mark_dnc error: {e}")
        raise HTTPException(status_code=500, detail=error_msg)


# ─────────────────────────────────────────────────
# POST /kjle/v1/leads/{lead_id}/enrichment
# Legacy per-lead demo_url writer (backward compat).
# ─────────────────────────────────────────────────

@router.post("/leads/{lead_id}/enrichment")
async def enrich_lead(lead_id: str, payload: EnrichmentUpdate, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        check = supabase.table("leads").select("id").eq("id", lead_id).execute()
        if not (check.data):
            raise HTTPException(status_code=404, detail="Lead not found")

        supabase.table("leads").update({"demo_url": payload.demo_url}).eq("id", lead_id).execute()
        return {"lead_id": lead_id, "demo_url": payload.demo_url, "updated": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"enrich_lead error lead_id={lead_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# DELETE /kjle/v1/leads/{lead_id}
# ─────────────────────────────────────────────────

@router.delete("/leads/{lead_id}")
async def delete_lead(lead_id: str, x_api_key: str = Header(...)):
    verify_api_key(x_api_key)
    supabase = get_supabase()

    try:
        lead_res = supabase.table("leads").select("id").eq("id", lead_id).single().execute()
        if not lead_res.data:
            raise HTTPException(status_code=404, detail="Lead not found")

        supabase.table("leads").delete().eq("id", lead_id).execute()
        return {"deleted": True, "lead_id": lead_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete_lead error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
