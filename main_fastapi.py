# main_fastapi.py
# Twilio ↔ OpenAI Realtime — Multibot
# FIXES: sin commit manual con server_vad; turn_detection completo; saludo inicial con voice+μ-law;
# g711_ulaw end-to-end; barge-in real (cancel + drenar cola); logs claros.

import os, json, base64, asyncio, websockets
from pathlib import Path
from typing import Optional, Dict, Any
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState, WebSocketDisconnect

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PORT = int(os.getenv("PORT", "8080"))
ENV_DEFAULT_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "").strip()

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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def ulaw_silence_b64(ms: int = 20) -> str:
    samples = 8_000 * ms // 1000  # 160 bytes a 20 ms
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")

_JSON_CACHE: Dict[str, Dict[str, Any]] = {}

def _load_bot_json(slug: str) -> Dict[str, Any]:
    slug = (slug or "").strip().lower() or DEFAULT_BOT
    if slug in _JSON_CACHE:
        return _JSON_CACHE[slug]

    def read_json(s: str):
        p = BOTS_DIR / f"{s}.json"
        if not p.exists():
            return None
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)

    cfg = read_json(slug) or (read_json(DEFAULT_BOT) if slug != DEFAULT_BOT else None) or {}

    cfg.setdefault("voice", "marin")
    cfg.setdefault("temperature", 0.85)
    cfg.setdefault("model", ENV_DEFAULT_MODEL or "gpt-4o-realtime-preview-2024-12-17")
    cfg.setdefault("system_prompt",
        "Eres un asistente de voz de In Houston Texas (español latino, cálido y profesional). "
        "Responde breve y útil; ofrece agendar cuando aplique. Si el usuario interrumpe, cede la palabra (barge-in).")
    cfg.setdefault("first_message", "Hola, soy del equipo de In Houston Texas. ¿En qué te ayudo hoy?")

    rt = cfg.get("realtime") or {}
    td = rt.get("turn_detection") or {}
    # Normalizar posibles claves antiguas
    if "silence_ms" in td and "silence_duration_ms" not in td:
        td["silence_duration_ms"] = td.pop("silence_ms")
    if "prefix_ms" in td and "prefix_padding_ms" not in td:
        td["prefix_padding_ms"] = td.pop("prefix_ms")
    td.setdefault("type", "server_vad")
    td.setdefault("silence_duration_ms", 700)
    td.setdefault("prefix_padding_ms", 100)
    # Muy importante para que el servidor haga todo y no tengamos que hacer commit:
    td.setdefault("create_response", True)
    td.setdefault("interrupt_response", True)

    rt["turn_detection"] = td
    rt.setdefault("input_audio_format", "g711_ulaw")
    rt.setdefault("output_audio_format", "g711_ulaw")
    cfg["realtime"] = rt

    _JSON_CACHE[slug] = cfg
    return cfg

async def _resolve_bot_slug_from_twilio(request: Request) -> str:
    q = dict(request.query_params)
    if q.get("bot", "").strip():
        return q["bot"].strip().lower()
    try:
        form = await request.form()
        to_number = (form.get("To") or form.get("Called") or "").strip()
        if to_number and to_number in TWILIO_BOT_MAP:
            return TWILIO_BOT_MAP[to_number].strip().lower()
    except Exception:
        pass
    return DEFAULT_BOT

@app.get("/")
async def root():
    return PlainTextResponse("✅ In Houston AI — FastAPI multibot listo para Twilio Realtime")

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

