# main_fastapi.py
# Twilio ↔ OpenAI Realtime (μ-law 8 kHz end-to-end)
# Correcciones:
#  - Barge-in: cancelar TTS al detectar speech_started
#  - Commit solo con ≥120 ms de audio (evita commit_empty)
#  - Semáforo de respuesta activa (evita conversation_already_has_active_response)
#  - Keep-alive de silencio μ-law 20 ms durante habla del usuario

import os, json, base64, asyncio, websockets
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState, WebSocketDisconnect

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PORT = int(os.getenv("PORT", "8080"))

BOTS_DIR = Path(os.getenv("BOTS_DIR", "bots"))
DEFAULT_BOT = os.getenv("DEFAULT_BOT", "sundin")

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ───────── Audio utils ─────────
def ulaw_silence_b64(ms: int = 20) -> str:
    samples = 8_000 * ms // 1000
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")  # μ-law silencio = 0xFF

# ───────── Bots JSON ─────────
_JSON_CACHE: Dict[str, Dict[str, Any]] = {}

def _load_bot_json(slug: str) -> Dict[str, Any]:
    slug = (slug or "").strip().lower() or DEFAULT_BOT
    if slug in _JSON_CACHE:
        return _JSON_CACHE[slug]

    def _read(s: str):
        p = BOTS_DIR / f"{s}.json"
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)
        return None

    cfg = _read(slug) or (slug != DEFAULT_BOT and _read(DEFAULT_BOT))
    if cfg is None:
        cfg = {
            "system_prompt": "Eres el asistente de voz de In Houston Texas. Responde breve y agenda citas.",
            "first_message": "Hola, soy el asistente de In Houston Texas. ¿En qué te ayudo?",
            "voice": "alloy", "temperature": 0.7,
            "model": "gpt-4o-realtime-preview",
            "realtime": {"turn_detection": "server_vad",
                         "input_audio_format": "g711_ulaw",
                         "output_audio_format": "g711_ulaw"},
        }

    cfg.setdefault("voice", "alloy")
    cfg.setdefault("temperature", 0.7)
    cfg.setdefault("system_prompt", "")
    cfg.setdefault("first_message", "")
    cfg.setdefault("model", "gpt-4o-realtime-preview")
    rt = cfg.setdefault("realtime", {})
    if isinstance(rt.get("turn_detection"), str):
        rt["turn_detection"] = {"type": rt["turn_detection"]}
    rt.setdefault("turn_detection", {"type": "server_vad"})
    rt.setdefault("input_audio_format", "g711_ulaw")
    rt.setdefault("output_audio_format", "g711_ulaw")
    cfg["realtime"] = rt
    _JSON_CACHE[slug] = cfg
    return cfg

async def _resolve_bot_slug_from_twilio(request: Request) -> str:
    q = dict(request.query_params)
    if (b := q.get("bot", "").strip()):
        return b.lower()
    try:
        form = await request.form()
        to_number = (form.get("To") or form.get("Called") or "").strip()
        if to_number and to_number in TWILIO_BOT_MAP:
            return TWILIO_BOT_MAP[to_number].strip().lower()
    except Exception:
        pass
    return DEFAULT_BOT

# ───────── Health ─────────
@app.get("/")
async def root():
    return PlainTextResponse("✅ In Houston AI — FastAPI multibot listo para Twilio Realtime")

# ───────── TwiML ─────────
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

