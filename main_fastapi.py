# main_fastapi.py
# Versión multibot Twilio ↔ OpenAI Realtime (baseline estable + barge-in simple + guards)
# - μ-law 8 kHz end-to-end (sin resampling)
# - VAD de servidor (turn_detection) en OpenAI
# - Ritmo 20 ms hacia Twilio (keepalive si no hay cola)
# - Config por JSON en bots/<slug>.json

import os
import json
import base64
import asyncio
import websockets
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState, WebSocketDisconnect

# ===== CONFIG GLOBAL =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PORT = int(os.getenv("PORT", "8080"))

# Carpeta de bots y fallback
BOTS_DIR = Path(os.getenv("BOTS_DIR", "bots"))
DEFAULT_BOT = os.getenv("DEFAULT_BOT", "sundin")

# Mapa opcional: número de destino (To) → slug de bot
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

# ===== Utilidades de audio =====
def ulaw_silence_b64(ms: int = 20) -> str:
    """Frame de silencio μ-law 8k (20 ms). μ-law silencio = 0xFF."""
    samples = 8_000 * ms // 1000  # 160 bytes para 20 ms
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")

# ===== Utilidades de bots (JSON) =====
_JSON_CACHE: Dict[str, Dict[str, Any]] = {}

def _load_bot_json(slug: str) -> Dict[str, Any]:
    """
    Carga bots/<slug>.json. Si falla, intenta DEFAULT_BOT.
    Estructura mínima esperada:
      {
        "system_prompt": "texto...",
        "first_message": "Hola, soy ... ¿en qué te ayudo?",
        "voice": "alloy",
        "temperature": 0.8,
        "model": "gpt-4o-realtime-preview",
        "vad": {
            "type": "server_vad",
            "silence_ms": 550,
            "prefix_ms": 150
        }
      }
    """
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
            "vad": {"type": "server_vad", "silence_ms": 550, "prefix_ms": 150}
        }

    cfg.setdefault("voice", "alloy")
    cfg.setdefault("temperature", 0.8)
    cfg.setdefault("system_prompt", "")
    cfg.setdefault("model", "gpt-4o-realtime-preview")
    cfg.setdefault("vad", {"type": "server_vad", "silence_ms": 550, "prefix_ms": 150})
    _JSON_CACHE[slug] = cfg
    return cfg

def _canon_e164(num: str) -> str:
    s = (num or "").strip()
    s = s.replace(" ", "").replace("-", "").replace("(", "").replace(")")
    if s.startswith("00"):
        s = "+" + s[2:]
    return s

async def _resolve_bot_slug_from_twilio(request: Request) -> str:
    # 1) query
    q = dict(request.query_params)
    if "bot" in q and q["bot"].strip():
        return q["bot"].strip().lower()
    # 2) cuerpo Twilio
    try:
        form = await request.form()
        to_number = _canon_e164(form.get("To") or form.get("Called") or "")
        if to_number and to_number in TWILIO_BOT_MAP:
            return TWILIO_BOT_MAP[to_number].strip().lower()
    except Exception:
        pass
    # 3) fallback
    return DEFAULT_BOT

# ===== Health =====
@app.get("/")
async def root():
    return PlainTextResponse("✅ In Houston AI — FastAPI multibot listo para Twilio Realtime")

# ===== TwiML =====
@app.post("/twiml")
async def twiml_webhook(request: Request):
    host = request.url.hostname or "inhouston-ai-api.onrender.com"
    slug = await _resolve_bot_slug_from_twilio(request)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/media?bot={slug}" />
  </Connect>
