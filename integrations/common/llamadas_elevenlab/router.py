# -*- coding: utf-8 -*-
"""
Router de integraciones ElevenLabs/Level Up (post-llamada + eventos).
- /levelup/post-call: recibe webhook de Level Up/ElevenLabs al terminar llamada
  -> detecta bot por número o por 'bot_id' opcional del payload
  -> arma email HTML completo con datos de la llamada y lo envía a los destinatarios del bot
  -> dispara follow-up opcional (send-link) si está configurado en el JSON del bot
- /levelup/events: recibe otros eventos/transcripciones y manda email
"""

import os
import re
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from integrations.common.registry import registry
from integrations.common.mailer import render_email, send_email_html

router = APIRouter()

PROJECT_TAG = os.getenv("PROJECT_TAG", "INH-Integrations")
SEND_LINK_FALLBACK = os.getenv("SEND_LINK_URL", "https://multi-bot-inteligente-v1.onrender.com/actions/send-link")

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def normalize_phone(raw: str, region: str = "US") -> Optional[str]:
    s = (raw or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\+\d{8,15}", s):
        return s
    digits = re.sub(r"\D", "", s)
    if region.upper() == "US":
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        if len(digits) == 10:
            return f"+1{digits}"
    if 8 <= len(digits) <= 15:
        return f"+{digits}"
    return None

def flatten_strings(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from flatten_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from flatten_strings(v)
    else:
        if obj is not None:
            yield str(obj)

def detect_phone(payload: Dict[str, Any]) -> Optional[str]:
    # pistas comunes
    keys = ["from", "caller", "caller_number", "ani", "customer_phone", "user_phone", "contact", "phone", "source"]
    for k in keys:
        if k in payload and payload[k]:
            n = normalize_phone(str(payload[k]))
            if n: return n
    # fallback: buscar en todo
    for v in flatten_strings(payload):
        n = normalize_phone(v)
        if n: return n
    return None

def resolve_bot(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Resuelve bot por:
      1) bot_id (si viene en payload)
      2) número (to/recipient) si viene
      3) heurística por teléfono detectado
      4) primero de la lista (fallback)
    """
    bot_id = payload.get("bot_id") or payload.get("agent_id") or payload.get("bot")  # por si LevelUp manda algo
    if bot_id and registry.get(bot_id):
        return registry.get(bot_id)

    # intentamos por 'to'/'recipient'/'called_number'
    to_like = payload.get("to") or payload.get("recipient") or payload.get("called_number")
    if to_like:
        bot = registry.find_by_number(str(to_like))
        if bot: return bot

    # heurística por teléfono detectado
    phone = detect_phone(payload)
    if phone:
        # Si el teléfono detectado es del BOT (To) quizá venga con prefijo 'whatsapp:' en payloads
        bot = registry.find_by_number(phone) or registry.find_by_number(f"whatsapp:{phone}")
        if bot: return bot

    # fallback
    bots = registry.all()
    return bots[0] if bots else {}

def get_bot_email_config(bot: Dict[str, Any]) -> Dict[str, Any]:
    email_cfg = (bot.get("email") or {})
    to = email_cfg.get("to") or []
    if isinstance(to, str):
        to = [s.strip() for s in to.split(",") if s.strip()]
    subject_prefix = email_cfg.get("subject_prefix") or f"[{PROJECT_TAG}]"
    sender = email_cfg.get("from") or os.getenv("MAIL_FROM", "no-reply@example.com")
    return {"to": to, "subject_prefix": subject_prefix, "from": sender}

def get_bot_followups(bot: Dict[str, Any]) -> Dict[str, Any]:
    fu = bot.get("followups") or {}
    return {
        "send_link_url": fu.get("send_link_url") or SEND_LINK_FALLBACK,
        "bot": fu.get("bot"),
        "channel": fu.get("channel", "sms"),
        "link": fu.get("link")
    }

@router.post("/levelup/post-call")
async def post_call(request: Request):
    # 1) Parse payload
    try:
        data = await request.json()
    except Exception:
        form = await request.form()
        data = {k: form.get(k) for k in form.keys()}

    # 2) Resolver bot y configuración
    bot = resolve_bot(data)
    email_cfg = get_bot_email_config(bot)
    followups = get_bot_followups(bot)

    # 3) Extraer campos estándar (flexibles a la forma de Level Up)
    conv_id = data.get("conversation_id") or data.get("session_id") or data.get("id") or "unknown"
    from_num = normalize_phone(data.get("from") or data.get("caller") or data.get("ani") or "")
    to_num = normalize_phone(data.get("to") or data.get("recipient") or data.get("called_number") or "")
    duration = data.get("duration") or data.get("call_duration") or data.get("duration_seconds")
    status = data.get("status") or "completed"
    recording_url = data.get("recording_url") or data.get("recordingUrl") or ""
    transcript = data.get("transcript") or data.get("summary") or ""
    agent_name = bot.get("display_name") or data.get("agent_name") or "Agente"

    # 4) Email HTML (plantilla)
    html = render_email(
        "notification_call.html",
        {
            "ts": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "bot": bot,
            "agent_name": agent_name,
            "conv_id": conv_id,
            "from_num": from_num or "-",
            "to_num": to_num or "-",
            "status": status,
            "duration": duration or "-",
            "recording_url": recording_url or "-",
            "transcript": transcript,
            "raw_json": json.dumps(data, ensure_ascii=False, indent=2)
        }
    )
    subject = f"{email_cfg['subject_prefix']} CALL {status} | {agent_name} | {conv_id}"

    # 5) Enviar email
    email_res = send_email_html(
        subject=subject,
        html=html,
        to=email_cfg["to"],
        text_fallback=f"CALL {status} {conv_id}\nFrom: {from_num}\nTo: {to_num}\nDur: {duration}\nRec: {recording_url}\n\n{transcript}"
    )

    # 6) Follow-up opcional (SMS/WA con link)
    follow_result = None
    if followups.get("send_link_url") and followups.get("bot") and followups.get("link") and from_num:
        payload = {
            "bot": followups["bot"],
            "phone": from_num,
            "channel": followups.get("channel", "sms"),
            "link": followups["link"]
        }
        try:
            r = requests.post(followups["send_link_url"], json=payload, timeout=8)
            follow_result = {"status": r.status_code, "ok": r.ok, "text": r.text[:300]}
        except Exception as e:
            follow_result = {"ok": False, "error": str(e)}

    return JSONResponse({
        "ok": True,
        "email": email_res,
        "follow_up": follow_result,
        "bot_id": bot.get("id"),
        "bot_display": bot.get("display_name")
    })

@router.post("/levelup/events")
async def levelup_events(request: Request):
    # Recibe cualquier evento o transcript parcial y lo reenvía a email
    try:
        data = await request.json()
    except Exception:
        form = await request.form()
        data = {k: form.get(k) for k in form.keys()}

    bot = resolve_bot(data)
    email_cfg = get_bot_email_config(bot)
    event_type = data.get("type") or "event"
    conv_id = data.get("conversation_id") or data.get("session_id") or data.get("id") or "unknown"
    agent_name = bot.get("display_name") or data.get("agent_name") or "Agente"
    transcript = data.get("transcript") or data.get("summary") or ""

    html = render_email(
        "notification_call.html",
        {
            "ts": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "bot": bot,
            "agent_name": agent_name + f" / {event_type}",
            "conv_id": conv_id,
            "from_num": "-",
            "to_num": "-",
            "status": event_type,
            "duration": "-",
            "recording_url": "-",
            "transcript": transcript,
            "raw_json": json.dumps(data, ensure_ascii=False, indent=2)
        }
    )
    subject = f"{email_cfg['subject_prefix']} EVENT {event_type} | {agent_name} | {conv_id}"
    email_res = send_email_html(subject=subject, html=html, to=email_cfg["to"])

    return JSONResponse({"ok": True, "email": email_res, "bot_id": bot.get("id")})

