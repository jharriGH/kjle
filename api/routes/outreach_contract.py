"""
KJLE — Outreach Contract v1 (card 2007, sections 0 + 6)
Routes:
  POST /outreach/leads/search      — lead search with filters + signals
  POST /outreach/leads/count       — same filters → {count, as_of}
  POST /dnc/check                  — batch DNC check (email|phone|domain ≤500)
  POST /dnc                        — opt-out write (global+permanent)
  POST /outreach/signals           — signal writeback from ContactEnginez
  GET  /outreach/admin/tz-backfill/status — tz backfill progress
  POST /outreach/admin/tz-backfill        — trigger one chunk

Auth: Authorization: Bearer <token> + X-KJE-Service: <caller_slug>
      Token = OUTREACH_API_KEY env var (falls back to API_SECRET_KEY).
      DNC_REQUIRE_AUTH=true is live; existing /dnc/* paths stay LIVE.

Idempotency: Idempotency-Key header (write endpoints). Keys stored 7 days
      in outreach_idempotency_keys; replays return original response.

Error body: {error:{code, message, retryable}}
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, field_validator

from ..database import get_db
from ..lib.phone_utils import normalize_phone
from ..lib.tz_backfill import run_tz_backfill_chunk

logger = logging.getLogger(__name__)

# ── Auth ─────────────────────────────────────────────────────────────────────
_API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")
OUTREACH_API_KEY = os.environ.get("OUTREACH_API_KEY", _API_SECRET_KEY)


def _outreach_auth(
    authorization: Optional[str] = Header(None),
    x_kje_service: Optional[str] = Header(None, alias="X-KJE-Service"),
) -> str:
    """Bearer + X-KJE-Service auth for all outreach contract endpoints."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "unauthorized", "message": "Missing Authorization Bearer token", "retryable": False}},
        )
    token = authorization[7:].strip()
    if token != OUTREACH_API_KEY:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "unauthorized", "message": "Invalid Bearer token", "retryable": False}},
        )
    if not x_kje_service:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "unauthorized", "message": "Missing X-KJE-Service header", "retryable": False}},
        )
    return x_kje_service


router = APIRouter()


# ── Idempotency helpers ───────────────────────────────────────────────────────

def _idempotency_check(
    db, idempotency_key: Optional[str], endpoint: str
) -> Optional[tuple[int, dict]]:
    """Return (status, body) if key exists and is not expired, else None."""
    if not idempotency_key:
        return None
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        res = (
            db.table("outreach_idempotency_keys")
            .select("response_status,response_body")
            .eq("key", idempotency_key)
            .eq("endpoint", endpoint)
            .gt("expires_at", now_iso)
            .limit(1)
            .execute()
        )
        if res.data:
            row = res.data[0]
            return row["response_status"], row["response_body"]
    except Exception as exc:
        logger.warning(f"[idempotency] check failed key={idempotency_key!r}: {exc}")
    return None


def _idempotency_store(
    db, idempotency_key: Optional[str], endpoint: str, status: int, body: dict
) -> None:
    if not idempotency_key:
        return
    try:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        db.table("outreach_idempotency_keys").upsert(
            {
                "key": idempotency_key,
                "endpoint": endpoint,
                "response_body": body,
                "response_status": status,
                "expires_at": expires_at,
            },
            on_conflict="key,endpoint",
        ).execute()
    except Exception as exc:
        logger.warning(f"[idempotency] store failed key={idempotency_key!r}: {exc}")


# ── business_key derivation ───────────────────────────────────────────────────

_PUBLIC_SUFFIXES_2 = frozenset({
    "co.uk", "co.nz", "co.jp", "com.au", "co.za", "com.sg",
    "org.uk", "gov.uk", "co.in", "net.au", "org.au",
})


