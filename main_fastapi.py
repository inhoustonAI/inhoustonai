# -*- coding: utf-8 -*-
"""
INH Integrations (FastAPI) — Level Up first

Arquitectura:
- El AGENTE (voz / chat) lo maneja **Level Up** (ElevenLabs).
- Este servicio solo maneja **integraciones** y **notificaciones por email**.

Endpoints:
- GET  /health
- POST /twilio/voice/status         -> Twilio Voice Status Callback (inicio/fin de llamada)
- POST /twilio/messaging/status     -> Twilio Messaging Status Callback (SMS/WhatsApp)
- POST /levelup/events              -> Webhook genérico desde Level Up (transcripciones / eventos)

ENV requeridas/útiles:
  # Email (SMTP)
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=__SET__
  SMTP_PASS=__SET__
  MAIL_FROM="In Houston AI <no-reply@inhoustontexas.us>"
  MAIL_TO="ops@inhoustontexas.us,sundin@..."   # múltiples separados por coma

  # Opcional: tagging del proyecto
  PROJECT_TAG=INH-MultiAgents
"""

import os
import pathlib
import json
import smtplib
from typing import Any, Dict, Optional, List
from email.message import EmailMessage
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from dotenv import load_dotenv

ROOT_DIR = pathlib.Path(__file__).resolve().parent
load_dotenv(dotenv_path=ROOT_DIR / ".env")

# --------- Config ---------
FLASK_ENV = os.getenv("FLASK_ENV", "development")
PORT = int(os.getenv("PORT", "5000"))
PROJECT_TAG = os.getenv("PROJECT_TAG", "INH")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
MAIL_FROM = os.getenv("MAIL_FROM", "no-reply@example.com")
MAIL_TO = [e.strip() for e in os.getenv("MAIL_TO", "").split(",") if e.strip()]

# --------- App ---------
app = FastAPI(title="INH Integrations (FastAPI)")

