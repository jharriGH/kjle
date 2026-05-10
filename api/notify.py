"""
KJLE — POST /notify endpoint (empire-wide notifications)

Sends SMS via Twilio + email via Resend to Jim. Any empire service
calls this rather than building its own notify pipeline. Auth is the
shared empire x-brain-key header (verified in main.py).
"""

import os
import logging
from typing import Literal, Optional
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

JIM_PHONE = "+15622436177"
JIM_EMAIL = "jim@developingriches.com"
EMAIL_FROM = "alerts@kjle.com"

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
RESEND_API_URL = "https://api.resend.com/emails"


class NotifyRequest(BaseModel):
    severity: Literal["info", "warn", "critical"]
    message: str = Field(..., max_length=500)
    channel: Literal["sms", "email", "both"] = "both"


async def send_sms(message: str, severity: str) -> dict:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    sender = os.environ.get("TWILIO_SENDER")

    if not (sid and token and sender):
        return {"ok": False, "error": "twilio_creds_missing"}

    body = f"[{severity.upper()}] {message[:140]}"
    url = f"{TWILIO_API_BASE}/Accounts/{sid}/Messages.json"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                auth=(sid, token),
                data={"From": sender, "To": JIM_PHONE, "Body": body},
            )
        if resp.status_code >= 400:
            return {"ok": False, "error": f"twilio_http_{resp.status_code}", "detail": resp.text[:300]}
        data = resp.json()
        return {
            "ok": True,
            "sms_sid": data.get("sid"),
            "status": data.get("status"),
            "to": data.get("to"),
        }
    except Exception as e:
        logger.exception("send_sms failed")
        return {"ok": False, "error": "twilio_exception", "detail": str(e)[:300]}


async def send_email(message: str, severity: str) -> dict:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return {"ok": False, "error": "resend_key_missing"}

    severity_color = {
        "info": "#3b82f6",
        "warn": "#f59e0b",
        "critical": "#ef4444",
    }.get(severity, "#6b7280")

    subject = f"[{severity.upper()}] {message[:60]}"
    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:600px;margin:0 auto;padding:24px;">
      <div style="border-left:4px solid {severity_color};padding-left:16px;margin-bottom:16px;">
        <div style="text-transform:uppercase;font-size:12px;letter-spacing:1px;color:{severity_color};font-weight:700;">
          {severity}
        </div>
        <h2 style="margin:4px 0 0 0;font-size:18px;color:#111;">KJLE Notification</h2>
      </div>
      <p style="font-size:15px;line-height:1.5;color:#222;white-space:pre-wrap;">{message}</p>
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
      <p style="font-size:12px;color:#6b7280;">
        Sent from KJLE /notify endpoint · {datetime.now(timezone.utc).isoformat()}
      </p>
    </div>
    """.strip()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": EMAIL_FROM,
                    "to": [JIM_EMAIL],
                    "subject": subject,
                    "html": html,
                },
            )
        if resp.status_code >= 400:
            return {"ok": False, "error": f"resend_http_{resp.status_code}", "detail": resp.text[:300]}
        data = resp.json()
        return {
            "ok": True,
            "email_id": data.get("id"),
            "to": JIM_EMAIL,
        }
    except Exception as e:
        logger.exception("send_email failed")
        return {"ok": False, "error": "resend_exception", "detail": str(e)[:300]}


async def notify(req: NotifyRequest):
    # Critical severity always fans out to both channels regardless of request.
    channel = "both" if req.severity == "critical" else req.channel

    sms_result: Optional[dict] = None
    email_result: Optional[dict] = None

    if channel in ("sms", "both"):
        sms_result = await send_sms(req.message, req.severity)
    if channel in ("email", "both"):
        email_result = await send_email(req.message, req.severity)

    response = {
        "severity": req.severity,
        "channel": channel,
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    errors = {}

    if sms_result is not None:
        if sms_result.get("ok"):
            response["sms_sid"] = sms_result.get("sms_sid")
            response["sms_status"] = sms_result.get("status")
        else:
            errors["sms"] = sms_result

    if email_result is not None:
        if email_result.get("ok"):
            response["email_id"] = email_result.get("email_id")
        else:
            errors["email"] = email_result

    if errors:
        response["errors"] = errors
        # Partial success → 207 Multi-Status; total failure → 502.
        any_ok = ("sms_sid" in response) or ("email_id" in response)
        return JSONResponse(status_code=207 if any_ok else 502, content=response)

    return response