def _registrable_domain(raw: Optional[str]) -> Optional[str]:
    """Extract registrable domain from a URL or hostname string."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        host = urlparse(raw).hostname or ""
        host = host.lower().strip()
        # Strip leading www.
        if host.startswith("www."):
            host = host[4:]
        if not host or "." not in host:
            return None
        parts = host.split(".")
        if len(parts) >= 3:
            maybe_2 = ".".join(parts[-2:])
            if maybe_2 in _PUBLIC_SUFFIXES_2:
                return ".".join(parts[-3:])
        return ".".join(parts[-2:])
    except Exception:
        return None


def _business_key(website: Optional[str], email: Optional[str], lead_id: str) -> str:
    if website:
        d = _registrable_domain(website)
        if d:
            return d
    if email and "@" in email:
        domain = email.split("@", 1)[1].strip().lower()
        if domain and "." in domain:
            return domain
    return f"lead:{lead_id}"


# ── Lead search columns ───────────────────────────────────────────────────────

_LEAD_SELECT_COLS = (
    "id, business_name, email, email_status, website, phone, niche_slug, "
    "city, state, zip, timezone, do_not_contact, "
    "google_review_count, google_stars, form_status, site_dead, "
    "site_copyright_year, website_has_ssl, mobile_friendly, builder, outreach_socials"
)


def _format_lead(row: dict) -> dict:
    lead_id = str(row["id"])
    return {
        "kjle_lead_id":  lead_id,
        "business_name": row.get("business_name"),
        "first_name":    None,  # not stored in leads table
        "email":         row.get("email"),
        "email_status":  row.get("email_status"),
        "website":       row.get("website"),
        "phone":         row.get("phone"),
        "niche_slug":    row.get("niche_slug"),
        "city":          row.get("city"),
        "state":         row.get("state"),
        "zip":           row.get("zip"),
        "tz":            row.get("timezone"),
        "business_key":  _business_key(row.get("website"), row.get("email"), lead_id),
        "signals": {
            "google_reviews":      row.get("google_review_count"),
            "rating":              row.get("google_stars"),
            "form_status":         row.get("form_status"),
            "site_dead":           row.get("site_dead"),
            "site_copyright_year": row.get("site_copyright_year"),
            "has_ssl":             row.get("website_has_ssl"),
            "mobile_ok":           row.get("mobile_friendly"),
            "builder":             row.get("builder"),
        },
    }


# ── Pydantic models ───────────────────────────────────────────────────────────

class SignalFilters(BaseModel):
    google_reviews_lt:   Optional[int]        = None
    rating_lt:           Optional[float]      = None
    form_status:         Optional[list[str]]  = None
    site_dead:           Optional[bool]       = None
    site_copyright_lte:  Optional[int]        = None
    has_email:           Optional[bool]       = None


class LeadFilters(BaseModel):
    niche_slugs: Optional[list[str]]    = None
    cities:      Optional[list[str]]    = None
    states:      Optional[list[str]]    = None
    zips:        Optional[list[str]]    = None
    signals:     Optional[SignalFilters] = None


class LeadSearchRequest(BaseModel):
    filters:     Optional[LeadFilters] = None
    exclude_dnc: bool                  = True
    cursor:      Optional[str]         = None
    limit:       int                   = 100

    @field_validator("limit")
    @classmethod
    def _limit_cap(cls, v: int) -> int:
        return max(1, min(v, 1000))


class LeadCountRequest(BaseModel):
    filters: Optional[LeadFilters] = None


class DncCheckItem(BaseModel):
    email:  Optional[str] = None
    phone:  Optional[str] = None
    domain: Optional[str] = None


class DncCheckRequest(BaseModel):
    items: list[DncCheckItem]

    @field_validator("items")
    @classmethod
    def _items_cap(cls, v):
        if len(v) > 500:
            raise ValueError("items list exceeds maximum of 500")
        return v


_REASON_ENUM = Literal[
    "unsubscribe", "complaint", "remove_reply",
    "call_optout", "form_optout", "manual"
]


class DncWriteRequest(BaseModel):
    email:  Optional[str] = None
    phone:  Optional[str] = None
    domain: Optional[str] = None
    reason: _REASON_ENUM
    source: str


class SignalItem(BaseModel):
    kjle_lead_id:        str
    form_status:         Optional[str]  = None
    site_dead:           Optional[bool] = None
    site_copyright_year: Optional[int]  = None
    ssl:                 Optional[bool] = None
    mobile_ok:           Optional[bool] = None
    builder:             Optional[str]  = None
    phones:              Optional[list[str]] = None
    socials:             Optional[dict]  = None


class SignalsRequest(BaseModel):
    items: list[SignalItem]


# ── Filter builder ────────────────────────────────────────────────────────────

def _apply_filters(q, filters: Optional[LeadFilters], exclude_dnc: bool):
    """Apply LeadFilters to a supabase query builder and return it."""
    q = q.eq("is_active", True)
    if filters:
        if filters.niche_slugs:
            q = q.in_("niche_slug", filters.niche_slugs)
        if filters.cities:
            q = q.in_("city", filters.cities)
        if filters.states:
            q = q.in_("state", filters.states)
        if filters.zips:
            q = q.in_("zip", filters.zips)
        if filters.signals:
            sig = filters.signals
            if sig.google_reviews_lt is not None:
                q = q.lt("google_review_count", sig.google_reviews_lt)
            if sig.rating_lt is not None:
                q = q.lt("google_stars", sig.rating_lt)
            if sig.form_status:
                q = q.in_("form_status", sig.form_status)
            if sig.site_dead is not None:
                q = q.eq("site_dead", sig.site_dead)
            if sig.site_copyright_lte is not None:
                q = q.lte("site_copyright_year", sig.site_copyright_lte)
            if sig.has_email:
                q = q.not_.is_("email", "null").neq("email", "")
    if exclude_dnc:
        q = q.neq("do_not_contact", True)
    return q


# ── 1. POST /outreach/leads/search ───────────────────────────────────────────

@router.post("/outreach/leads/search", dependencies=[Depends(_outreach_auth)])
async def outreach_leads_search(req: LeadSearchRequest):
    """
    Search leads with filters. Returns leads[] with tz (IANA), business_key,
    signals{}, and keyset cursor for pagination.
    """
    db = get_db()
    q = db.table("leads").select(_LEAD_SELECT_COLS)
    q = _apply_filters(q, req.filters, req.exclude_dnc)
    q = q.order("id", desc=False)
    if req.cursor:
        try:
            q = q.gt("id", req.cursor)
        except Exception:
            raise HTTPException(
                status_code=400,
                detail={"error": {"code": "invalid_cursor", "message": "cursor must be a valid lead id", "retryable": False}},
            )
    q = q.limit(req.limit)

    try:
        rows = q.execute().data or []
    except Exception as exc:
        logger.error(f"[outreach/leads/search] query failed: {exc}")
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": "db_error", "message": "database query failed", "retryable": True}},
        )

    leads = [_format_lead(r) for r in rows]
    next_cursor = str(rows[-1]["id"]) if len(rows) == req.limit else None

    return {"leads": leads, "next_cursor": next_cursor}


# ── 2. POST /outreach/leads/count ────────────────────────────────────────────

@router.post("/outreach/leads/count", dependencies=[Depends(_outreach_auth)])
async def outreach_leads_count(req: LeadCountRequest):
    """
    Count leads matching filters. Target <2s.
    Uses stats_cache for broad niche-only filters; exact COUNT otherwise.
    """
    db = get_db()
    as_of = datetime.now(timezone.utc).isoformat()

    filters = req.filters

    # Fast path: niche-only filter with no signals → try stats_cache
    _broad = (
        filters is None
        or (
            not filters.cities
            and not filters.states
            and not filters.zips
            and (not filters.signals or _all_signal_null(filters.signals))
        )
    )
    if _broad and filters and filters.niche_slugs:
        cached_count = _count_from_stats_cache(db, filters.niche_slugs)
        if cached_count is not None:
            return {"count": cached_count, "as_of": as_of, "source": "cache"}

    # Exact count
    try:
        q = db.table("leads").select("id", count="exact")
        q = _apply_filters(q, filters, exclude_dnc=True)
        res = q.limit(1).execute()
        return {"count": res.count or 0, "as_of": as_of, "source": "exact"}
    except Exception as exc:
        logger.error(f"[outreach/leads/count] query failed: {exc}")
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": "db_error", "message": "database query failed", "retryable": True}},
        )


def _all_signal_null(sig: SignalFilters) -> bool:
    return all(
        v is None for v in [
            sig.google_reviews_lt, sig.rating_lt, sig.form_status,
            sig.site_dead, sig.site_copyright_lte, sig.has_email,
        ]
    )


def _count_from_stats_cache(db, niche_slugs: list[str]) -> Optional[int]:
    """Read counts from stats_cache 'segments_by_niche'. Returns None on miss."""
    try:
        res = (
            db.table("stats_cache")
            .select("data")
            .eq("key", "segments_by_niche")
            .limit(1)
            .execute()
        )
        if not res.data:
            return None
        rows = res.data[0].get("data") or []
        slugs_set = set(niche_slugs)
        total = 0
        for row in rows:
            if row.get("niche_slug") in slugs_set:
                total += (
                    (row.get("hot_count") or 0)
                    + (row.get("warm_count") or 0)
                    + (row.get("cold_count") or 0)
                    + (row.get("unclassified_count") or 0)
                )
        return total
    except Exception as exc:
        logger.warning(f"[outreach/leads/count] stats_cache read failed: {exc}")
        return None


# ── 3. POST /dnc/check  (new batch endpoint) ─────────────────────────────────

@router.post("/dnc/check", dependencies=[Depends(_outreach_auth)])
async def outreach_dnc_check(req: DncCheckRequest):
    """
    Batch DNC check. Checks email_suppressions (email), dnc_suppressions (phone),
    and email_suppressions domain-pattern (domain). No external provider calls.
    """
    db = get_db()

    emails  = [(i, (item.email or "").strip().lower()) for i, item in enumerate(req.items) if item.email]
    phones  = [(i, item.phone) for i, item in enumerate(req.items) if item.phone]
    domains = [(i, (item.domain or "").strip().lower()) for i, item in enumerate(req.items) if item.domain]

    blocked: dict[int, str] = {}  # index → reason

    # Email batch check
    if emails:
        email_vals = [e for _, e in emails if e]
        try:
            res = (
                db.table("email_suppressions")
                .select("email,reason")
                .in_("email", email_vals)
                .execute()
            )
            suppressed_emails = {r["email"]: r.get("reason", "suppressed") for r in (res.data or [])}
            for idx, email in emails:
                if email in suppressed_emails:
                    blocked[idx] = f"email_suppressed:{suppressed_emails[email]}"
        except Exception as exc:
            logger.warning(f"[dnc/check] email_suppressions lookup failed: {exc}")

    # Phone batch check — check dnc_suppressions
    if phones:
        normalized_phones: list[tuple[int, Optional[str]]] = []
        for idx, raw_phone in phones:
            norm = normalize_phone(raw_phone or "")
            normalized_phones.append((idx, norm))

        phone_vals = [p for _, p in normalized_phones if p]
        if phone_vals:
            try:
                res = (
                    db.table("dnc_suppressions")
                    .select("phone,reason")
                    .in_("phone", phone_vals)
                    .execute()
                )
                suppressed_phones = {r["phone"]: r.get("reason", "suppressed") for r in (res.data or [])}
                for idx, norm in normalized_phones:
                    if norm and norm in suppressed_phones:
                        blocked[idx] = f"phone_suppressed:{suppressed_phones[norm]}"
            except Exception as exc:
                logger.warning(f"[dnc/check] dnc_suppressions lookup failed: {exc}")

        # Also check do_not_contact on leads table for phones
        for idx, norm in normalized_phones:
            if norm and idx not in blocked:
                try:
                    res = (
                        db.table("leads")
                        .select("id")
                        .eq("phone", norm)
                        .eq("do_not_contact", True)
                        .limit(1)
                        .execute()
                    )
                    if res.data:
                        blocked[idx] = "lead_dnc"
                except Exception:
                    pass

    # Domain batch check — check email_suppressions for @domain entries
    if domains:
        for idx, domain in domains:
            if not domain or idx in blocked:
                continue
            try:
                # Check if any suppression exists with this domain
                res = (
                    db.table("email_suppressions")
                    .select("email,reason")
                    .like("email", f"%@{domain}")
                    .limit(1)
                    .execute()
                )
                if res.data:
                    blocked[idx] = f"domain_suppressed:{res.data[0].get('reason','suppressed')}"
            except Exception as exc:
                logger.warning(f"[dnc/check] domain lookup failed for {domain!r}: {exc}")

    # Build response
    results = []
    for i, item in enumerate(req.items):
        item_str = item.email or item.phone or item.domain or ""
        if i in blocked:
            results.append({"item": item_str, "blocked": True, "reason": blocked[i]})
        else:
            results.append({"item": item_str, "blocked": False, "reason": ""})

    return {"results": results}


# ── 4. POST /dnc  (new unified write) ────────────────────────────────────────

@router.post("/dnc", status_code=201, dependencies=[Depends(_outreach_auth)])
async def outreach_dnc_write(
    req: DncWriteRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    Opt-out write. Global and permanent. Maps to existing suppression tables.
    email  → email_suppressions
    phone  → dnc_suppressions + lead.do_not_contact
    domain → email_suppressions (sentinel @domain entry)
    """
    if not any([req.email, req.phone, req.domain]):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "bad_request", "message": "one of email, phone, or domain is required", "retryable": False}},
        )

    db = get_db()
    endpoint = "POST /dnc"

    # Idempotency replay
    cached = _idempotency_check(db, idempotency_key, endpoint)
    if cached is not None:
        _, body = cached
        return body

    now_iso = datetime.now(timezone.utc).isoformat()
    written: list[str] = []
    errors: list[str] = []

    if req.email:
        email = (req.email or "").strip().lower()
        if email and "@" in email:
            try:
                db.table("email_suppressions").upsert(
                    {"email": email, "reason": req.reason, "source": req.source},
                    on_conflict="email",
                ).execute()
                # Mark on leads table
                db.table("leads").update({
                    "do_not_contact": True,
                    "updated_at": now_iso,
                }).eq("email", email).execute()
                written.append(f"email:{email}")
            except Exception as exc:
                logger.error(f"[POST /dnc] email write failed: {exc}")
                errors.append(f"email_write_failed:{str(exc)[:80]}")

    if req.phone:
        norm = normalize_phone(req.phone)
        if norm:
            try:
                db.table("dnc_suppressions").upsert(
                    {
                        "phone": norm, "reason": req.reason,
                        "source": req.source, "suppressed_at": now_iso,
                    },
                    on_conflict="phone",
                ).execute()
                # Invalidate cache
                try:
                    db.table("dnc_cache").update({"expires_at": "2000-01-01T00:00:00+00:00"}).eq("phone", norm).execute()
                except Exception:
                    pass
                # Mark on leads
                db.table("leads").update({
                    "do_not_contact": True, "updated_at": now_iso,
                }).eq("phone", norm).execute()
                written.append(f"phone:{norm}")
            except Exception as exc:
                logger.error(f"[POST /dnc] phone write failed: {exc}")
                errors.append(f"phone_write_failed:{str(exc)[:80]}")
        else:
            errors.append(f"invalid_phone:{req.phone!r}")

    if req.domain:
        domain = (req.domain or "").strip().lower().lstrip("@")
        if domain and "." in domain:
            sentinel_email = f"@{domain}"
            try:
                db.table("email_suppressions").upsert(
                    {"email": sentinel_email, "reason": req.reason, "source": req.source},
                    on_conflict="email",
                ).execute()
                written.append(f"domain:{domain}")
            except Exception as exc:
                logger.error(f"[POST /dnc] domain write failed: {exc}")
                errors.append(f"domain_write_failed:{str(exc)[:80]}")

    if errors and not written:
        raise HTTPException(
            status_code=500,
            detail={"error": {"code": "write_failed", "message": "; ".join(errors), "retryable": True}},
        )

    response_body = {
        "status": "created",
        "written": written,
        "errors": errors if errors else None,
    }
    _idempotency_store(db, idempotency_key, endpoint, 201, response_body)
    return response_body


