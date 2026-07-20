"""
KJLE — Campaign Prep: ADA/Accessibility Cold Outreach Segment Engine
POST /kjle/v1/campaign-prep

FRESHNESS APPROACH: enqueue-and-exclude
  Stale leads (accessibility_scanned_at older than reverify_stale_days, or null)
  are enqueued in scan_jobs at priority=9 (high / on-demand) and EXCLUDED from
  this run. They appear in the "requeued_stale" list in the response. Re-run
  this endpoint after the scan daemon catches up to include them.
  NOTE: reverify.py Mode 3 re-verifies website-audit (HTML) signals, NOT the axe
  accessibility scan. scan_jobs priority=9 triggers the daemon's axe scan path.

PURL SEAM
  attributes.purlLink = CE_PURL_BASE (env var, default https://scan.compliancemds.com/?d=)
  + the lead's website domain. Uses "?d=" param (ComplianceMDs landing-page prefill). No ce_ tables are queried. To replace with a
  pre-baked slug, find the comment "# PURL SEAM: replace" in _purl_link() and
  swap in a GET /ce/v1/purl/by-lead/{lead_id} call when ComplianceMDs PURL API
  is integrated.

topIssues: top-3 plain-English violation labels, severity-ranked (critical first).
  Batch-fetched from scan_results in chunks of 200 lead_ids; Python-side DISTINCT ON
  (latest row per lead_id) avoids N+1. Leads with no scan detail get topIssues="".
  violationCount, criticalCount, and accessibilityScore are populated from the
  leads table directly (no extra round-trips).

DNC/contactable guards: replicates fetch_kjle_leads (reachinbox.py):
  is_active=True, email_valid=True, email IS NOT NULL, plus email_suppressions
  table scrub. Suppressed addresses are never included.

Protected imports (read-only, never modified):
  - reachinbox.map_lead_to_ri  — maps lead dict to RI payload shape
  - reachinbox.ri_post         — RI HTTP POST (only called when dry_run=False)
"""

import csv
import io
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from ..database import get_db
from .reachinbox import map_lead_to_ri, ri_post

logger = logging.getLogger(__name__)
router = APIRouter()

API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")

# PURL SEAM: swap CE_PURL_BASE + domain for a pre-baked slug when ComplianceMDs
# PURL API is ready. See _purl_link() below for the exact replacement point.
CE_PURL_BASE = os.environ.get("CE_PURL_BASE", "https://scan.compliancemds.com/?d=")

_ADA_COLS = (
    "id, business_name, email, phone, city, state, niche_slug, pain_score, "
    "website, accessibility_score, accessibility_violations, accessibility_critical, "
    "accessibility_scanned_at"
)
_HARD_CAP = 2000
_SUPP_CHUNK = 100   # email_suppressions batch size
_SCAN_CHUNK = 200   # scan_results IN-list chunk (avoids PostgREST URL-length blow-up)

# axe impact severity order (critical=0 is highest priority)
_IMPACT_ORDER = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}

AXE_LABEL_MAP = {
    "image-alt":            "Images missing alt text",
    "color-contrast":       "Insufficient color contrast",
    "label":                "Form fields without labels",
    "link-name":            "Links without descriptive text",
    "button-name":          "Buttons without labels",
    "html-has-lang":        "Missing page language setting",
    "document-title":       "Missing or empty page title",
    "landmark-one-main":    "Missing main content landmark",
    "page-has-heading-one": "Missing main heading (H1)",
    "frame-title":          "Frames without titles",
    "aria-required-attr":   "Incomplete ARIA attributes",
    "duplicate-id":         "Duplicate element IDs",
    "list":                 "Improperly structured lists",
    "heading-order":        "Headings out of order",
    "region":               "Content not in landmarks",
}


def _humanize_axe_id(axe_id: str) -> str:
    return axe_id.replace("-", " ").title()


