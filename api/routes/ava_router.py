"""
api/routes/ava_router.py
AVA Smart Routing — admin CRUD for the `ava_router` Supabase schema.

Mounted at: /kjle/v1/ava-router/*

Endpoints (config CRUD ONLY — never touches the live AVA engine or Asterisk):
  GET    /ava-router/routes
  POST   /ava-router/routes
  PUT    /ava-router/routes/{route_id}
  DELETE /ava-router/routes/{route_id}

  GET    /ava-router/personas
  POST   /ava-router/personas
  PUT    /ava-router/personas/{context}

  GET    /ava-router/settings
  PUT    /ava-router/settings              (upsert by key)

  GET    /ava-router/route-log             (read-only, paginated, newest first)

The `ava_router` migration is applied manually by Jim. If the tables do not
yet exist at request time, every endpoint returns a typed empty/no-op
response with `migration_pending: true` — never a 500.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

SCHEMA = "ava_router"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ava(db):
    """Return a PostgREST client bound to the ava_router schema."""
    return db.schema(SCHEMA)


def _is_missing_table(exc: Exception) -> bool:
    """
    Detect PostgREST/Postgres errors that indicate the ava_router schema or
    one of its tables does not yet exist. Used to gracefully degrade so the
    panel can render before Jim applies the migration.
    """
    msg = str(exc).lower()
    needles = (
        "could not find the table",
        "does not exist",
        "schema cache",
        "pgrst205",
        "42p01",
        "3f000",
        "404",
    )
    return any(n in msg for n in needles)


def _empty(kind: str, **extra) -> Dict[str, Any]:
    payload = {
        "status": "ok",
        "migration_pending": True,
        "note": f"ava_router.{kind} not yet present; migration applied separately by Jim.",
        "timestamp": _now_iso(),
    }
    payload.update(extra)
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class RouteCreate(BaseModel):
    product: str
    category: Optional[str] = None
    direction: str = "inbound"
    inbound_context: str
    match_source: Optional[str] = None
    priority: int = 100
    enabled: bool = True
    is_default: bool = False
    business_hours_start: Optional[str] = None
    business_hours_end: Optional[str] = None
    timezone: Optional[str] = None
    fallback_context: Optional[str] = None


class RouteUpdate(BaseModel):
    product: Optional[str] = None
    category: Optional[str] = None
    direction: Optional[str] = None
    inbound_context: Optional[str] = None
    match_source: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    is_default: Optional[bool] = None
    business_hours_start: Optional[str] = None
    business_hours_end: Optional[str] = None
    timezone: Optional[str] = None
    fallback_context: Optional[str] = None


class PersonaCreate(BaseModel):
    context: str
    agent_name: Optional[str] = None
    voice: Optional[str] = None
    tone: Optional[str] = None
    language: Optional[str] = "en"
    greeting: Optional[str] = None
    persona_prompt: Optional[str] = None
    temperature: Optional[float] = 0.7
    speaking_rate: Optional[float] = 1.0
    vad_threshold: Optional[float] = 0.5
    noise_reduction: Optional[bool] = True
    silence_hangup_seconds: Optional[int] = 30
    max_call_seconds: Optional[int] = 1800
    enabled: bool = True


class PersonaUpdate(BaseModel):
    agent_name: Optional[str] = None
    voice: Optional[str] = None
    tone: Optional[str] = None
    language: Optional[str] = None
    greeting: Optional[str] = None
    persona_prompt: Optional[str] = None
    temperature: Optional[float] = None
    speaking_rate: Optional[float] = None
    vad_threshold: Optional[float] = None
    noise_reduction: Optional[bool] = None
    silence_hangup_seconds: Optional[int] = None
    max_call_seconds: Optional[int] = None
    enabled: Optional[bool] = None


class SettingUpsert(BaseModel):
    key: str
    value: Any


# ─────────────────────────────────────────────────────────────────────────────
# Routes  (ava_router.routes)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/ava-router/routes")
async def list_routes(
    product: Optional[str] = Query(None),
    enabled: Optional[bool] = Query(None),
):
    db = get_db()
    try:
        q = _ava(db).table("routes").select("*").order("priority", desc=False)
        if product:
            q = q.eq("product", product)
        if enabled is not None:
            q = q.eq("enabled", enabled)
        rows = q.execute().data or []
        return {"status": "ok", "count": len(rows), "routes": rows, "migration_pending": False}
    except Exception as e:
        if _is_missing_table(e):
            return _empty("routes", routes=[], count=0)
        logger.exception("ava-router list_routes failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ava-router/routes")
async def create_route(body: RouteCreate):
    db = get_db()
    try:
        payload = body.model_dump(exclude_none=True)
        payload["created_at"] = _now_iso()
        payload["updated_at"] = _now_iso()
        res = _ava(db).table("routes").insert(payload).execute()
        created = (res.data or [None])[0]
        return {"status": "ok", "route": created, "migration_pending": False}
    except Exception as e:
        if _is_missing_table(e):
            return _empty("routes", route=None)
        logger.exception("ava-router create_route failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/ava-router/routes/{route_id}")
async def update_route(route_id: str, body: RouteUpdate):
    db = get_db()
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided in request body")
    updates["updated_at"] = _now_iso()
    try:
        res = _ava(db).table("routes").update(updates).eq("id", route_id).execute()
        updated = (res.data or [None])[0]
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Route '{route_id}' not found")
        return {"status": "ok", "route": updated, "migration_pending": False}
    except HTTPException:
        raise
    except Exception as e:
        if _is_missing_table(e):
            return _empty("routes", route=None)
        logger.exception("ava-router update_route failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/ava-router/routes/{route_id}")
async def delete_route(route_id: str):
    db = get_db()
    try:
        res = _ava(db).table("routes").delete().eq("id", route_id).execute()
        deleted = res.data or []
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Route '{route_id}' not found")
        return {"status": "ok", "deleted_id": route_id, "migration_pending": False}
    except HTTPException:
        raise
    except Exception as e:
        if _is_missing_table(e):
            return _empty("routes", deleted_id=None)
        logger.exception("ava-router delete_route failed")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Personas  (ava_router.personas — keyed by `context`)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/ava-router/personas")
async def list_personas(
    enabled: Optional[bool] = Query(None),
):
    db = get_db()
    try:
        q = _ava(db).table("personas").select("*").order("context", desc=False)
        if enabled is not None:
            q = q.eq("enabled", enabled)
        rows = q.execute().data or []
        return {"status": "ok", "count": len(rows), "personas": rows, "migration_pending": False}
    except Exception as e:
        if _is_missing_table(e):
            return _empty("personas", personas=[], count=0)
        logger.exception("ava-router list_personas failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ava-router/personas")
async def create_persona(body: PersonaCreate):
    db = get_db()
    try:
        payload = body.model_dump(exclude_none=True)
        payload["created_at"] = _now_iso()
        payload["updated_at"] = _now_iso()
        res = _ava(db).table("personas").insert(payload).execute()
        created = (res.data or [None])[0]
        return {"status": "ok", "persona": created, "migration_pending": False}
    except Exception as e:
        if _is_missing_table(e):
            return _empty("personas", persona=None)
        logger.exception("ava-router create_persona failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/ava-router/personas/{context}")
async def update_persona(context: str, body: PersonaUpdate):
    db = get_db()
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided in request body")
    updates["updated_at"] = _now_iso()
    try:
        res = _ava(db).table("personas").update(updates).eq("context", context).execute()
        updated = (res.data or [None])[0]
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Persona for context '{context}' not found")
        return {"status": "ok", "persona": updated, "migration_pending": False}
    except HTTPException:
        raise
    except Exception as e:
        if _is_missing_table(e):
            return _empty("personas", persona=None)
        logger.exception("ava-router update_persona failed")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Settings  (ava_router.settings — key/value)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/ava-router/settings")
async def get_settings():
    db = get_db()
    try:
        rows = _ava(db).table("settings").select("*").order("key", desc=False).execute().data or []
        return {"status": "ok", "count": len(rows), "settings": rows, "migration_pending": False}
    except Exception as e:
        if _is_missing_table(e):
            return _empty("settings", settings=[], count=0)
        logger.exception("ava-router get_settings failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/ava-router/settings")
async def upsert_setting(body: SettingUpsert):
    db = get_db()
    payload = {
        "key": body.key,
        "value": body.value,
        "updated_at": _now_iso(),
    }
    try:
        res = _ava(db).table("settings").upsert(payload, on_conflict="key").execute()
        row = (res.data or [None])[0]
        return {"status": "ok", "setting": row, "migration_pending": False}
    except Exception as e:
        if _is_missing_table(e):
            return _empty("settings", setting=None)
        logger.exception("ava-router upsert_setting failed")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Route log  (ava_router.route_log — read-only, paginated, newest first)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/ava-router/route-log")
async def list_route_log(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    db = get_db()
    try:
        rows = (
            _ava(db)
            .table("route_log")
            .select("*")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
            .data
            or []
        )
        return {
            "status": "ok",
            "limit": limit,
            "offset": offset,
            "count": len(rows),
            "entries": rows,
            "migration_pending": False,
        }
    except Exception as e:
        if _is_missing_table(e):
            return _empty("route_log", entries=[], count=0, limit=limit, offset=offset)
        logger.exception("ava-router list_route_log failed")
        raise HTTPException(status_code=500, detail=str(e))
