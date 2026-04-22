"""
KJLE — Resend transactional email sender
File: api/lib/email_sender.py

Thin wrapper around Resend's REST API (https://resend.com/docs/api-reference).
Uses httpx (already a project dep) — no new package required.

Sender domain: freereputationaudits.com (highest warmup — 96.8).
Requires domain DNS verification in Resend dashboard before sends will succeed.

Env var required: RESEND_API_KEY (set on Render).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_FROM   = "KJLE Reports <kjle@kjreportz.com>"


def is_configured() -> bool:
    """True if RESEND_API_KEY is present in the environment."""
    return bool(os.environ.get("RESEND_API_KEY", "").strip())


async def send_email(
    to: str,
    subject: str,
    body_text: str,
    from_addr: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> dict:
    """
    Sends a plaintext email via Resend.

    Returns: {"ok": bool, "id": str|None, "error": str|None, "skipped": bool}

    Never raises — callers treat the result dict as the source of truth.
    If RESEND_API_KEY is unset, returns ok=False, skipped=True and logs a warning
    (used during local dev / pre-deploy).
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        logger.warning("email_sender: RESEND_API_KEY not set — email skipped")
        return {"ok": False, "id": None, "error": "RESEND_API_KEY not set", "skipped": True}

    payload = {
        "from":    from_addr or DEFAULT_FROM,
        "to":      [to] if isinstance(to, str) else list(to),
        "subject": subject,
        "text":    body_text,
    }
    if reply_to:
        payload["reply_to"] = reply_to

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
        if r.status_code in (200, 201):
            data = r.json() if r.content else {}
            return {"ok": True, "id": data.get("id"), "error": None, "skipped": False}
        err = f"HTTP {r.status_code}: {r.text[:300]}"
        logger.error(f"email_sender: send failed — {err}")
        return {"ok": False, "id": None, "error": err, "skipped": False}
    except Exception as e:
        logger.error(f"email_sender: exception sending mail: {e}")
        return {"ok": False, "id": None, "error": str(e), "skipped": False}
