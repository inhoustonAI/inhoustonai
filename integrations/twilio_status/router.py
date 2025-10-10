# -*- coding: utf-8 -*-
"""
Callbacks de estado de Twilio:
- POST /twilio/voice/status      -> estados de llamada (ringing, in-progress, completed...)
- POST /twilio/messaging/status  -> estados de SMS/WhatsApp (queued, sent, delivered, failed...)

Ambos envían email HTML usando la misma plantilla y config por bot (bots/*.json).
El bot se resuelve por el número 'To' del callback.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
from typing import Dict, Any

from integrations.common.registry import registry
from integrations.common.mailer import render_email, send_email_html

router = APIRouter()

def ts():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

def email_cfg_for_bot(bot: Dict[str, Any]):
    email_cfg = bot.get("email") or {}
    to = email_cfg.get("to") or []
    if isinstance(to, str):
        to = [s.strip() for s in to.split(",") if s.strip()]
    subject_prefix = email_cfg.get("subject_prefix") or "[Twilio]"
    return to, subject_prefix, email_cfg.get("from")

def find_bot_by_to(to_number: str):
    if not to_number:
        return None
    # Twilio manda "whatsapp:+1..." o "+1..."
    bot = registry.find_by_number(to_number) or registry.find_by_number(f"whatsapp:{to_number}")
    return bot

@router.post("/twilio/voice/status")
async def twilio_voice_status(request: Request):
    # Twilio envía x-www-form-urlencoded
    form = await request.form()
    data = {k: form.get(k) for k in form.keys()}

    to_num = (data.get("To") or "").strip()
    from_num = (data.get("From") or "").strip()
    status = (data.get("CallStatus") or data.get("CallStatusCallbackEvent") or "unknown").strip()
    duration = data.get("CallDuration") or data.get("DialCallDuration") or "-"
    call_sid = data.get("CallSid") or "-"
    recording_url = data.get("RecordingUrl") or data.get("RecordingUrl0") or "-"

    bot = find_bot_by_to(to_num) or (registry.all()[0] if registry.all() else {})
    to, prefix, _ = email_cfg_for_bot(bot)
    agent_name = (bot.get("display_name") if bot else "Bot") + " / Twilio Voice"

    html = render_email(
        "notification_call.html",
        {
            "ts": ts(),
            "bot": bot,
            "agent_name": agent_name,
            "conv_id": call_sid,
            "from_num": from_num or "-",
            "to_num": to_num or "-",
            "status": status,
            "duration": duration,
            "recording_url": recording_url or "-",
            "transcript": "",
            "raw_json": str(dict(data))
        }
    )
    subject = f"{prefix} VOICE {status} | {agent_name} | {call_sid}"
    email_res = send_email_html(subject=subject, html=html, to=to)

    return JSONResponse({"ok": True, "email": email_res})

@router.post("/twilio/messaging/status")
async def twilio_msg_status(request: Request):
    form = await request.form()
    data = {k: form.get(k) for k in form.keys()}

    to_num = (data.get("To") or "").strip()
    from_num = (data.get("From") or "").strip()
    status = (data.get("MessageStatus") or "unknown").strip()
    msg_sid = data.get("MessageSid") or "-"
    error = data.get("ErrorCode") or data.get("ErrorMessage") or "-"

    bot = find_bot_by_to(to_num) or (registry.all()[0] if registry.all() else {})
    to, prefix, _ = email_cfg_for_bot(bot)
    agent_name = (bot.get("display_name") if bot else "Bot") + " / Twilio Msg"

    html = render_email(
        "notification_call.html",
        {
            "ts": ts(),
            "bot": bot,
            "agent_name": agent_name,
            "conv_id": msg_sid,
            "from_num": from_num or "-",
            "to_num": to_num or "-",
            "status": f"{status} (err: {error})",
            "duration": "-",
            "recording_url": "-",
            "transcript": "",
            "raw_json": str(dict(data))
        }
    )
    subject = f"{prefix} MSG {status} | {agent_name} | {msg_sid}"
    email_res = send_email_html(subject=subject, html=html, to=to)

    return JSONResponse({"ok": True, "email": email_res})