def _top3_from_violations(violations_raw) -> list:
    """Parse a scan_results.violations JSONB value and return top-3 plain-English labels."""
    if not violations_raw:
        return []
    if isinstance(violations_raw, str):
        try:
            violations_raw = json.loads(violations_raw)
        except Exception:
            return []
    if not isinstance(violations_raw, list):
        return []

    sorted_viols = sorted(
        violations_raw,
        key=lambda v: _IMPACT_ORDER.get((v.get("impact") or "minor"), 3),
    )
    labels = []
    seen: set = set()
    for v in sorted_viols:
        axe_id = (v.get("id") or "").strip()
        if not axe_id or axe_id in seen:
            continue
        labels.append(AXE_LABEL_MAP.get(axe_id) or _humanize_axe_id(axe_id))
        seen.add(axe_id)
        if len(labels) == 3:
            break
    return labels


def _fetch_top_issues(db, lead_ids: list) -> tuple:
    """
    Batch-fetch top-3 violation labels per lead from scan_results in _SCAN_CHUNK-sized
    IN queries. Python-side DISTINCT ON (max scanned_at per lead_id) avoids N+1.
    Returns ({lead_id: "label1, label2, label3"}, no_detail_count).
    """
    if not lead_ids:
        return {}, 0

    latest_per_lead: dict = {}  # lead_id -> latest scan_results row dict

    for i in range(0, len(lead_ids), _SCAN_CHUNK):
        chunk = lead_ids[i:i + _SCAN_CHUNK]
        try:
            rows = (
                db.table("scan_results")
                .select("lead_id, violations, scanned_at")
                .in_("lead_id", chunk)
                .execute().data or []
            )
        except Exception as e:
            logger.warning(f"[campaign_prep] scan_results fetch failed chunk={i}: {e}")
            continue

        for row in rows:
            lid = row.get("lead_id")
            if lid is None:
                continue
            sat = row.get("scanned_at") or ""
            if lid not in latest_per_lead or sat > (latest_per_lead[lid].get("scanned_at") or ""):
                latest_per_lead[lid] = row

    result: dict = {}
    no_detail_count = 0
    for lid in lead_ids:
        row = latest_per_lead.get(lid)
        top3 = _top3_from_violations(row.get("violations") if row else None)
        if top3:
            result[lid] = ", ".join(top3)
        else:
            result[lid] = ""
            no_detail_count += 1

    return result, no_detail_count


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


class CampaignPrepRequest(BaseModel):
    max_score: float = 30.0
    min_violations: Optional[int] = None
    niche_slug: Optional[str] = None
    state: Optional[str] = None
    limit: int = 500
    reverify_stale_days: int = 7
    output: Literal["csv", "reachinbox_payload"] = "csv"
    dry_run: bool = True
    campaign_id: Optional[int] = None  # required for output=reachinbox_payload + dry_run=False


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_domain(website: str) -> str:
    """Strip scheme + path from website URL, leaving bare domain."""
    if not website:
        return ""
    return re.sub(r'^https?://', '', website.strip()).split('/')[0]


def _purl_link(website: str) -> str:
    # PURL SEAM: replace this line with a GET /ce/v1/purl/by-lead/{lead_id}
    # lookup when ComplianceMDs PURL API is integrated. No ce_ tables are queried.
    return (CE_PURL_BASE + _extract_domain(website)) if website else ""


