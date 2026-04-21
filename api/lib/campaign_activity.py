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
    Return per-campaign activity rollup since `since_utc`.
    Returns [] gracefully if the campaign_performance table does not exist
    or any query error occurs — callers render "(no campaign data yet)".
    """
    try:
        db = get_db()
        res = (
            db.table("campaign_performance")
            .select("campaign_id, campaign_name, sent, opened, replied, booked")
            .gte("recorded_at", since_utc.isoformat())
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        # Table missing, schema drift, or DB hiccup — degrade gracefully.
        logger.info(f"campaign_activity: summary unavailable ({e}); returning empty list")
        return []

    # Aggregate by campaign_id across the window (multiple snapshots may exist).
    bucket: Dict[str, Dict] = {}
    for r in rows:
        cid = r.get("campaign_id")
        if not cid:
            continue
        b = bucket.setdefault(cid, {
            "campaign_id":   cid,
            "campaign_name": r.get("campaign_name") or cid,
            "sent":   0,
            "opened": 0,
            "replied": 0,
            "booked": 0,
        })
        for k in ("sent", "opened", "replied", "booked"):
            v = r.get(k)
            if isinstance(v, (int, float)):
                b[k] += int(v)

    return sorted(bucket.values(), key=lambda x: x["sent"], reverse=True)