# --------- Helpers ---------
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def pretty(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return str(obj)

def send_email(subject: str, text: str, html: Optional[str] = None,
               to: Optional[List[str]] = None, attachments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Envía email vía SMTP (TLS).
    attachments: lista de dicts: {"filename": "...", "content": bytes, "mime": "audio/mpeg"}
    """
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS or not MAIL_FROM:
        return {"ok": False, "reason": "SMTP env missing"}

    recipients = to or MAIL_TO
    if not recipients:
        return {"ok": False, "reason": "No recipients configured (MAIL_TO)"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.set_content(text or "")

    if html:
        msg.add_alternative(html, subtype="html")

    for att in attachments or []:
        msg.add_attachment(att["content"], maintype=(att.get("mime","application/octet-stream").split("/")[0]),
                           subtype=(att.get("mime","application/octet-stream").split("/")[1]),
                           filename=att.get("filename","file.bin"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)

    return {"ok": True, "sent_to": recipients}

# --------- Routes ---------
@app.get("/health")
def health():
    return JSONResponse({
        "status": "ok",
        "env": FLASK_ENV,
        "ts": now_iso(),
        "mail_to": MAIL_TO,
        "project": PROJECT_TAG
    })

# -----------------------------
# Twilio Voice: Status Callback
# Configurar en Twilio Number:
#   Voice -> "Call status changes" -> POST https://<tu-app>/twilio/voice/status
# -----------------------------
@app.post("/twilio/voice/status")
async def twilio_voice_status(request: Request):
    form = await request.form()
    # Campos comunes de Twilio Voice status callback
    payload = {k: form.get(k) for k in form.keys()}  # dict plano
    call_sid = payload.get("CallSid")
    status = payload.get("CallStatus")         # queued | ringing | in-progress | completed | busy | failed | no-answer
    from_ = payload.get("From")
    to_ = payload.get("To")
    duration = payload.get("CallDuration")     # en segundos (si completed)
    recording_url = payload.get("RecordingUrl") or payload.get("RecordingUrl0")  # si existe
    account = payload.get("AccountSid")

    subject = f"[{PROJECT_TAG}] CALL {status} | {to_} <- {from_} | {call_sid}"
    lines = [
        f"Project: {PROJECT_TAG}",
        f"Time: {now_iso()}",
        f"Status: {status}",
        f"From: {from_}",
        f"To: {to_}",
        f"CallSid: {call_sid}",
        f"AccountSid: {account}",
        f"Duration(s): {duration or '-'}",
        f"RecordingUrl: {recording_url or '-'}",
        "",
        "Raw form:",
        pretty(payload),
    ]
    res = send_email(subject, "\n".join(lines))
    return JSONResponse({"ok": True, "email": res})

# -----------------------------
# Twilio Messaging: Status Callback
# Configurar:
#   - En el Messaging Service (recomendado): Status Callback URL -> POST https://<tu-app>/twilio/messaging/status
#     (o en el número individual si no usas service)
# Nota: esto reporta estados (sent, delivered, failed). Para INBOUND content,
#       el agente Level Up debe avisarnos vía /levelup/events.
# -----------------------------
@app.post("/twilio/messaging/status")
async def twilio_msg_status(request: Request):
    form = await request.form()
    payload = {k: form.get(k) for k in form.keys()}
    msg_sid = payload.get("MessageSid")
    status = payload.get("MessageStatus")      # accepted | queued | sent | delivered | undelivered | failed | read (WA)
    from_ = payload.get("From")
    to_ = payload.get("To")
    body = payload.get("Body")  # no siempre viene en status callback

    subject = f"[{PROJECT_TAG}] MSG {status} | {to_} <- {from_} | {msg_sid}"
    lines = [
        f"Project: {PROJECT_TAG}",
        f"Time: {now_iso()}",
        f"Status: {status}",
        f"From: {from_}",
        f"To: {to_}",
        f"MessageSid: {msg_sid}",
        f"Body (if present): {body or '-'}",
        "",
        "Raw form:",
        pretty(payload),
    ]
    res = send_email(subject, "\n".join(lines))
    return JSONResponse({"ok": True, "email": res})

# -----------------------------
# Level Up: Webhook genérico
# Configurar en Level Up (ElevenLabs):
#   - Webhook URL -> POST https://<tu-app>/levelup/events
#   - Enviar transcripciones/turnos/metadata de conversación
# Este endpoint no asume un esquema fijo: manda EMAIL con el JSON crudo.
# Si el JSON trae campos típicos (conversation_id, channel, transcript, messages),
# los resalta en el asunto/cuerpo.
# -----------------------------
@app.post("/levelup/events")
async def levelup_events(request: Request):
    try:
        data = await request.json()
    except Exception:
        # Si envían form-url-encoded, intenta convertir
        form = await request.form()
        data = {k: form.get(k) for k in form.keys()}

    # Heurísticas para el asunto
    conv_id = data.get("conversation_id") or data.get("session_id") or data.get("id") or "unknown"
    channel = data.get("channel") or data.get("source") or "voice/chat"
    event_type = data.get("type") or "event"
    title = data.get("title") or ""
    # Posible transcript directo
    transcript = data.get("transcript") or data.get("summary") or ""

    subject = f"[{PROJECT_TAG}] LEVELUP {event_type} | {channel} | {conv_id}"
    if title:
        subject += f" | {title}"

    # Construir cuerpo
    lines = [
        f"Project: {PROJECT_TAG}",
        f"Time: {now_iso()}",
        f"Event Type: {event_type}",
        f"Channel: {channel}",
        f"Conversation ID: {conv_id}",
        "",
    ]
    if transcript:
        lines += ["--- Transcript / Summary ---", transcript, ""]

    lines += ["--- Raw JSON ---", pretty(data)]

    res = send_email(subject, "\n".join(lines))
    return JSONResponse({"ok": True, "email": res})

# -----------------------------
# Run local
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