def _is_stale(scanned_at: Optional[str], cutoff_dt: datetime) -> bool:
    """Returns True when scanned_at is older than cutoff_dt or is None/unparseable."""
    if not scanned_at:
        return True
    try:
        dt = datetime.fromisoformat(scanned_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < cutoff_dt
    except (ValueError, AttributeError):
        return True


def _scrub_suppressions(db, rows: list) -> tuple:
    """Remove email-suppressed leads (mirrors fetch_kjle_leads). Returns (clean, n_excluded)."""
    if not rows:
        return rows, 0
    emails = sorted({(r.get("email") or "").strip().lower() for r in rows if r.get("email")})
    suppressed: set = set()
    for i in range(0, len(emails), _SUPP_CHUNK):
        chunk = emails[i:i + _SUPP_CHUNK]
        try:
            sup = (
                db.table("email_suppressions")
                .select("email")
                .in_("email", chunk)
                .execute().data or []
            )
            suppressed.update((s.get("email") or "").lower() for s in sup)
        except Exception as e:
            logger.warning(f"[campaign_prep] email_suppressions scrub failed: {e}")
    before = len(rows)
    clean = [r for r in rows if (r.get("email") or "").strip().lower() not in suppressed]
    return clean, before - len(clean)


def _enrich_attrs(lead: dict, ri_payload: dict, top_issues_map: dict) -> dict:
    """Add ADA merge-tag attributes to a map_lead_to_ri payload (mutates attrs in-place)."""
    attrs = ri_payload.setdefault("attributes", {})
    attrs["violationCount"]     = lead.get("accessibility_violations") or 0
    attrs["criticalCount"]      = lead.get("accessibility_critical") or 0
    attrs["accessibilityScore"] = round(float(lead.get("accessibility_score") or 0.0), 1)
    attrs["topIssues"]          = top_issues_map.get(lead["id"]) or ""
    attrs["purlLink"]           = _purl_link(lead.get("website") or "")
    return ri_payload


# ─────────────────────────────────────────────────────────────────────────────
# POST /campaign-prep
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/campaign-prep", dependencies=[Depends(verify_api_key)])
async def campaign_prep(body: CampaignPrepRequest):
    """
    ADA Campaign Prep — freshness-gated accessibility segment for cold outreach.

    Pulls hot ADA leads (accessibility_score < max_score), freshness-gates them
    (enqueue stale for re-scan and exclude), enriches with merge tags, and returns
    CSV data or ReachInbox payloads + stats.
    """
    if body.output == "reachinbox_payload" and not body.dry_run and not body.campaign_id:
        raise HTTPException(
            status_code=400,
            detail="campaign_id is required when output=reachinbox_payload and dry_run=false",
        )

    limit = min(body.limit, _HARD_CAP)
    db = get_db()
    now_dt = datetime.now(timezone.utc)
    stale_cutoff_dt = now_dt - timedelta(days=body.reverify_stale_days)

    # ── Step 1: Fetch ADA segment ────────────────────────────────────────────
    # Applies contactable guards matching fetch_kjle_leads: is_active, email_valid,
    # email IS NOT NULL. Plus ADA-specific: website non-null/non-empty, score set,
    # score < max_score. email_suppressions DNC scrub applied below.
    query = (
        db.table("leads")
        .select(_ADA_COLS)
        .eq("is_active", True)
        .eq("email_valid", True)
        .not_.is_("email", "null")
        .not_.is_("website", "null")
        .neq("website", "")
        .not_.is_("accessibility_score", "null")
        .lt("accessibility_score", body.max_score)
    )
    if body.min_violations is not None:
        query = query.gte("accessibility_violations", body.min_violations)
    if body.niche_slug:
        query = query.eq("niche_slug", body.niche_slug)
    if body.state:
        query = query.eq("state", body.state.upper())

    rows = (
        query
        .order("accessibility_score", desc=False)
        .order("id")
        .limit(limit)
        .execute().data or []
    )
    total_matched = len(rows)

    rows, dnc_excluded = _scrub_suppressions(db, rows)

    # ── Step 2: Freshness gate ────────────────────────────────────────────────
    # Partition into FRESH and STALE. Stale leads are enqueued at priority=9
    # (high / on-demand axe scan) and EXCLUDED from this run's output.
    fresh_leads = []
    stale_leads = []
    for r in rows:
        if _is_stale(r.get("accessibility_scanned_at"), stale_cutoff_dt):
            stale_leads.append(r)
        else:
            fresh_leads.append(r)

    requeued_stale: list = []
    enqueue_failed: list = []
    now_iso = now_dt.isoformat()
    for lead in stale_leads:
        website = (lead.get("website") or "").strip()
        if not website:
            continue
        try:
            db.table("scan_jobs").insert({
                "url":         website,
                "lead_id":     lead["id"],
                "priority":    9,
                "status":      "queued",
                "enqueued_at": now_iso,
            }).execute()
            requeued_stale.append(str(lead["id"]))
        except Exception as e:
            logger.warning(f"[campaign_prep] scan_jobs enqueue failed lead={lead['id']}: {e}")
            enqueue_failed.append(str(lead["id"]))

    # ── Step 3: Drop remediated (defensive for fresh leads) ──────────────────
    # Fresh leads were already filtered accessibility_score < max_score at DB
    # level, so this should be a no-op. Kept as a correctness guard.
    surviving = []
    dropped_remediated = 0
    for lead in fresh_leads:
        if (lead.get("accessibility_score") or 0.0) >= body.max_score:
            dropped_remediated += 1
            logger.info(
                f"[campaign_prep] dropped remediated lead={lead['id']} "
                f"score={lead.get('accessibility_score')}"
            )
        else:
            surviving.append(lead)

    final_count = len(surviving)

    # ── Step 3.5: Batch-fetch top-3 violation labels from scan_results ────────
    # ONE chunked query (chunks of _SCAN_CHUNK), Python-side DISTINCT ON per lead.
    surviving_ids = [lead["id"] for lead in surviving]
    top_issues_map, no_detail_count = _fetch_top_issues(db, surviving_ids)

    # ── Steps 4 + 5: Enrich for merge tags (PURL via CE_PURL_BASE) ──────────
    enriched: list = []
    for lead in surviving:
        ri_payload = map_lead_to_ri(lead)  # protected import — not reimplemented
        ri_payload = _enrich_attrs(lead, ri_payload, top_issues_map)
        enriched.append((lead, ri_payload))

    # Stats block — always returned regardless of output mode
    stats = {
        "total_matched":      total_matched,
        "fresh":              len(fresh_leads),
        "stale_reverified":   0,           # enqueue-and-exclude: no inline reverify
        "requeued_stale":     len(requeued_stale),
        "dropped_remediated": dropped_remediated,
        "deferred":           len(enqueue_failed),
        "final_count":        final_count,
        "dnc_excluded":       dnc_excluded,
        "no_detail_count":    no_detail_count,
    }

    # ── Step 6: Output ────────────────────────────────────────────────────────

    # CSV output: return JSON with embedded csv_data string + stats so the
    # operator can inspect stats and save the CSV without a separate request.
    if body.output == "csv":
        csv_cols = [
            "lead_id", "companyName", "email", "website",
            "accessibility_score", "accessibility_violations", "accessibility_critical",
            "topIssues", "purlLink",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=csv_cols, lineterminator="\n")
        writer.writeheader()
        for lead, ri in enriched:
            attrs = ri.get("attributes", {})
            writer.writerow({
                "lead_id":                  lead["id"],
                "companyName":              attrs.get("companyName") or "",
                "email":                    lead.get("email") or "",
                "website":                  lead.get("website") or "",
                "accessibility_score":      attrs.get("accessibilityScore") or "",
                "accessibility_violations": attrs.get("violationCount") or "",
                "accessibility_critical":   attrs.get("criticalCount") or "",
                "topIssues":                attrs.get("topIssues") or "",
                "purlLink":                 attrs.get("purlLink") or "",
            })
        buf.seek(0)
        csv_str = buf.getvalue()
        filename = f"kjle_ada_{now_dt.strftime('%Y%m%d_%H%M%S')}.csv"
        return {
            "status":      "success",
            "dry_run":     body.dry_run,
            "stats":       stats,
            "csv_columns": csv_cols,
            "csv_data":    csv_str,
            "filename":    filename,
        }

    # reachinbox_payload output
    payload_list = [ri for _, ri in enriched]

    if not body.dry_run and body.campaign_id:
        leads_pushed = 0
        leads_failed = 0
        for i in range(0, len(payload_list), 100):
            batch = payload_list[i:i + 100]
            try:
                await ri_post("/leads/add", {"campaignId": body.campaign_id, "leads": batch})
                leads_pushed += len(batch)
            except Exception as e:
                leads_failed += len(batch)
                logger.warning(f"[campaign_prep] RI push batch {i} failed: {e}")
        return {
            "status":       "success",
            "dry_run":      False,
            "campaign_id":  body.campaign_id,
            "leads_pushed": leads_pushed,
            "leads_failed": leads_failed,
            "stats":        stats,
            "payloads":     payload_list,
        }

    return {
        "status":   "success",
        "dry_run":  True,
        "stats":    stats,
        "payloads": payload_list,
    }