# ───────── WebSocket Twilio <-> OpenAI ─────────
@app.websocket("/media")
async def media_socket(ws_twilio: WebSocket):
    await ws_twilio.accept()
    print("🟢 [Twilio] WebSocket /media ACCEPTED")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY")
        await ws_twilio.close(); return

    q = dict(ws_twilio.query_params)
    bot_slug = (q.get("bot") or DEFAULT_BOT).strip().lower()
    cfg = _load_bot_json(bot_slug)

    voice = cfg.get("voice", "alloy")
    temperature = float(cfg.get("temperature", 0.7))
    system_prompt = (cfg.get("system_prompt") or "").strip()
    first_message = (cfg.get("first_message") or "").strip()
    model = (cfg.get("model") or "gpt-4o-realtime-preview").strip()
    rt = cfg.get("realtime") or {}
    in_fmt = rt.get("input_audio_format") or "g711_ulaw"
    out_fmt = rt.get("output_audio_format") or "g711_ulaw"
    turn_det = rt.get("turn_detection") or {"type": "server_vad"}

    print(f"🤖 [BOT] slug={bot_slug} model={model} voice={voice} temp={temperature}")
    print(f"🎛️  [BOT] realtime={{'turn_detection': {turn_det}, 'input_audio_format': '{in_fmt}', 'output_audio_format': '{out_fmt}'}}")

    realtime_uri = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}

    stream_sid: Optional[str] = None

    # ── Estado de conversación ──
    user_speaking = False               # True entre speech_started y speech_stopped
    frames_since_start = 0              # Twilio media frames de 20 ms (para medir buffer)
    RESPONSE_ACTIVE = False             # Semáforo de respuesta del modelo

    # ── Cola de salida (μ-law) y emisor ritmado ──
    outbound_queue: asyncio.Queue[str] = asyncio.Queue()

    async def _twilio_send_ulaw_b64(b64: str):
        if ws_twilio.application_state != WebSocketState.CONNECTED:
            return
        payload = {"event": "media", "media": {"payload": b64}}
        if stream_sid: payload["streamSid"] = stream_sid
        try:
            await ws_twilio.send_text(json.dumps(payload))
        except Exception as e:
            print(f"⚠️ [Twilio] envío fallido: {e}")

    async def _drain_outbound_queue():
        try:
            while True:
                outbound_queue.get_nowait()
                outbound_queue.task_done()
        except asyncio.QueueEmpty:
            pass

    async def paced_sender():
        SILENCE_20 = ulaw_silence_b64(20)
        while ws_twilio.application_state == WebSocketState.CONNECTED:
            try:
                if user_speaking:
                    await _twilio_send_ulaw_b64(SILENCE_20)
                    await asyncio.sleep(0.02)
                    continue
                b64 = await asyncio.wait_for(outbound_queue.get(), timeout=0.06)
                await _twilio_send_ulaw_b64(b64)
                outbound_queue.task_done()
                await asyncio.sleep(0.02)
            except asyncio.TimeoutError:
                await _twilio_send_ulaw_b64(SILENCE_20)

    try:
        async with websockets.connect(
            realtime_uri,
            extra_headers=headers,
            subprotocols=["realtime"],
            ping_interval=20, ping_timeout=20, close_timeout=5,
            max_size=10_000_000,
        ) as ws_ai:
            print("🔗 [OpenAI] Realtime CONNECTED")

            # Session config
            await ws_ai.send(json.dumps({
                "type": "session.update",
                "session": {
                    "turn_detection": turn_det,
                    "input_audio_format": in_fmt,
                    "output_audio_format": out_fmt,
                    "voice": voice,
                    "modalities": ["audio", "text"],
                    "instructions": system_prompt or "Asistente de voz en español latino.",
                    "temperature": temperature,
                }
            }))

            # Greeting
            if first_message:
                await ws_ai.send(json.dumps({
                    "type": "response.create",
                    "response": {"modalities": ["audio", "text"], "instructions": first_message}
                }))

            sender_task = asyncio.create_task(paced_sender())

            # Twilio -> OpenAI
            async def twilio_to_ai():
                nonlocal stream_sid, frames_since_start
                try:
                    while True:
                        msg_txt = await ws_twilio.receive_text()
                        data = json.loads(msg_txt)
                        ev = data.get("event")

                        if ev == "start":
                            stream_sid = data.get("start", {}).get("streamSid")
                            print(f"🎧 [Twilio] stream START sid={stream_sid}")

                        elif ev == "media":
                            # μ-law base64 directo a OpenAI
                            b64 = data["media"]["payload"]
                            await ws_ai.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))
                            if user_speaking:
                                frames_since_start += 1  # cada media ~20 ms

                        elif ev == "stop":
                            print("🛑 [Twilio] stream STOP")
                            try: await ws_ai.close()
                            except Exception: pass
                            break
                except WebSocketDisconnect:
                    print("🔴 [Twilio] WS disconnect")
                    try: await ws_ai.close()
                    except Exception: pass
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")
                    try: await ws_ai.close()
                    except Exception: pass

            # OpenAI -> Twilio
            async def ai_to_twilio():
                nonlocal user_speaking, RESPONSE_ACTIVE, frames_since_start
                try:
                    async for raw in ws_ai:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue
                        t = evt.get("type")

                        if t and t not in (
                            "response.audio.delta", "response.output_audio.delta",
                            "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                        ):
                            print(f"ℹ️ [OpenAI] {t} :: {evt}")

                        if t == "error":
                            print(f"❌ [OpenAI] ERROR DETALLE: {evt}")
                            continue

                        # Señalización de respuestas (semáforo)
                        if t in ("response.created", "response.output_text.delta", "response.audio.delta", "response.output_audio.delta"):
                            RESPONSE_ACTIVE = True
                        if t in ("response.completed", "response.done"):
                            RESPONSE_ACTIVE = False

                        # Barge-in: empieza a hablar el usuario
                        if t == "input_audio_buffer.speech_started":
                            user_speaking = True
                            frames_since_start = 0
                            # Cortar TTS en curso y vaciar cola
                            await ws_ai.send(json.dumps({"type": "response.cancel"}))
                            RESPONSE_ACTIVE = False
                            await _drain_outbound_queue()

                        # Fin de habla del usuario
                        if t == "input_audio_buffer.speech_stopped":
                            user_speaking = False
                            total_ms = frames_since_start * 20
                            frames_since_start = 0
                            if total_ms < 120:
                                # Menos de 100 ms → NO commit (evita commit_empty)
                                print(f"⏭️  [VAD] buffer {total_ms}ms <120ms, ignorado")
                            else:
                                await ws_ai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                # Asegurarnos de no crear una nueva respuesta si aún hay una activa (carril seguro)
                                if RESPONSE_ACTIVE:
                                    await ws_ai.send(json.dumps({"type": "response.cancel"}))
                                    RESPONSE_ACTIVE = False
                                    await asyncio.sleep(0.03)  # pequeño respiro para cerrar
                                await ws_ai.send(json.dumps({
                                    "type": "response.create",
                                    "response": {"modalities": ["audio", "text"]}
                                }))

                        # Audio del modelo → cola
                        if t in ("response.audio.delta", "response.output_audio.delta"):
                            if user_speaking:
                                continue  # no pises al usuario
                            audio_b64 = evt.get("delta") or evt.get("audio")
                            if audio_b64:
                                await outbound_queue.put(audio_b64)

                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            await asyncio.gather(twilio_to_ai(), ai_to_twilio())

            if not sender_task.done():
                sender_task.cancel()
                try: await sender_task
                except asyncio.CancelledError: pass

    except Exception as e:
        import traceback
        print("❌ [OpenAI] Fallo al conectar:", e)
        traceback.print_exc()
    finally:
        print("🔴 [Twilio] WebSocket CLOSED")

# ───────── Debug ─────────
@app.get("/whoami")
async def whoami(request: Request):
    slug = await _resolve_bot_slug_from_twilio(request)
    return {"bot": slug}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