@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [Twilio] WebSocket /media ACCEPTED")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY")
        await websocket.close()
        return

    # ----- Cargar bot -----
    q = dict(websocket.query_params)
    bot_slug = (q.get("bot") or DEFAULT_BOT).strip().lower()
    cfg = _load_bot_json(bot_slug)

    voice = cfg["voice"]
    temperature = float(cfg["temperature"])
    system_prompt = (cfg.get("system_prompt") or "").strip()
    first_message = (cfg.get("first_message") or "").strip()
    model = (cfg.get("model") or ENV_DEFAULT_MODEL or "gpt-4o-realtime-preview-2024-12-17").strip()

    rt = cfg["realtime"]
    in_fmt  = rt["input_audio_format"]        # 'g711_ulaw'
    out_fmt = rt["output_audio_format"]       # 'g711_ulaw'
    turn_det = rt["turn_detection"]           # dict

    print(f"🤖 [BOT] slug={bot_slug} model={model} voice={voice} temp={temperature}")
    print(f"🗂️ [CFG] first_message={first_message[:120]!r}")
    print(f"🎛️  [BOT] realtime={{'turn_detection': {turn_det}, 'input_audio_format': '{in_fmt}', 'output_audio_format': '{out_fmt}'}}")

    realtime_uri = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}

    stream_sid: Optional[str] = None
    user_speaking = False
    outbound_queue: asyncio.Queue[str] = asyncio.Queue()

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

    async def _drain_outbound_queue():
        try:
            while True:
                outbound_queue.get_nowait()
                outbound_queue.task_done()
        except asyncio.QueueEmpty:
            pass

    async def paced_sender():
        SILENCE_20 = ulaw_silence_b64(20)
        while websocket.application_state == WebSocketState.CONNECTED:
            try:
                if user_speaking:
                    await _twilio_send_ulaw_b64(SILENCE_20)
                    await asyncio.sleep(0.020)
                    continue
                b64 = await asyncio.wait_for(outbound_queue.get(), timeout=0.060)
                await _twilio_send_ulaw_b64(b64)
                outbound_queue.task_done()
                await asyncio.sleep(0.020)
            except asyncio.TimeoutError:
                await _twilio_send_ulaw_b64(SILENCE_20)

    try:
        async with websockets.connect(
            realtime_uri,
            extra_headers=headers,
            subprotocols=["realtime"],
            ping_interval=20, ping_timeout=20, close_timeout=5,
            max_size=10_000_000,
        ) as openai_ws:
            print("🔗 [OpenAI] Realtime CONNECTED")

            # 1) Session update (aplica voz, formatos y VAD server con respuesta automática)
            session_update = {
                "type": "session.update",
                "session": {
                    "turn_detection": turn_det,        # incluye create_response/interrupt_response
                    "input_audio_format": in_fmt,
                    "output_audio_format": out_fmt,
                    "voice": voice,
                    "modalities": ["audio", "text"],
                    "instructions": system_prompt or (
                        "Eres un asistente de voz en español latino; sé breve, útil y ofrece agendar cuando aplique."
                    ),
                    "temperature": temperature,
                }
            }
            print(f"➡️  [OpenAI] session.update (voice={voice}, temp={temperature}, in={in_fmt}, out={out_fmt})")
            await openai_ws.send(json.dumps(session_update))

            # 2) Primer saludo (forzado con voice + formato) — usa tu JSON sí o sí
            initial = {
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "voice": voice,
                    "output_audio_format": out_fmt
                }
            }
            if first_message:
                initial["response"]["instructions"] = first_message
            await openai_ws.send(json.dumps(initial))

            sender_task = asyncio.create_task(paced_sender())

            # Twilio → OpenAI (siempre agregamos; NO hacemos commit manual con server_vad)
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
                            # μ-law b64 directo
                            ulaw_b64 = data["media"]["payload"]
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": ulaw_b64
                            }))

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

            # OpenAI → Twilio
            async def openai_to_twilio():
                nonlocal user_speaking
                try:
                    async for raw in openai_ws:
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

                        if t == "input_audio_buffer.speech_started":
                            # Barge-in: cancelar TTS y drenar cola; NO commit manual
                            user_speaking = True
                            await openai_ws.send(json.dumps({"type": "response.cancel"}))
                            await _drain_outbound_queue()

                        if t == "input_audio_buffer.speech_stopped":
                            # Con server_vad + create_response:true, el servidor crea la respuesta.
                            user_speaking = False
                            # No enviar commit ni response.create aquí.

                        if t in ("response.audio.delta", "response.output_audio.delta"):
                            if user_speaking:
                                continue
                            audio_b64 = evt.get("delta") or evt.get("audio")
                            if audio_b64:
                                await outbound_queue.put(audio_b64)

                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

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

@app.get("/whoami")
async def whoami(request: Request):
    slug = await _resolve_bot_slug_from_twilio(request)
    return {"bot": slug}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
