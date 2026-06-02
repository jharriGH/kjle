"""
KJLE — Backlog contactability scrub (Phase 4 Layer 5B Stage 1: test-fire)
File: api/routes/backlog_scrub.py

Routes (PREFIX /kjle/v1 added by main.py):
  POST /backlog/contactability/test-fire — x-api-key auth; small-sample
                                            local-only classifier sweep.

PURPOSE
-------
Apply the Slice 3B PURE-FUNCTION classifiers
  - api/lib/lead_filters.py::classify_lead_quality
  - api/lib/phone_filters.py::classify_phone_quality
to a SMALL deterministic sample of backlog leads (default 500). Read +
classify + optional UPDATE. NO external API calls — no Searchbug,
no Outscraper, no Firecrawl, no Truelist, no DNC, no TCPA, no carrier
lookups, no anything. Pure-function classify only.

dry_run=true (the default) is read-only: collects aggregates + sample
flagged leads and returns them without writing anything.

dry_run=false stamps:
    contactable          (boolean)
    filter_reasons       (jsonb array of reason tokens)
    filter_classified_at (timestamptz now())
    filter_classified_by (text label from request body)
on each scrubbed row.

Sample selection is DETERMINISTIC (ORDER BY id LIMIT sample_size) so the
test fire is repeatable / inspectable — never ORDER BY random(). The
selector excludes rows where filter_classified_at IS NOT NULL so each
invocation keeps moving forward over the backlog instead of re-scrubbing
the same head of the table.

Phase 4 Layer 5B Stage 1.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from ..database import get_db
from ..lib.lead_filters import classify_lead_quality
from ..lib.phone_filters import classify_phone_quality

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Auth ─────────────────────────────────────────────────────────────────────
API_SECRET_KEY = os.environ.get("API_SECRET_KEY")


def verify_api_key(x_api_key: str = Header(...)) -> None:
    if not API_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="server_misconfigured: API_SECRET_KEY env var unset",
        )
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Request / response models ────────────────────────────────────────────────

class TestFireRequest(BaseModel):
    sample_size: int = Field(default=500, ge=1, le=5000)
    dry_run: bool = Field(default=True)
    label: str = Field(default="stage1_test")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _classify_one(lead: dict) -> dict:
    """
    Run BOTH classifiers on a single lead dict. Returns:
        {
          "contactable": bool,
          "reasons":     [str],   # combined lead + phone reasons
          "lead_reasons":  [str],
          "phone_reasons": [str],
        }

    Pure function — no IO, no exceptions raised by callee path. (The
    classifiers themselves are exception-safe on bad input.)
    """
    quality = classify_lead_quality(lead)
    phone = lead.get("phone")
    phone_quality = classify_phone_quality(phone) if phone else None

    contactable = bool(quality.get("contactable", True)) and (
        bool(phone_quality.get("contactable", True))
        if phone_quality else True
    )
    lead_reasons = list(quality.get("reasons") or [])
    phone_reasons = (
        list(phone_quality.get("reasons") or []) if phone_quality else []
    )
    return {
        "contactable":   contactable,
        "reasons":       lead_reasons + phone_reasons,
        "lead_reasons":  lead_reasons,
        "phone_reasons": phone_reasons,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /backlog/contactability/test-fire
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/backlog/contactability/test-fire")
async def backlog_contactability_test_fire(
    req: TestFireRequest,
    x_api_key: str = Header(...),
) -> dict:
    """
    Phase 4 Layer 5B Stage 1 — TEST FIRE.

    Apply local-only contactability classifiers to a small deterministic
    sample of backlog leads. ZERO external API calls.

    Body:
        {
          "sample_size": 500,           # 1..5000
          "dry_run":     true,          # default true (read-only)
          "label":       "stage1_test_2026_06_01"
        }

    Selector (deterministic — repeatable):
        is_active = true
        AND filter_classified_at IS NULL
        AND (phone IS NOT NULL OR business_name IS NOT NULL)
        ORDER BY id LIMIT sample_size

    Per-lead processing:
        a. classify_lead_quality(lead)              → lead reasons
        b. classify_phone_quality(phone) if phone   → phone reasons
        c. contactable = lead.contactable AND phone.contactable (if phone)
        d. reasons     = lead_reasons + phone_reasons
        e. dry_run=true  → in-memory aggregate only
        f. dry_run=false → UPDATE contactable, filter_reasons,
                                 filter_classified_at, filter_classified_by

    Response:
        {
          "dry_run":                          bool,
          "label":                            str,
          "sample_size_requested":            int,
          "total_processed":                  int,
          "newly_marked_uncontactable":       int,
          "already_contactable_remains_so":   int,
          "filter_reason_distribution":       {reason: count, ...},
          "samples":                          [ {id, business_name, phone, reasons}, ... ],
          "writes_attempted":                 int,
          "writes_failed":                    int,
          "warnings":                         [str]
        }

    HTTP 401 on bad x-api-key.
    HTTP 500 on DB read failure or if the migration is missing.
    """
    verify_api_key(x_api_key)

    # Pydantic Field(ge=1, le=5000) already validates range. Belt-and-braces:
    if req.sample_size < 1 or req.sample_size > 5000:
        raise HTTPException(
            status_code=400,
            detail="sample_size must be between 1 and 5000",
        )

    db = get_db()
    warnings: list[str] = []

    # ── Selector: deterministic head-of-backlog slice ────────────────────────
    try:
        sel = (
            db.table("leads")
              .select(
                 "id,business_name,phone,address,city,state,"
                  "contactable,filter_reasons,filter_classified_at"
              )
              .eq("is_active", True)
              .is_("filter_classified_at", "null")
              .order("id")
              .limit(req.sample_size)
              .execute()
        )
        rows = sel.data or []
    except Exception as e:
        # If filter_classified_at column is missing the migration hasn't been
        # applied — fail loudly so Jim knows to run it.
        msg = str(e)
        if "filter_classified_at" in msg or "column" in msg.lower():
            raise HTTPException(
                status_code=500,
                detail=(
                    "leads.filter_classified_at column missing — apply "
                    "migrations/leads_filter_classified_tracking.sql first"
                ),
            )
        raise HTTPException(
            status_code=500, detail=f"selector_failed: {msg[:200]}"
        )

    # ── Hot path: classify + (optional) write ────────────────────────────────
    # Note: ALSO filter out rows where both phone AND business_name are
    # actually empty — Supabase's "IS NOT NULL" on text columns won't catch
    # whitespace-only / "" cases, and a row with neither contact path is
    # nothing the classifier can act on anyway.
    classified_at = _now_iso()
    label = (req.label or "stage1_test").strip() or "stage1_test"

    total_processed = 0
    newly_marked_uncontactable = 0
    already_contactable_remains_so = 0
    reason_dist: dict[str, int] = {}
    samples: list[dict] = []
    writes_attempted = 0
    writes_failed = 0

    for row in rows:
        try:
            phone = row.get("phone")
            business_name = row.get("business_name")
            if not phone and not (business_name and str(business_name).strip()):
                # Nothing for the classifiers to grip on — skip.
                continue

            total_processed += 1

            result = _classify_one(row)
            contactable = result["contactable"]
            reasons = result["reasons"]

            for r in reasons:
                reason_dist[r] = reason_dist.get(r, 0) + 1

            was_contactable = row.get("contactable")
            if was_contactable is None:
                was_contactable = True  # column default

            if not contactable:
                # Only count as NEWLY uncontactable if the row was previously
                # marked contactable=true (or had no value, defaulting to true).
                if bool(was_contactable):
                    newly_marked_uncontactable += 1
                if len(samples) < 10:
                    bn = (business_name or "")
                    samples.append({
                        "id": row.get("id"),
                        "business_name": (bn[:80] + "…") if len(bn) > 80 else bn,
                        "phone": phone,
                        "reasons": reasons,
                    })
            else:
                already_contactable_remains_so += 1

            if not req.dry_run:
                writes_attempted += 1
                try:
                    db.table("leads").update({
                        "contactable":          contactable,
                        "filter_reasons":       reasons,
                        "filter_classified_at": classified_at,
                        "filter_classified_by": label,
                    }).eq("id", row.get("id")).execute()
                except Exception as ew:
                    writes_failed += 1
                    logger.warning(
                        "backlog_scrub: per-row update failed id=%s: %s",
                        row.get("id"), ew,
                    )

        except Exception as e:
            # One bad row never kills the batch.
            warnings.append(
                f"row_classification_failed id={row.get('id')}: "
                f"{type(e).__name__}: {str(e)[:120]}"
            )
            logger.warning(
                "backlog_scrub: per-row classification failed: %s", e
            )

    response = {
        "dry_run":                        req.dry_run,
        "label":                          label,
        "sample_size_requested":          req.sample_size,
        "rows_selected":                  len(rows),
        "total_processed":                total_processed,
        "newly_marked_uncontactable":     newly_marked_uncontactable,
        "already_contactable_remains_so": already_contactable_remains_so,
        "filter_reason_distribution":     reason_dist,
        "samples":                        samples,
        "writes_attempted":               writes_attempted,
        "writes_failed":                  writes_failed,
        "warnings":                       warnings,
        "classified_at":                  classified_at,
    }

    logger.info(
        "backlog_scrub.test_fire: dry_run=%s label=%s selected=%d "
        "processed=%d new_uncontactable=%d writes=%d failed=%d",
        req.dry_run, label, len(rows), total_processed,
        newly_marked_uncontactable, writes_attempted, writes_failed,
    )

    return response


# ─────────────────────────────────────────────────────────────────────────────
# Inline self-test (no DB required) — exercises the classifier wiring.
# Run with: python -m api.routes.backlog_scrub
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Mock 3 leads spanning: (a) clean, (b) leadcrap-blocked,
    # (c) phone-pattern-blocked. Verify _classify_one returns expected
    # reasons WITHOUT touching the database or any external service.
    mock_leads = [
        {
            "id": 1,
            "business_name": "Joe's HVAC",
            "phone": "+13109876543",
            "description": "family-run heating contractor",
        },
        {
            "id": 2,
            "business_name": "Acme Corp - Permanently Closed",
            "phone": "+13109876543",
        },
        {
            "id": 3,
            "business_name": "Mike's Plumbing",
            "phone": "+18005551111",  # toll_free + 555 test
        },
    ]
    expectations = [
        (True,  set()),
        (False, {"permanently_closed"}),
        (False, {"toll_free_npa"}),  # test_number_555 also expected
    ]
    fails = 0
    for lead, (want_contactable, want_reasons) in zip(mock_leads, expectations):
        got = _classify_one(lead)
        ok = (got["contactable"] == want_contactable
              and want_reasons.issubset(set(got["reasons"])))
        mark = "OK" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"[{mark}] id={lead['id']} -> contactable={got['contactable']} "
              f"reasons={got['reasons']}")
    print(f"{len(mock_leads) - fails}/{len(mock_leads)} passed")
    raise SystemExit(0 if fails == 0 else 1)
