"""
KJLE — Prompt 27: CSV Management
Route prefix handled in main.py: /kjle/v1
"""

import csv
import io
import logging
import re
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# ── In-memory upload store (max 50, evict oldest) ─────────────────────────────

_upload_store: OrderedDict[str, dict] = OrderedDict()
_UPLOAD_STORE_MAX = 50

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
PREVIEW_ROWS = 5

# ── Known lead fields ─────────────────────────────────────────────────────────

LEAD_FIELDS = [
    "business_name", "phone", "email", "website",
    "address", "city", "state", "zip",
    "niche_slug", "pain_score", "notes",
    "owner_name", "google_stars", "google_review_count",
]

FIELD_ALIASES: Dict[str, List[str]] = {
    "business_name":       ["business", "company", "name", "biz_name", "business name"],
    "phone":               ["phone", "phone_number", "tel", "telephone", "mobile", "cell"],
    "email":               ["email", "email_address", "e-mail"],
    "website":             ["website", "url", "web", "site", "domain"],
    "city":                ["city", "town", "municipality"],
    "state":               ["state", "province", "region", "st"],
    "zip":                 ["zip", "zipcode", "zip_code", "postal", "postal_code"],
    "niche_slug":          ["niche", "category", "type", "industry", "vertical", "niche_slug"],
    "pain_score":          ["pain", "pain_score", "score", "priority"],
    "owner_name":          ["owner", "owner_name", "contact", "contact_name"],
    "google_stars":        ["stars", "rating", "google_stars", "google_rating"],
    "google_review_count": ["reviews", "review_count", "google_reviews"],
}


# ── Pydantic models ───────────────────────────────────────────────────────────

class ImportOptions(BaseModel):
    field_mapping: Dict[str, str]                  # csv_column -> lead_field
    niche_slug_override: Optional[str] = None
    normalize_niche: bool = True
    skip_duplicates: bool = True
    default_pain_score: int = 50
    state_override: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _store_upload(upload_id: str, data: dict) -> None:
    if len(_upload_store) >= _UPLOAD_STORE_MAX:
        _upload_store.popitem(last=False)  # evict oldest
    _upload_store[upload_id] = data


def _suggest_mappings(headers: List[str]) -> Dict[str, str]:
    """Fuzzy-match CSV headers to known lead fields via FIELD_ALIASES."""
    suggestions: Dict[str, str] = {}
    for csv_col in headers:
        normalized = csv_col.strip().lower().replace(" ", "_").replace("-", "_")
        # Direct match first
        if normalized in LEAD_FIELDS:
            suggestions[csv_col] = normalized
            continue
        # Alias match
        for lead_field, aliases in FIELD_ALIASES.items():
            if normalized in [a.replace(" ", "_").replace("-", "_") for a in aliases]:
                suggestions[csv_col] = lead_field
                break
    return suggestions


def _normalize_niche(raw: str) -> str:
    return raw.strip().lower().replace(" ", "_").replace("-", "_")


def _clean_phone(raw: str) -> Optional[str]:
    digits = re.sub(r"\D", "", raw or "")
    return digits if len(digits) >= 10 else None


def _valid_email(raw: str) -> bool:
    return "@" in (raw or "") and "." in (raw or "").split("@")[-1]


