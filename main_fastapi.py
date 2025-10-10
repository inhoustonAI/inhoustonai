# main_fastapi.py
# In Houston AI — Twilio ↔ OpenAI Realtime (Multibot) — Oct 2025
# - Barge-in real (cancela TTS al hablar el usuario)
# - μ-law 8 kHz / 20 ms end-to-end (sin resampling)
# - Keep-alive estable hacia Twilio con silencio μ-law
# - Matriz vía bots/<slug>.json (model, voice, system_prompt, first_message, realtime/vad)

import os
import json
import base64
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any

import websockets  # OpenAI Realtime WS client

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState, WebSocketDisconnect

# =========================
# CONFIG GLOBAL
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PORT = int(os.getenv("PORT", "8080"))

# Carpeta de bots y fallback
BOTS_DIR = Path(os.getenv("BOTS_DIR", "bots"))
DEFAULT_BOT = os.getenv("DEFAULT_BOT", "sundin")

# Mapa opcional: número "To" → slug
_TWILIO_BOT_MAP_RAW = os.getenv("TWILIO_BOT_MAP", "{}")
try:
    TWILIO_BOT_MAP: Dict[str, str] = json.loads(_TWILIO_BOT_MAP_RAW)
except Exception:
    TWILIO_BOT_MAP = {}

print(f"🧩 Using PORT: {PORT}")
print(f"🧩 BOTS_DIR: {BOTS_DIR.resolve()} (exists={BOTS_DIR.exists()})")
print(f"🧩 DEFAULT_BOT: {DEFAULT_BOT}")
print(f"🧩 TWILIO_BOT_MAP: {TWILIO_BOT_MAP}")

app = FastAPI(title="In Houston AI — Twilio Realtime Bridge (Multibot)")

# CORS para Twilio
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Utilidades de audio
# =========================
def ulaw_silence_b64(ms: int = 20) -> str:
    """Frame de silencio μ-law 8k (20 ms). μ-law silencio = 0xFF."""
    samples = 8_000 * ms // 1000
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")

# =========================
# Carga y normalización de bots JSON
# =========================
_JSON_CACHE: Dict[str, Dict[str, Any]] = {}

def _normalize_realtime(cfg: Dict[str, Any]) -> None:
    """
    Admite dos esquemas:
      A) cfg["realtime"] = {turn_detection, input_audio_format, output_audio_format}
      B) cfg["vad"] = {type, silence_ms, prefix_ms}  → se mapea a realtime.turn_detection
    """
    # Defaults
    cfg.setdefault("voice", "alloy")
    cfg.setdefault("temperature", 0.8)
    cfg.setdefault("system_prompt", "")
    cfg.setdefault("first_message", "")
    cfg.setdefault("model", "gpt-4o-realtime-preview")

    rt = cfg.get("realtime")
    vad = cfg.get("vad")

    # Base realtime dict
    if not isinstance(rt, dict):
        rt = {}
    # Mapear VAD heredado si existe
    if isinstance(vad, dict) and "type" in vad and "turn_detection" not in rt:
        # OpenAI Realtime espera {"type": "...", ...}; pasamos fields útiles como hints
        td = {"type": str(vad.get("type", "server_vad"))}
        # Algunos motores admiten umbrales/hints (no todos se usan, pero no estorban)
        if "silence_ms" in vad:
            td["silence_ms"] = int(vad["silence_ms"])
        if "prefix_ms" in vad:
            td["prefix_ms"] = int(vad["prefix_ms"])
        rt["turn_detection"] = td

    # Normalizaciones y defaults
    td = rt.get("turn_detection")
    if isinstance(td, str):
        td = {"type": td}
    if not isinstance(td, dict):
        td = {"type": "server_vad"}
    rt["turn_detection"] = td

    rt.setdefault("input_audio_format", "g711_ulaw")
    rt.setdefault("output_audio_format", "g711_ulaw")

    cfg["realtime"] = rt

def _load_bot_json(slug: str) -> Dict[str, Any]:
    """Carga bots/<slug>.json; si falla, intenta DEFAULT_BOT; si no, usa defaults seguros."""
    slug = (slug or "").strip().lower() or DEFAULT_BOT
    if slug in _JSON_CACHE:
        return _JSON_CACHE[slug]

    def read_json(s: str) -> Optional[Dict[str, Any]]:
        path = BOTS_DIR / f"{s}.json"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    cfg = read_json(slug)
    if cfg is None and slug != DEFAULT_BOT:
        cfg = read_json(DEFAULT_BOT)
    if cfg is None:
        cfg = {
            "system_prompt": (
                "Eres el asistente de voz de In Houston Texas. "
                "Hablas en español latino, cálido y profesional. "
                "Respondes breve, haces preguntas claras y ayudas a agendar citas."
            ),
            "first_message": "Hola, soy el asistente de In Houston Texas. ¿En qué te ayudo?",
            "voice": "alloy",
            "temperature": 0.8,
            "model": "gpt-4o-realtime-preview",
            "realtime": {
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
            },
        }

    _normalize_realtime(cfg)
    _JSON_CACHE[slug] = cfg
    return cfg

