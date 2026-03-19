"""
KJLE — Prompt 32: Access & Auth Routes
File: api/routes/access_auth.py

API Key management — create, list, revoke, rotate, and audit.
Keys are stored as SHA-256 hashes. Full key only shown once at creation.
Master key (API_SECRET_KEY env var) always works and cannot be revoked.
"""

import os
import secrets
import hashlib
import logging
from typing import Optional, List
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Header, Query
from pydantic import BaseModel
from supabase import create_client, Client

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
MASTER_API_KEY = os.environ.get("API_SECRET_KEY", "kjle-prod-2026-secret")

router = APIRouter()


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _generate_key() -> str:
    """Generate a new API key in format: kjle_<32 random hex chars>"""
    return f"kjle_{secrets.token_hex(16)}"


def verify_master_key(x_api_key: str = Header(...)):
    """Only the master key can manage other keys."""
    if x_api_key != MASTER_API_KEY:
        raise HTTPException(status_code=401, detail="Master API key required for auth management")
    return x_api_key


def verify_any_key(x_api_key: str = Header(...)):
    """Accept master key OR any active key in the api_keys table."""
    if x_api_key == MASTER_API_KEY:
        return x_api_key
    supabase = get_supabase()
    key_hash = _hash_key(x_api_key)
    result = supabase.table("api_keys").select("id, is_active").eq("key_hash", key_hash).execute()
    if not result.data or not result.data[0].get("is_active"):
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    return x_api_key


# ─────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────

class KeyCreate(BaseModel):
    label: str
    scope: Optional[List[str]] = []  # e.g. ['read', 'write', 'admin']


# ─────────────────────────────────────────────────
# GET /kjle/v1/auth/keys — List all keys (masked)
# ─────────────────────────────────────────────────

