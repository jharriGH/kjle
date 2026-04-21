"""
KJLE — Campaign Activity Helper (parallel-prompt integration shim)
File: api/lib/campaign_activity.py

This module owns the interface between the daily cost report and the
`campaign_performance` table being built in a parallel Claude Code session.

CONTRACT (pre-approved, stable):
    get_kjle_campaign_activity_summary(since_utc: datetime) -> list[dict]

Returns a list of per-campaign rollup dicts shaped like:
    {
        "campaign_id":   str,
        "campaign_name": str,
        "sent":          int,
        "opened":        int,
        "replied":       int,
        "booked":        int,
    }

If `campaign_performance` table does not exist yet (parallel session hasn't
landed its migration), this helper returns []. Daily email renders
"(no campaign data yet)" in that case. Neither session blocks the other.

The parallel prompt author may replace the body of this function to read
from `campaign_performance` once their migration + tracking wires are live.
The function signature is load-bearing — do not change it.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Dict

from ..database import get_db

logger = logging.getLogger(__name__)


def get_kjle_campaign_activity_summary(since_utc: datetime) -> List[Dict]:
    """
    Return per-campaign activity rollup since `since_utc` for project='kjle'.

    Implementation reads from the `campaign_performance` table (project-aware,
    multi-server). Schema column names differ from the contract shape — this
    function maps them. The function signature + return shape are load-bearing
    for daily_report.py — do not change.

    Column mapping (campaign_performance → contract):
        reachinbox_campaign_id → campaign_id
        campaign_name          → campaign_name
        sent_count             → sent
        opened_count           → opened
        replied_count          → replied
        demo_booked_count      → booked

    Scope: project='kjle' only (telehealth/other projects excluded from KJE digest).
    Activity filter: updated_at >= since_utc AND sent_count > 0.
    Returns [] gracefully on any DB/table error.
    """
    try:
        db = get_db()
        res = (
            db.table("campaign_performance")
            .select(
                "reachinbox_campaign_id, campaign_name, "
                "sent_count, opened_count, replied_count, demo_booked_count"
            )
            .eq("project", "kjle")
            .gte("updated_at", since_utc.isoformat())
            .gt("sent_count", 0)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        # Table missing, schema drift, or DB hiccup — degrade gracefully.
        logger.info(f"campaign_activity: summary unavailable ({e}); returning empty list")
        return []

    out: List[Dict] = []
    for r in rows:
        cid = r.get("reachinbox_campaign_id") or ""
        out.append({
            "campaign_id":   cid,
            "campaign_name": r.get("campaign_name") or cid or "(unnamed)",
            "sent":          int(r.get("sent_count")        or 0),
            "opened":        int(r.get("opened_count")      or 0),
            "replied":       int(r.get("replied_count")     or 0),
            "booked":        int(r.get("demo_booked_count") or 0),
        })
    return sorted(out, key=lambda x: x["sent"], reverse=True)
