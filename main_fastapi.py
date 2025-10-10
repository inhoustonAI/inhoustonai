# -*- coding: utf-8 -*-
"""
Main FastAPI para base multi-agentes (Twilio + ElevenLabs)
- /health                    -> estado del servicio
- /bots                      -> lista de bots cargados desde ./bots/*.json
- POST /twilio/sms           -> webhook SMS/WhatsApp (Twilio)
- POST /twilio/voice         -> webhook Voice (Twilio, <Play> a /tts)
- GET  /tts?text=...         -> genera MP3 con ElevenLabs (audio/mpeg)

Listo para correr con uvicorn o gunicorn+uvicorn workers.
"""
import json
import os
import pathlib
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# Twilio TwiML helpers (no necesitas el client para responder webhooks)
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

# -----------------------------
# Configuración y utilidades
# -----------------------------
ROOT_DIR = pathlib.Path(__file__).resolve().parent
load_dotenv(dotenv_path=ROOT_DIR / ".env")  # si existe .env en el root

FLASK_ENV = os.getenv("FLASK_ENV", "development")  # mantenemos la var por compat.
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

def _load_bots(bots_dir: str) -> Dict[str, Dict[str, Any]]:
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
            # No reventar por un JSON mal formado
            print(f"[WARN] No se pudo cargar {f.name}: {e}")
    return data

BOTS_REGISTRY = _load_bots(BOTS_DIR)

def reload_bots() -> None:
    global BOTS_REGISTRY
    BOTS_REGISTRY = _load_bots(BOTS_DIR)

def find_bot_by_number(number: str) -> Optional[Dict[str, Any]]:
    """Busca el bot cuyo número (voice o whatsapp:+...) coincida con 'number'."""
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
    """
    Twilio necesita URL absolutas en <Play>. Construimos base con host+proto.
    """
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.hostname
    if not path.startswith("/"):
        path = "/" + path
    return f"{proto}://{host}{path}"

# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title="INH MultiAgents (FastAPI)")

# -----------------------------
# Modelos
# -----------------------------
class TTSQuery(BaseModel):
    text: str
    voice_id: Optional[str] = None
    model_id: Optional[str] = "eleven_turbo_v2"

# -----------------------------
# Rutas básicas
# -----------------------------
@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "env": FLASK_ENV,
            "bots_count": len(BOTS_REGISTRY),
            "elevenlabs": bool(ELEVENLABS_API_KEY),
        }
    )

@app.get("/bots")
def list_bots() -> JSONResponse:
    return JSONResponse({"bots": list(BOTS_REGISTRY.values())})

@app.post("/bots/reload")
def bots_reload() -> JSONResponse:
    reload_bots()
    return JSONResponse({"reloaded": True, "bots_count": len(BOTS_REGISTRY)})

# -----------------------------
# Webhooks Twilio (SMS/WhatsApp)
# -----------------------------
@app.post("/twilio/sms")
async def twilio_sms(request: Request) -> Response:
    form = await request.form()
    from_number = str(form.get("From", "")).strip()
    body = str(form.get("Body", "")).strip() or "Hola 👋"
    bot = find_bot_by_number(from_number)

    label = bot["display_name"] if bot and bot.get("display_name") else "Agente"
    reply = f"[{label}] Recibido: {body}"

    # Puedes enrutar por bot['prompt'] o NLU más adelante.
    twiml = MessagingResponse()
    twiml.message(reply)
    xml = str(twiml)
    return Response(content=xml, media_type="application/xml")

# -----------------------------
# Webhook Twilio (Voice)
# -----------------------------
@app.post("/twilio/voice")
async def twilio_voice(request: Request) -> Response:
    form = await request.form()
    from_number = str(form.get("From", "")).strip()
    bot = find_bot_by_number(from_number)

    # Texto base para TTS
    display = bot["display_name"] if bot and bot.get("display_name") else "Agente"
    prompt = (bot.get("prompt") if bot else None) or "Hola, ¿en qué puedo ayudarte?"
    text = f"Hola, estás hablando con {display}. {prompt}"

    # Si no hay API key de ElevenLabs, degradamos a <Say>
    vr = VoiceResponse()
    if not ELEVENLABS_API_KEY:
        vr.say(text, language="es-ES")
        return Response(content=str(vr), media_type="application/xml")

    # Construimos URL absoluta a /tts
    voice_id = ""
    if bot and bot.get("elevenlabs", {}).get("voice_id"):
        voice_id = bot["elevenlabs"]["voice_id"]
    elif DEFAULT_VOICE_ID:
        voice_id = DEFAULT_VOICE_ID

    # Ojo: encode params vía query string
    from urllib.parse import urlencode, quote_plus
    qs = urlencode({"text": text, "voice_id": voice_id}, quote_via=quote_plus)
    tts_url = make_absolute_url(request, f"/tts?{qs}")

    # Twilio reproducirá el MP3 directamente
    vr.play(tts_url)
    return Response(content=str(vr), media_type="application/xml")

# -----------------------------
# TTS (ElevenLabs) -> MP3
# -----------------------------
def elevenlabs_tts_bytes(text: str, voice_id: str, model_id: str = "eleven_turbo_v2") -> bytes:
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("Falta ELEVENLABS_API_KEY en el entorno")
    if not voice_id:
        raise RuntimeError("Falta voice_id (usa DEFAULT_VOICE_ID o define en el bot)")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.7},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.content

@app.get("/tts")
def tts_get(text: str, voice_id: Optional[str] = None, model_id: str = "eleven_turbo_v2"):
    """Devuelve audio/mpeg para que Twilio <Play> lo consuma."""
    v_id = voice_id or DEFAULT_VOICE_ID
    try:
        mp3 = elevenlabs_tts_bytes(text=text, voice_id=v_id, model_id=model_id)
    except Exception as e:
        # Si algo falla, devolvemos texto plano para depurar rápido
        return PlainTextResponse(f"TTS error: {e}", status_code=500)
    return StreamingResponse(iter([mp3]), media_type="audio/mpeg")

# -----------------------------
# Raíz (útil para verificar despliegue)
# -----------------------------
@app.get("/")
def root() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "INH MultiAgents (FastAPI)"})


# -----------------------------
# Punto de entrada local
# -----------------------------
if __name__ == "__main__":
    # Ejecutar local: python main_fastapi.py
    import uvicorn
    uvicorn.run("main_fastapi.py:app", host="0.0.0.0", port=PORT, reload=True)