# =========================
# Resolución de slug
# =========================
async def _resolve_bot_slug_from_twilio(request: Request) -> str:
    """
    Prioridad:
      1) query ?bot=slug
      2) map por número To (TWILIO_BOT_MAP) desde form-urlencoded
      3) DEFAULT_BOT
    """
    # 1) query
    q = dict(request.query_params)
    if "bot" in q and q["bot"].strip():
        return q["bot"].strip().lower()

    # 2) cuerpo Twilio
    try:
        form = await request.form()
        to_number = (form.get("To") or form.get("Called") or "").strip()
        if to_number and to_number in TWILIO_BOT_MAP:
            return TWILIO_BOT_MAP[to_number].strip().lower()
    except Exception:
        pass

    # 3) fallback
    return DEFAULT_BOT

# =========================
# Health
# =========================
@app.get("/")
async def root():
    return PlainTextResponse("✅ In Houston AI — FastAPI multibot listo para Twilio Realtime")

# =========================
# TwiML: recibe la llamada y conecta Media Stream
# =========================
@app.post("/twiml")
async def twiml_webhook(request: Request):
    host = request.headers.get("X-Forwarded-Host") or request.url.hostname or "inhouston-ai-api.onrender.com"
    slug = await _resolve_bot_slug_from_twilio(request)
    # Pasamos el slug a /media por query
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/media?bot={slug}" />
  </Connect>