# ── 5. POST /outreach/signals ─────────────────────────────────────────────────

@router.post("/outreach/signals", dependencies=[Depends(_outreach_auth)])
async def outreach_signals(
    req: SignalsRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    Signal writeback from ContactEnginez. Upserts form_status, site_dead,
    site_copyright_year, builder, outreach_socials onto leads.
    ssl → website_has_ssl; mobile_ok → mobile_friendly.
    """
    db = get_db()
    endpoint = "POST /outreach/signals"

    cached = _idempotency_check(db, idempotency_key, endpoint)
    if cached is not None:
        _, body = cached
        return body

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = 0
    errors  = 0

    UPDATE_CHUNK = 100

    for item in req.items:
        payload: dict[str, Any] = {"updated_at": now_iso}

        if item.form_status is not None:
            payload["form_status"] = item.form_status
        if item.site_dead is not None:
            payload["site_dead"] = item.site_dead
        if item.site_copyright_year is not None:
            payload["site_copyright_year"] = item.site_copyright_year
        if item.builder is not None:
            payload["builder"] = item.builder
        if item.ssl is not None:
            payload["website_has_ssl"] = item.ssl
        if item.mobile_ok is not None:
            payload["mobile_friendly"] = item.mobile_ok
        if item.socials is not None:
            payload["outreach_socials"] = item.socials

        if len(payload) == 1:  # only updated_at
            continue

        try:
            db.table("leads").update(payload).eq("id", item.kjle_lead_id).execute()
            updated += 1
        except Exception as exc:
            logger.error(f"[outreach/signals] update failed id={item.kjle_lead_id!r}: {exc}")
            errors += 1

    response_body = {
        "status": "ok",
        "updated": updated,
        "errors": errors,
        "as_of": now_iso,
    }
    _idempotency_store(db, idempotency_key, endpoint, 200, response_body)
    return response_body


# ── Admin: TZ backfill ─────────────────────────────────────────────────────────

@router.get("/outreach/admin/tz-backfill/status", dependencies=[Depends(_outreach_auth)])
async def tz_backfill_status():
    """Return tz backfill progress: populated count vs total active US leads."""
    db = get_db()
    try:
        total_res = (
            db.table("leads")
            .select("id", count="exact")
            .eq("is_active", True)
            .not_.is_("state", "null")
            .limit(1)
            .execute()
        )
        total_us = total_res.count or 0

        populated_res = (
            db.table("leads")
            .select("id", count="exact")
            .eq("is_active", True)
            .not_.is_("state", "null")
            .not_.is_("timezone", "null")
            .neq("timezone", "")
            .limit(1)
            .execute()
        )
        populated = populated_res.count or 0

        pct = round(100.0 * populated / total_us, 2) if total_us else 0.0
        remaining = total_us - populated
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": "db_error", "message": str(exc), "retryable": True}},
        )

    return {
        "total_us_leads":  total_us,
        "tz_populated":    populated,
        "remaining":       remaining,
        "pct_populated":   pct,
        "target_pct":      98.0,
        "as_of":           datetime.now(timezone.utc).isoformat(),
    }


@router.post("/outreach/admin/tz-backfill", dependencies=[Depends(_outreach_auth)])
async def tz_backfill_trigger(chunk_size: int = 20_000):
    """Trigger one backfill chunk synchronously. Returns chunk stats."""
    chunk_size = max(1000, min(chunk_size, 50_000))
    db = get_db()
    result = run_tz_backfill_chunk(db, chunk_size=chunk_size)
    return {"status": "chunk_complete", **result}