</Response>"""
    return Response(content=xml.strip(), media_type="application/xml")

# ===== WebSocket Twilio <-> OpenAI =====
@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [Twilio] WebSocket /media ACCEPTED")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY en variables de entorno")
        await websocket.close()
        return

    # Bot config
    q = dict(websocket.query_params)
    bot_slug = (q.get("bot") or DEFAULT_BOT).strip().lower()
    cfg = _load_bot_json(bot_slug)

    voice = cfg.get("voice", "alloy")
    temperature = float(cfg.get("temperature", 0.8))
    system_prompt = (cfg.get("system_prompt") or "").strip()
    first_message = (cfg.get("first_message") or "").strip()
    model = (cfg.get("model") or "gpt-4o-realtime-preview").strip()

    vad_cfg = cfg.get("vad") or {}
    vad_type = (vad_cfg.get("type") or "server_vad").strip()
    vad_silence_ms = int(vad_cfg.get("silence_ms", 550))
    vad_prefix_ms = int(vad_cfg.get("prefix_ms", 150))

    print(f"🤖 [BOT] slug={bot_slug} model={model} voice={voice} temp={temperature}")

    realtime_uri = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    stream_sid: Optional[str] = None

    # Salida hacia Twilio (μ-law b64) con pacing 20ms
    outbound_queue: asyncio.Queue[str] = asyncio.Queue()

    # Estado de conversación / guards
    speaking_out = False          # el bot está emitiendo TTS
    barge_cancel_sent = False     # ya se canceló esta respuesta por barge-in
    response_active = False       # hay una respuesta en curso

    # Contador de audio ENTRANTE del usuario (para evitar commit vacío)
    USER_FRAME_MS = 20
    input_ms_since_last_commit = 0  # ms de audio desde último commit

    async def _twilio_send_ulaw_b64(ulaw_b64: str):
        if websocket.application_state != WebSocketState.CONNECTED:
            return
        payload = {"event": "media", "media": {"payload": ulaw_b64}}
        if stream_sid:
            payload["streamSid"] = stream_sid
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception as e:
            print(f"⚠️ [Twilio] envío fallido: {e}")

    async def paced_sender():
        SILENCE_20 = ulaw_silence_b64(20)
        try:
            while websocket.application_state == WebSocketState.CONNECTED:
                try:
                    b64 = await asyncio.wait_for(outbound_queue.get(), timeout=0.06)
                    await _twilio_send_ulaw_b64(b64)
                    await asyncio.sleep(0.02)
                except asyncio.TimeoutError:
                    await _twilio_send_ulaw_b64(SILENCE_20)
        except Exception as e:
            print(f"⚠️ [paced_sender] detenido: {e}")

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

            # Config sesión
            session_update = {
                "type": "session.update",
                "session": {
                    "turn_detection": {
                        "type": vad_type,
                        "silence_duration_ms": vad_silence_ms,
                        "prefix_padding_ms": vad_prefix_ms,
                    },
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "voice": voice,
                    "modalities": ["text", "audio"],
                    "instructions": system_prompt or (
                        "Eres un asistente de voz en español latino. "
                        "Saluda breve, sé útil, agenda citas cuando aplique."
                    ),
                    "temperature": temperature,
                }
            }
            print(f"➡️  [OpenAI] session.update (voice={voice}, temp={temperature}, vad={vad_type})")
            await openai_ws.send(json.dumps(session_update))

            # Saludo inicial
            if first_message:
                await openai_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"instructions": first_message}
                }))
                response_active = True
                print("👋 [OpenAI] first_message disparado")
            else:
                await openai_ws.send(json.dumps({"type": "response.create"}))
                response_active = True
                print("👋 [OpenAI] response.create inicial sin first_message")

            # Lanzar pacing
            sender_task = asyncio.create_task(paced_sender())

            # Twilio -> OpenAI
            async def twilio_to_openai():
                nonlocal stream_sid, speaking_out, barge_cancel_sent, input_ms_since_last_commit
                try:
                    while True:
                        msg_txt = await websocket.receive_text()
                        data = json.loads(msg_txt)
                        ev = data.get("event")

                        if ev == "start":
                            stream_sid = data.get("start", {}).get("streamSid")
                            print(f"🎧 [Twilio] stream START sid={stream_sid}")

                        elif ev == "media":
                            ulaw_b64 = data["media"]["payload"]

                            # Barge-in: si el bot habla y el usuario entra, cancelamos
                            if speaking_out and not barge_cancel_sent:
                                try:
                                    print("🛑 [BARGE-IN] user speaking → response.cancel + flush queue")
                                    await openai_ws.send(json.dumps({"type": "response.cancel"}))
                                    barge_cancel_sent = True
                                    # Vaciar cola TTS para cortar YA
                                    try:
                                        while True:
                                            outbound_queue.get_nowait()
                                            outbound_queue.task_done()
                                    except asyncio.QueueEmpty:
                                        pass
                                    speaking_out = False
                                except Exception as e:
                                    print(f"⚠️ [BARGE-IN] fallo al cancelar: {e}")

                            # Enviar audio del usuario
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": ulaw_b64
                            }))
                            # Contabilizamos 20 ms por frame Twilio
                            input_ms_since_last_commit += USER_FRAME_MS

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
                    print("🔻 [Twilio] WebSocket disconnect")
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

            # OpenAI -> Twilio
            async def openai_to_twilio():
                nonlocal speaking_out, barge_cancel_sent, response_active, input_ms_since_last_commit
                try:
                    async for raw in openai_ws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue

                        t = evt.get("type")

                        if t == "error":
                            print(f"🛑 [OpenAI ERROR] {json.dumps(evt, ensure_ascii=False)}")
                            continue

                        if t and t not in (
                            "response.audio.delta",
                            "response.output_audio.delta",
                            "input_audio_buffer.speech_started",
                            "input_audio_buffer.speech_stopped",
                        ):
                            print(f"ℹ️ [OpenAI] {t}")

                        # Estado de respuesta
                        if t == "response.created":
                            response_active = True
                        if t in ("response.output_item.done", "response.done"):
                            speaking_out = False
                            barge_cancel_sent = False
                            response_active = False

                        # VAD: fin de habla del usuario → commit + respuesta (solo si hubo audio y no hay respuesta activa)
                        if t == "input_audio_buffer.speech_stopped":
                            print("✂️  [OpenAI] speech_stopped → (guardas) commit + response.create")
                            if input_ms_since_last_commit >= 120 and not response_active:
                                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                input_ms_since_last_commit = 0
                                await openai_ws.send(json.dumps({"type": "response.create"}))
                                response_active = True
                            else:
                                # No commit: o no hubo audio suficiente o hay una respuesta en curso
                                pass

                        # Audio saliente (TTS)
                        if t in ("response.audio.delta", "response.output_audio.delta"):
                            audio_b64 = evt.get("delta") or evt.get("audio")
                            if not audio_b64:
                                continue
                            speaking_out = True
                            await outbound_queue.put(audio_b64)

                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

            # Cerrar pacing
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
        if websocket.application_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass

# ===== Local debug =====
@app.get("/whoami")
async def whoami(request: Request):
    slug = await _resolve_bot_slug_from_twilio(request)
    return {"bot": slug}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