</Response>"""
    return Response(content=xml.strip(), media_type="application/xml")

# =========================
# WebSocket principal (Twilio <-> OpenAI)
# =========================
@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [Twilio] WebSocket /media ACCEPTED")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY en variables de entorno")
        await websocket.close()
        return

    # Bot por query param
    q = dict(websocket.query_params)
    bot_slug = (q.get("bot") or DEFAULT_BOT).strip().lower()
    cfg = _load_bot_json(bot_slug)

    voice = cfg.get("voice", "alloy")
    temperature = float(cfg.get("temperature", 0.8))
    system_prompt = (cfg.get("system_prompt") or "").strip()
    first_message = (cfg.get("first_message") or "").strip()
    model = (cfg.get("model") or "gpt-4o-realtime-preview").strip()
    rt = cfg.get("realtime") or {}
    in_fmt = rt.get("input_audio_format") or "g711_ulaw"   # STRING
    out_fmt = rt.get("output_audio_format") or "g711_ulaw" # STRING
    turn_det = rt.get("turn_detection") or {"type": "server_vad"}  # dict

    print(f"🤖 [BOT] slug={bot_slug} model={model} voice={voice} temp={temperature}")
    print(f"🎛️ [BOT] realtime={{'turn_detection': {turn_det}, 'input_audio_format': '{in_fmt}', 'output_audio_format': '{out_fmt}'}}")

    realtime_uri = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    stream_sid: Optional[str] = None

    # ===== Estado de barge-in =====
    user_speaking = False  # True cuando llega "input_audio_buffer.speech_started"

    # Cola de salida para Twilio (μ-law b64) y emisor ritmado (20 ms)
    outbound_queue: asyncio.Queue[str] = asyncio.Queue()

    async def _twilio_send_ulaw_b64(ulaw_b64: str):
        """Envía un frame μ-law a Twilio (con streamSid si está disponible)."""
        if websocket.application_state != WebSocketState.CONNECTED:
            return
        payload = {"event": "media", "media": {"payload": ulaw_b64}}
        if stream_sid:
            payload["streamSid"] = stream_sid
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception as e:
            print(f"⚠️ [Twilio] envío fallido: {e}")

    async def _drain_outbound_queue():
        """Vacía de inmediato la cola para no pisar al usuario (barge-in)."""
        try:
            while True:
                outbound_queue.get_nowait()
                outbound_queue.task_done()
        except asyncio.QueueEmpty:
            pass

    async def paced_sender():
        """
        Consumidor de la cola de salida que envía a Twilio a ~20 ms por frame.
        Si no hay audio y el usuario está hablando → enviamos solo silencio (keepalive).
        """
        SILENCE_20 = ulaw_silence_b64(20)
        while websocket.application_state == WebSocketState.CONNECTED:
            try:
                if user_speaking:
                    # Mantén vivo el stream, pero NO envíes TTS (solo silencio)
                    await _twilio_send_ulaw_b64(SILENCE_20)
                    await asyncio.sleep(0.02)
                    continue

                b64 = await asyncio.wait_for(outbound_queue.get(), timeout=0.06)
                await _twilio_send_ulaw_b64(b64)
                outbound_queue.task_done()
                await asyncio.sleep(0.02)
            except asyncio.TimeoutError:
                # Cola vacía → keepalive
                await _twilio_send_ulaw_b64(SILENCE_20)

    try:
        async with websockets.connect(
            realtime_uri,
            extra_headers=headers,
            subprotocols=["realtime"],
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=10_000_000,
        ) as openai_ws:
            print("🔗 [OpenAI] Realtime CONNECTED")

            # ---- Configurar sesión con parámetros del JSON ----
            session_update = {
                "type": "session.update",
                "session": {
                    "turn_detection": turn_det,          # dict
                    "input_audio_format": in_fmt,        # STRING (g711_ulaw)
                    "output_audio_format": out_fmt,      # STRING (g711_ulaw)
                    "voice": voice,
                    "modalities": ["audio", "text"],
                    "instructions": system_prompt or (
                        "Eres un asistente de voz en español latino. "
                        "Saluda breve, sé útil, agenda citas cuando aplique."
                    ),
                    "temperature": temperature,
                }
            }
            print(f"➡️ [OpenAI] session.update (voice={voice}, temp={temperature}, in={in_fmt}, out={out_fmt})")
            await openai_ws.send(json.dumps(session_update))

            # Saludo inicial (opcional)
            if first_message:
                initial = {
                    "type": "response.create",
                    "response": {"modalities": ["audio", "text"], "instructions": first_message}
                }
                await openai_ws.send(json.dumps(initial))

            # Lanzamos el emisor ritmado hacia Twilio
            sender_task = asyncio.create_task(paced_sender())

            # ---- Twilio -> OpenAI (μ-law directo) ----
            async def twilio_to_openai():
                nonlocal stream_sid
                try:
                    while True:
                        msg_txt = await websocket.receive_text()
                        data = json.loads(msg_txt)
                        ev = data.get("event")

                        if ev == "start":
                            stream_sid = data.get("start", {}).get("streamSid")
                            print(f"🎧 [Twilio] stream START sid={stream_sid}")

                        elif ev == "media":
                            # μ-law base64 tal cual hacia OpenAI
                            ulaw_b64 = data["media"]["payload"]
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": ulaw_b64  # g711_ulaw base64
                            }))

                        elif ev == "mark":
                            pass

                        elif ev == "stop":
                            print("🛑 [Twilio] stream STOP (fin de la llamada)")
                            try:
                                await openai_ws.close()
                            except Exception:
                                pass
                            break

                except WebSocketDisconnect:
                    print("🔴 [Twilio] WebSocket DISCONNECT")
                    try:
                        await openai_ws.close()
                    except Exception:
                        pass
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")
                    try:
                        await openai_ws.close()
                    except Exception:
                        pass

            # ---- OpenAI -> Twilio ----
            async def openai_to_twilio():
                nonlocal user_speaking
                try:
                    async for raw in openai_ws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue

                        t = evt.get("type")

                        # Logs útiles (omitimos frames de audio para no inundar)
                        if t and t not in (
                            "response.audio.delta",
                            "response.output_audio.delta",
                            "input_audio_buffer.speech_started",
                            "input_audio_buffer.speech_stopped",
                        ):
                            print(f"ℹ️ [OpenAI] {t} :: {evt}")

                        if t == "error":
                            err = evt.get("error") or evt
                            print(f"❌ [OpenAI] ERROR DETALLE: {err}")

                        # === BARGe-IN: usuario empezó a hablar ===
                        if t == "input_audio_buffer.speech_started":
                            user_speaking = True
                            # Cancela cualquier TTS en curso y vacía la cola
                            await openai_ws.send(json.dumps({"type": "response.cancel"}))
                            await _drain_outbound_queue()

                        # Fin de habla del usuario → commit + pedir respuesta (audio+texto)
                        if t == "input_audio_buffer.speech_stopped":
                            user_speaking = False
                            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await openai_ws.send(json.dumps({
                                "type": "response.create",
                                "response": {"modalities": ["audio", "text"]}
                            }))

                        # Audio saliente del modelo
                        if t in ("response.audio.delta", "response.output_audio.delta"):
                            # Si el usuario está hablando, ignoramos deltas (no lo pises)
                            if user_speaking:
                                continue
                            audio_b64 = evt.get("delta") or evt.get("audio")
                            if not audio_b64:
                                continue
                            await outbound_queue.put(audio_b64)

                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            # Correr ambas corrutinas
            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

            # Cerrar emisor ritmado con gracia
            if not sender_task.done():
                sender_task.cancel()
                try:
                    await sender_task
                except asyncio.CancelledError:
                    pass

    except Exception as e:
        import traceback
        print("❌ [OpenAI] Fallo al conectar:", e)
        traceback.print_exc()
    finally:
        print("🔴 [Twilio] WebSocket CLOSED")

# =========================
# Local debug
# =========================
@app.get("/whoami")
async def whoami(request: Request):
    slug = await _resolve_bot_slug_from_twilio(request)
    return {"bot": slug}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
