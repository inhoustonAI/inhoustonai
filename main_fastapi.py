# -*- coding: utf-8 -*-
"""
Main FastAPI multi-agentes (Twilio + ElevenLabs)
Endpoints:
- GET  /health                  -> estado del servicio
- GET  /bots                    -> lista de bots (desde ./bots/*.json)
- POST /bots/reload             -> recargar bots
- POST /twilio/sms              -> webhook SMS/WhatsApp (Twilio)
- POST /twilio/voice            -> webhook Voice (Twilio) con <Play> a /tts
- GET  /tts?text=..&voice_id=.. -> genera MP3 (audio/mpeg) con ElevenLabs
"""
import json
import os
import pathlib
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from dotenv import load_dotenv
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

ROOT_DIR = pathlib.Path(__file__).resolve().parent
load_dotenv(dotenv_path=ROOT_DIR / ".env")

FLASK_ENV = os.getenv("FLASK_ENV", "development")
PORT = int(os.getenv("PORT", "5000"))

# Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")
TWILIO_VOICE_FROM = os.getenv("TWILIO_VOICE_FROM", "")

# ElevenLabs
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
DEFAULT_VOICE_ID = os.getenv("DEFAULT_VOICE_ID", "")

# Bots
BOTS_DIR = os.getenv("BOTS_DIR", str(ROOT_DIR / "bots"))

# -----------------------------
# Utils: bots
# -----------------------------
def _load_bots(bots_dir: str):
    data: Dict[str, Dict[str, Any]] = {}
    p = pathlib.Path(bots_dir)
    if not p.is_dir():
        return data
    for f in p.glob("*.json"):
        try:
            with f.open("r", encoding="utf-8") as fh:
                obj = json.load(fh)
            bot_id = obj.get("id") or f.stem
            data[bot_id] = obj
        except Exception as e:
            print(f"[WARN] No se pudo cargar {f.name}: {e}")
    return data

BOTS_REGISTRY = _load_bots(BOTS_DIR)

def reload_bots():
    global BOTS_REGISTRY
    BOTS_REGISTRY = _load_bots(BOTS_DIR)

def find_bot_by_number(number: str) -> Optional[Dict[str, Any]]:
    if not number:
        return None
    n = str(number).strip().lower()
    for bot in BOTS_REGISTRY.values():
        nums = bot.get("phone_numbers", {})
        voice = str(nums.get("voice", "")).lower()
        wa = str(nums.get("whatsapp", "")).lower()
        if n == voice or n == wa:
            return bot
    return None

def make_absolute_url(request: Request, path: str) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.hostname
    if not path.startswith("/"):
        path = "/" + path
    return f"{proto}://{host}{path}"

def bot_voice_id(bot: Optional[Dict[str, Any]]) -> str:
    if bot and bot.get("elevenlabs", {}).get("voice_id"):
        return bot["elevenlabs"]["voice_id"]
    return DEFAULT_VOICE_ID

def is_whatsapp_num(num: str) -> bool:
    return str(num).lower().startswith("whatsapp:")

# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title="INH MultiAgents (FastAPI)")

@app.get("/")
def root():
    return JSONResponse({"ok": True, "service": "INH MultiAgents (FastAPI)"})


# -----------------------------
# Básicos
# -----------------------------
@app.get("/health")
def health():
    return JSONResponse({
        "status": "ok",
        "env": FLASK_ENV,
        "bots_count": len(BOTS_REGISTRY),
        "elevenlabs": bool(ELEVENLABS_API_KEY)
    })

@app.get("/bots")
def list_bots():
    return JSONResponse({"bots": list(BOTS_REGISTRY.values())})

@app.post("/bots/reload")
def bots_reload():
    reload_bots()
    return JSONResponse({"reloaded": True, "bots_count": len(BOTS_REGISTRY)})


# -----------------------------
# Webhook: SMS/WhatsApp
# - Responde texto
# - Si hay ElevenLabs y voice_id, adjunta MP3 como Media (WhatsApp/MMS)
# -----------------------------
@app.post("/twilio/sms")
async def twilio_sms(request: Request) -> Response:
    form = await request.form()
    from_number = str(form.get("From", "")).strip()
    body = (str(form.get("Body", "")).strip()) or "Hola 👋"
    bot = find_bot_by_number(from_number)

    label = bot["display_name"] if bot and bot.get("display_name") else "Agente"
    prompt = (bot.get("prompt") if bot else None) or "¿En qué puedo ayudarte?"

    # Mensaje base
    reply_text = f"[{label}] Recibido: {body}\n{prompt}"

    twiml = MessagingResponse()
    msg = twiml.message(reply_text)

    # Adjuntar audio si podemos generar TTS (WhatsApp/MMS acepta <Media>)
    v_id = bot_voice_id(bot)
    if ELEVENLABS_API_KEY and v_id:
        # Audio diciendo algo útil (eco del mensaje + mini saludo)
        tts_text = f"{label} confirma: recibí tu mensaje: {body}."
        from urllib.parse import urlencode, quote_plus
        qs = urlencode({"text": tts_text, "voice_id": v_id}, quote_via=quote_plus)
        media_url = make_absolute_url(request, f"/tts?{qs}")
        # Solo agregamos media en WhatsApp o MMS
        if is_whatsapp_num(from_number):
            msg.media(media_url)

    return Response(str(twiml), media_type="application/xml")


# -----------------------------
# Webhook: Voice
# - Si hay ElevenLabs -> <Play> /tts
# - Si no, fallback a <Say>
# -----------------------------
@app.post("/twilio/voice")
async def twilio_voice(request: Request) -> Response:
    form = await request.form()
    from_number = str(form.get("From", "")).strip()
    bot = find_bot_by_number(from_number)

    display = bot["display_name"] if bot and bot.get("display_name") else "Agente"
    prompt = (bot.get("prompt") if bot else None) or "Hola, ¿en qué puedo ayudarte?"
    text = f"Hola, estás hablando con {display}. {prompt}"

    vr = VoiceResponse()

    v_id = bot_voice_id(bot)
    if ELEVENLABS_API_KEY and v_id:
        from urllib.parse import urlencode, quote_plus
        qs = urlencode({"text": text, "voice_id": v_id}, quote_via=quote_plus)
        tts_url = make_absolute_url(request, f"/tts?{qs}")
        vr.play(tts_url)
    else:
        vr.say(text, language="es-ES")

    return Response(str(vr), media_type="application/xml")


# -----------------------------
# TTS: ElevenLabs -> audio/mpeg
# -----------------------------
def elevenlabs_tts_bytes(text: str, voice_id: str, model_id: str = "eleven_turbo_v2") -> bytes:
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("Falta ELEVENLABS_API_KEY")
    if not voice_id:
        raise RuntimeError("Falta voice_id (DEFAULT_VOICE_ID o por bot)")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json"
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.7}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.content

@app.get("/tts")
def tts(text: str, voice_id: Optional[str] = None, model_id: str = "eleven_turbo_v2"):
    v_id = voice_id or DEFAULT_VOICE_ID
    try:
        mp3 = elevenlabs_tts_bytes(text=text, voice_id=v_id, model_id=model_id)
    except Exception as e:
        return PlainTextResponse(f"TTS error: {e}", status_code=500)
    return StreamingResponse(iter([mp3]), media_type="audio/mpeg")


# -----------------------------
# Run local (dev)
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
