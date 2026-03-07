"""
KJLE API — Health Check Routes
GET /kjle/v1/health
GET /kjle/v1/health/db
"""
from fastapi import APIRouter
from datetime import datetime, timezone
from ..database import get_db

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "KJLE API",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health/db")
async def health_db():
    try:
        db = get_db()
        result = db.table("leads").select("id").limit(1).execute()
        return {
            "status": "ok",
            "database": "connected",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {
            "status": "error",
            "database": "disconnected",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