@router.get("/auth/keys")
async def list_keys(x_api_key: str = Header(...)):
    verify_master_key(x_api_key)
    supabase = get_supabase()

    try:
        result = supabase.table("api_keys").select(
            "id, label, key_prefix, scope, is_active, last_used_at, request_count, created_at, revoked_at"
        ).order("created_at", desc=True).execute()

        return {
            "total": len(result.data or []),
            "keys": result.data or [],
            "note": "Full key values are never stored or shown after creation.",
        }
    except Exception as e:
        logger.error(f"list_keys error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/auth/keys — Create a new API key
# ─────────────────────────────────────────────────

@router.post("/auth/keys")
async def create_key(payload: KeyCreate, x_api_key: str = Header(...)):
    verify_master_key(x_api_key)
    supabase = get_supabase()

    if not payload.label.strip():
        raise HTTPException(status_code=400, detail="label is required")

    try:
        raw_key = _generate_key()
        key_hash = _hash_key(raw_key)
        key_prefix = raw_key[:12]  # "kjle_" + first 7 chars
        now = datetime.now(timezone.utc).isoformat()

        record = {
            "label": payload.label.strip(),
            "key_hash": key_hash,
            "key_prefix": key_prefix,
            "scope": payload.scope or [],
            "is_active": True,
            "request_count": 0,
            "created_at": now,
        }

        result = supabase.table("api_keys").insert(record).execute()
        if not result.data:
            raise HTTPException(status_code=500, detail="Key creation failed")

        created = result.data[0]

        return {
            "id": created["id"],
            "label": created["label"],
            "key_prefix": created["key_prefix"],
            "scope": created["scope"],
            "created_at": created["created_at"],
            "api_key": raw_key,  # ⚠️ Only shown ONCE — store it now
            "warning": "This is the only time the full API key will be shown. Store it securely.",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create_key error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# DELETE /kjle/v1/auth/keys/{key_id} — Revoke a key
# ─────────────────────────────────────────────────

@router.delete("/auth/keys/{key_id}")
async def revoke_key(key_id: str, x_api_key: str = Header(...)):
    verify_master_key(x_api_key)
    supabase = get_supabase()

    try:
        existing = supabase.table("api_keys").select("id, label, is_active").eq("id", key_id).execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="API key not found")
        if not existing.data[0].get("is_active"):
            raise HTTPException(status_code=400, detail="Key is already revoked")

        now = datetime.now(timezone.utc).isoformat()
        supabase.table("api_keys").update({
            "is_active": False,
            "revoked_at": now,
        }).eq("id", key_id).execute()

        return {
            "revoked": True,
            "key_id": key_id,
            "label": existing.data[0]["label"],
            "revoked_at": now,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"revoke_key error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/auth/keys/{key_id}/rotate — Rotate a key
# Generates a new secret, invalidates the old one
# ─────────────────────────────────────────────────

@router.post("/auth/keys/{key_id}/rotate")
async def rotate_key(key_id: str, x_api_key: str = Header(...)):
    verify_master_key(x_api_key)
    supabase = get_supabase()

    try:
        existing = supabase.table("api_keys").select("*").eq("id", key_id).execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="API key not found")
        if not existing.data[0].get("is_active"):
            raise HTTPException(status_code=400, detail="Cannot rotate a revoked key")

        old = existing.data[0]
        new_raw_key = _generate_key()
        new_hash = _hash_key(new_raw_key)
        new_prefix = new_raw_key[:12]
        now = datetime.now(timezone.utc).isoformat()

        supabase.table("api_keys").update({
            "key_hash": new_hash,
            "key_prefix": new_prefix,
            "request_count": 0,
            "last_used_at": None,
        }).eq("id", key_id).execute()

        return {
            "rotated": True,
            "key_id": key_id,
            "label": old["label"],
            "new_key_prefix": new_prefix,
            "rotated_at": now,
            "api_key": new_raw_key,  # ⚠️ Only shown ONCE
            "warning": "This is the only time the new API key will be shown. Store it securely.",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"rotate_key error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# GET /kjle/v1/auth/keys/{key_id}/activity
# ─────────────────────────────────────────────────

@router.get("/auth/keys/{key_id}/activity")
async def key_activity(key_id: str, x_api_key: str = Header(...)):
    verify_master_key(x_api_key)
    supabase = get_supabase()

    try:
        key_res = supabase.table("api_keys").select(
            "id, label, key_prefix, is_active, last_used_at, request_count, created_at"
        ).eq("id", key_id).execute()
        if not key_res.data:
            raise HTTPException(status_code=404, detail="API key not found")

        # Recent audit log entries
        recent = supabase.table("api_audit_log").select(
            "endpoint, method, status_code, ip_address, created_at"
        ).eq("key_id", key_id).order("created_at", desc=True).limit(20).execute()

        return {
            "key": key_res.data[0],
            "recent_requests": recent.data or [],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"key_activity error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# POST /kjle/v1/auth/verify — Verify a key is valid
# ─────────────────────────────────────────────────

@router.post("/auth/verify")
async def verify_key(x_api_key: str = Header(...)):
    # Master key always valid
    if x_api_key == MASTER_API_KEY:
        return {
            "valid": True,
            "key_type": "master",
            "label": "Master Key (env var)",
            "scope": ["admin"],
        }

    supabase = get_supabase()
    key_hash = _hash_key(x_api_key)

    try:
        result = supabase.table("api_keys").select(
            "id, label, key_prefix, scope, is_active, last_used_at, request_count"
        ).eq("key_hash", key_hash).execute()

        if not result.data:
            return {"valid": False, "reason": "Key not found"}

        key = result.data[0]
        if not key.get("is_active"):
            return {"valid": False, "reason": "Key has been revoked"}

        # Update last_used_at and request_count
        supabase.table("api_keys").update({
            "last_used_at": datetime.now(timezone.utc).isoformat(),
            "request_count": (key.get("request_count") or 0) + 1,
        }).eq("id", key["id"]).execute()

        return {
            "valid": True,
            "key_type": "api_key",
            "key_id": key["id"],
            "label": key["label"],
            "key_prefix": key["key_prefix"],
            "scope": key["scope"],
            "last_used_at": key["last_used_at"],
            "request_count": key["request_count"],
        }

    except Exception as e:
        logger.error(f"verify_key error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────
# GET /kjle/v1/auth/audit-log — Paginated audit log
# ─────────────────────────────────────────────────

@router.get("/auth/audit-log")
async def audit_log(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    key_id: Optional[str] = Query(None),
    x_api_key: str = Header(...),
):
    verify_master_key(x_api_key)
    supabase = get_supabase()

    offset = (page - 1) * page_size

    try:
        query = supabase.table("api_audit_log").select("*", count="exact")

        if key_id:
            query = query.eq("key_id", key_id)

        query = query.order("created_at", desc=True).range(offset, offset + page_size - 1)
        result = query.execute()

        return {
            "total": result.count or 0,
            "page": page,
            "page_size": page_size,
            "results": result.data or [],
        }

    except Exception as e:
        logger.error(f"audit_log error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
