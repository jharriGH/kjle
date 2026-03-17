"""
api/routes/admin_settings.py
Admin settings key/value store backed by Supabase admin_settings table.

Endpoints:
  GET  /admin/settings           → all settings (typed, truelist_api_key masked)
  POST /admin/settings           → partial update (any subset of keys)
  GET  /admin/settings/{key}     → single setting value
  POST /admin/settings/reset     → reset all to defaults
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from ..database import get_db

router = APIRouter()

# ── Default values (all stored as TEXT in Supabase) ───────────────────────────
DEFAULTS: Dict[str, str] = {
    "budget_cap_usd":               "10.00",
    "classify_batch_size":          "1000",
    "enrich_batch_size":            "50",
    "stale_cutoff_days":            "180",
    "email_clean_enabled":          "false",
    "email_clean_batch_size":       "100",
    "truelist_api_key":             "",
    "cost_alert_threshold_warn":    "5.00",
    "cost_alert_threshold_critical":"8.00",
    "scheduler_enabled":            "true",
}

# Keys whose type should be cast on read
_FLOAT_KEYS = {"budget_cap_usd", "cost_alert_threshold_warn", "cost_alert_threshold_critical"}
_INT_KEYS   = {"classify_batch_size", "enrich_batch_size", "stale_cutoff_days", "email_clean_batch_size"}
_BOOL_KEYS  = {"email_clean_enabled", "scheduler_enabled"}
_SECRET_KEYS = {"truelist_api_key"}


def _cast(key: str, raw: str) -> Any:
    """Cast a raw text DB value to its correct Python type."""
    if key in _FLOAT_KEYS:
        try: return float(raw)
        except: return float(DEFAULTS.get(key, "0"))
    if key in _INT_KEYS:
        try: return int(raw)
        except: return int(DEFAULTS.get(key, "0"))
    if key in _BOOL_KEYS:
        return raw.strip().lower() in ("true", "1", "yes")
    return raw  # string


def _mask(key: str, value: str) -> Any:
    """Mask secret values in API responses."""
    if key in _SECRET_KEYS:
        return "***masked***" if value else ""
    return _cast(key, value)


def _load_all(db) -> Dict[str, str]:
    """Load all settings from DB, filling gaps with DEFAULTS."""
    res = db.table("admin_settings").select("key, value").execute()
    db_map: Dict[str, str] = {row["key"]: row["value"] for row in (res.data or [])}
    # Merge: DB values override defaults
    merged = {**DEFAULTS, **{k: v for k, v in db_map.items() if k in DEFAULTS}}
    return merged


def _upsert(db, key: str, value: str):
    """Upsert a single key/value into admin_settings."""
    db.table("admin_settings").upsert(
        {"key": key, "value": value, "updated_at": datetime.now(timezone.utc).isoformat()},
        on_conflict="key"
    ).execute()


# ── Pydantic model for POST body ──────────────────────────────────────────────
class SettingsUpdate(BaseModel):
    budget_cap_usd:               Optional[float] = None
    classify_batch_size:          Optional[int]   = None
    enrich_batch_size:            Optional[int]   = None
    stale_cutoff_days:            Optional[int]   = None
    email_clean_enabled:          Optional[bool]  = None
    email_clean_batch_size:       Optional[int]   = None
    truelist_api_key:             Optional[str]   = None
    cost_alert_threshold_warn:    Optional[float] = None
    cost_alert_threshold_critical:Optional[float] = None
    scheduler_enabled:            Optional[bool]  = None


# ── GET /admin/settings ───────────────────────────────────────────────────────
@router.get("/admin/settings")
async def get_all_settings(db=Depends(get_db)):
    try:
        raw = _load_all(db)
        return {
            "status": "success",
            "settings": {k: _mask(k, v) for k, v in raw.items()},
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── POST /admin/settings ──────────────────────────────────────────────────────
@router.post("/admin/settings")
async def update_settings(payload: SettingsUpdate, db=Depends(get_db)):
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided in request body")

    updated_keys = []
    errors = []

    for key, val in updates.items():
        if key not in DEFAULTS:
            errors.append(f"Unknown setting: {key}")
            continue
        try:
            # Serialize to string for storage
            if isinstance(val, bool):
                raw = "true" if val else "false"
            else:
                raw = str(val)
            _upsert(db, key, raw)
            updated_keys.append(key)
        except Exception as e:
            errors.append(f"{key}: {str(e)}")

    return {
        "status": "success" if not errors else "partial",
        "updated": updated_keys,
        "errors": errors,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── GET /admin/settings/reset — must be before /{key} to avoid routing conflict
@router.post("/admin/settings/reset")
async def reset_settings(db=Depends(get_db)):
    try:
        for key, value in DEFAULTS.items():
            _upsert(db, key, value)
        raw = _load_all(db)
        return {
            "status": "success",
            "message": "All settings reset to defaults",
            "settings": {k: _mask(k, v) for k, v in raw.items()},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /admin/settings/{key} ─────────────────────────────────────────────────
@router.get("/admin/settings/{key}")
async def get_setting(key: str, db=Depends(get_db)):
    if key not in DEFAULTS:
        raise HTTPException(status_code=404, detail=f"Unknown setting key: '{key}'")

    try:
        res = (
            db.table("admin_settings")
            .select("key, value")
            .eq("key", key)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        raw_val = rows[0]["value"] if rows else DEFAULTS[key]
        return {
            "status": "success",
            "key":    key,
            "value":  _mask(key, raw_val),
            "default": _mask(key, DEFAULTS[key]),
            "is_default": not bool(rows),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