def _parse_csv_bytes(content: bytes) -> tuple[List[str], List[dict]]:
    """Return (headers, rows_as_dicts)."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    rows = [dict(row) for row in reader]
    return headers, rows


# ── POST /csv/upload ──────────────────────────────────────────────────────────

@router.post("/csv/upload")
async def upload_csv(file: UploadFile = File(...), db=Depends(get_db)):
    """Upload a CSV file, preview first 5 rows, return suggested field mappings."""

    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted.")

    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 10MB limit.")

    if not content.strip():
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        headers, rows = _parse_csv_bytes(content)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not parse CSV: {e}")

    if not headers:
        raise HTTPException(status_code=422, detail="CSV has no headers.")

    upload_id = str(uuid.uuid4())
    preview = rows[:PREVIEW_ROWS]
    suggested = _suggest_mappings(headers)

    _store_upload(upload_id, {
        "filename": file.filename,
        "headers": headers,
        "rows": rows,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    })

    return {
        "upload_id": upload_id,
        "filename": file.filename,
        "row_count": len(rows),
        "headers": headers,
        "preview": preview,
        "suggested_mappings": suggested,
    }


# ── POST /csv/import/{upload_id} ─────────────────────────────────────────────

@router.post("/csv/import/{upload_id}")
async def import_csv(upload_id: str, opts: ImportOptions, db=Depends(get_db)):
    """Import leads from a previously uploaded CSV using caller-supplied field mapping."""

    upload = _upload_store.get(upload_id)
    if not upload:
        raise HTTPException(
            status_code=404,
            detail=f"Upload ID '{upload_id}' not found or expired. Re-upload the file.",
        )

    rows: List[dict] = upload["rows"]
    filename: str = upload["filename"]
    mapping: Dict[str, str] = opts.field_mapping  # csv_col -> lead_field

    imported = 0
    skipped_duplicate = 0
    skipped_invalid = 0
    failed = 0
    failed_rows: List[dict] = []

    # Pre-fetch existing phones for dedup (only if needed)
    existing_phones: set = set()
    if opts.skip_duplicates:
        try:
            page_size = 1000
            offset = 0
            while True:
                res = (
                    db.table("leads")
                    .select("phone")
                    .not_.is_("phone", "null")
                    .range(offset, offset + page_size - 1)
                    .execute()
                )
                batch = res.data or []
                for r in batch:
                    if r.get("phone"):
                        existing_phones.add(re.sub(r"\D", "", r["phone"]))
                if len(batch) < page_size:
                    break
                offset += page_size
        except Exception as e:
            logger.warning(f"[csv-import] Could not pre-fetch phones for dedup: {e}")

    for idx, row in enumerate(rows):
        row_num = idx + 1
        try:
            lead: Dict[str, Any] = {}

            # Map CSV columns -> lead fields
            for csv_col, lead_field in mapping.items():
                raw_val = row.get(csv_col, "")
                if raw_val is not None:
                    lead[lead_field] = str(raw_val).strip()

            # ── phone validation ──────────────────────────────────────────
            raw_phone = lead.get("phone", "")
            clean_phone = _clean_phone(raw_phone)
            if not clean_phone:
                skipped_invalid += 1
                failed_rows.append({"row": row_num, "data": row, "reason": "Invalid or missing phone"})
                continue
            lead["phone"] = clean_phone

            # ── duplicate check ───────────────────────────────────────────
            if opts.skip_duplicates:
                if clean_phone in existing_phones:
                    skipped_duplicate += 1
                    continue

            # ── email validation (soft — skip field if invalid) ───────────
            if "email" in lead and lead["email"]:
                if not _valid_email(lead["email"]):
                    lead.pop("email", None)

            # ── niche normalization ───────────────────────────────────────
            if opts.niche_slug_override:
                niche = opts.niche_slug_override
                if opts.normalize_niche:
                    niche = _normalize_niche(niche)
                lead["niche_slug"] = niche
            elif "niche_slug" in lead and lead["niche_slug"]:
                if opts.normalize_niche:
                    lead["niche_slug"] = _normalize_niche(lead["niche_slug"])

            # ── state override ────────────────────────────────────────────
            if opts.state_override:
                lead["state"] = opts.state_override.strip().upper()

            # ── pain_score ────────────────────────────────────────────────
            if "pain_score" in lead:
                try:
                    lead["pain_score"] = int(float(lead["pain_score"]))
                except (ValueError, TypeError):
                    lead["pain_score"] = opts.default_pain_score
            else:
                lead["pain_score"] = opts.default_pain_score

            # ── google_stars / google_review_count ────────────────────────
            for float_field in ["google_stars"]:
                if float_field in lead and lead[float_field]:
                    try:
                        lead[float_field] = float(lead[float_field])
                    except (ValueError, TypeError):
                        lead.pop(float_field, None)

            for int_field in ["google_review_count"]:
                if int_field in lead and lead[int_field]:
                    try:
                        lead[int_field] = int(float(lead[int_field]))
                    except (ValueError, TypeError):
                        lead.pop(int_field, None)

            # ── defaults ──────────────────────────────────────────────────
            lead["is_active"] = True
            lead["enrichment_stage"] = 0

            # ── strip empty strings ───────────────────────────────────────
            lead = {k: v for k, v in lead.items() if v != "" and v is not None}

            # ── insert ────────────────────────────────────────────────────
            db.table("leads").insert(lead).execute()

            existing_phones.add(clean_phone)  # update local set
            imported += 1

        except Exception as e:
            failed += 1
            logger.error(f"[csv-import] Failed row {row_num}: {e}")
            failed_rows.append({"row": row_num, "data": row, "reason": str(e)})

    total_rows = len(rows)
    skipped_total = skipped_duplicate + skipped_invalid

    # ── log to import_log ──────────────────────────────────────────────────
    niche_for_log = opts.niche_slug_override or None
    try:
        db.table("import_log").insert({
            "filename": filename,
            "total_rows": total_rows,
            "imported": imported,
            "skipped_duplicate": skipped_duplicate,
            "skipped_invalid": skipped_invalid,
            "failed": failed,
            "niche_slug": niche_for_log,
            "field_mapping": opts.field_mapping,
            "options": {
                "normalize_niche": opts.normalize_niche,
                "skip_duplicates": opts.skip_duplicates,
                "default_pain_score": opts.default_pain_score,
                "state_override": opts.state_override,
            },
        }).execute()
    except Exception as e:
        logger.warning(f"[csv-import] Could not write to import_log: {e}")

    return {
        "status": "complete",
        "upload_id": upload_id,
        "filename": filename,
        "total_rows": total_rows,
        "imported": imported,
        "skipped_duplicate": skipped_duplicate,
        "skipped_invalid": skipped_invalid,
        "failed": failed,
        "skipped_total": skipped_total,
        "failed_rows": failed_rows[:100],  # cap response size
    }


# ── GET /csv/imports ──────────────────────────────────────────────────────────

@router.get("/csv/imports")
async def list_imports(db=Depends(get_db)):
    """Return last 50 imports from import_log."""
    res = (
        db.table("import_log")
        .select("id, filename, total_rows, imported, skipped_duplicate, skipped_invalid, failed, niche_slug, imported_at")
        .order("imported_at", desc=True)
        .limit(50)
        .execute()
    )
    return {"imports": res.data or [], "count": len(res.data or [])}


# ── GET /csv/imports/{import_id} ──────────────────────────────────────────────

@router.get("/csv/imports/{import_id}")
async def get_import(import_id: str, db=Depends(get_db)):
    """Return full details for a single import log entry."""
    res = db.table("import_log").select("*").eq("id", import_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Import '{import_id}' not found.")
    return res.data[0]


# ── DELETE /csv/uploads/{upload_id} ──────────────────────────────────────────

@router.delete("/csv/uploads/{upload_id}")
async def delete_upload(upload_id: str, db=Depends(get_db)):
    """Remove an upload session from in-memory store."""
    if upload_id not in _upload_store:
        raise HTTPException(status_code=404, detail=f"Upload ID '{upload_id}' not found.")
    del _upload_store[upload_id]
    return {"status": "deleted", "upload_id": upload_id}
