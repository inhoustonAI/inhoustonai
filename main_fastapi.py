# main_fastapi.py
# ============================================================
# IN HOUSTON AI — MATRIZ MULTIBOT (Twilio Media Streams ↔ OpenAI Realtime)
# - Lee el bot desde /bots/<bot>.json   (ej: /twiml?bot=sundin)
# - μ-law 8k end-to-end (sin resampling)
# - VAD server-side en OpenAI (speech_stopped => commit + response)
# - Emisor paceado a ~20ms + silencio keepalive para evitar audio cortado
# ============================================================

import os
import json
import base64
import asyncio
import websockets
from typing import Optional
from fastapi import FastAPI, WebSocket, Request, Query
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

# ------------- Config global -------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
PORT = int(os.getenv("PORT") or 8080)
BOTS_DIR = os.getenv("BOTS_DIR", "bots")
DEFAULT_MODEL = "gpt-4o-realtime-preview-2024-12-17"
DEFAULT_VOICE = "alloy"

app = FastAPI(title="IN HOUSTON AI — MATRIZ MULTIBOT")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

print("🧠 IN HOUSTON AI — MATRIZ MULTIBOT INICIADA")
print(f"📦 BOTS_DIR: {BOTS_DIR}")

# ------------- Helpers -------------
def load_bot_config(bot_name: str) -> dict:
    """Carga bots/<bot>.json (case-insensitive); lanza FileNotFoundError si no existe."""
    path_exact = os.path.join(BOTS_DIR, f"{bot_name}.json")
    if os.path.exists(path_exact):
        with open(path_exact, "r", encoding="utf-8") as f:
            return json.load(f)
    # intento lowercase
    path_lower = os.path.join(BOTS_DIR, f"{bot_name.lower()}.json")
    if os.path.exists(path_lower):
        with open(path_lower, "r", encoding="utf-8") as f:
            return json.load(f)
    raise FileNotFoundError(f"No existe el archivo {path_exact}")

def b64_silence_ulaw_8k(ms: int = 20) -> str:
    """Frame μ-law 8k de silencio (0xFF) con duración ms."""
    samples = 8000 * ms // 1000
    return base64.b64encode(b"\xFF" * samples).decode("ascii")

# ------------- Health -------------
@app.get("/")
async def root():
    return PlainTextResponse("✅ IN HOUSTON AI — Multibot listo")

# ------------- TwiML (NO token efímero aquí) -------------
@app.post("/twiml")
async def twiml_webhook(request: Request, bot: str = Query(...)):
    """
    Twilio => /twiml?bot=<id>
    Devuelve TwiML con saludo (opcional) y abre Media Stream a /media?bot=<id>.
    """
    try:
        cfg = load_bot_config(bot)
    except Exception as e:
        print(f"⚠️ Error cargando bot '{bot}': {e}")
        return PlainTextResponse("Bot no encontrado", status_code=404)

    host = request.url.hostname or os.getenv("RENDER_EXTERNAL_URL", "inhouston-ai-api.onrender.com")
    greeting = (cfg.get("greeting") or "").strip()
    tw_voice = cfg.get("twilio", {}).get("voice", "Polly.Lucia-Neural")
    tw_lang  = cfg.get("twilio", {}).get("language", "es-MX")

    # Consejo: si usas <Say> largo, Twilio tarda. Mantenlo breve o elimínalo.
    say_block = f'<Say voice="{tw_voice}" language="{tw_lang}">{greeting}</Say>' if greeting else ""

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {say_block}
  <Connect>
    <Stream url="wss://{host}/media?bot={bot}"
            track="inbound_audio"
            audioFormat="audio/x-ulaw;rate=8000"/>
  </Connect>
</Response>"""
    return Response(content=xml.strip(), media_type="application/xml")

# ------------- WebSocket principal (Twilio <-> OpenAI) -------------
@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    # 1) Valida query param bot
    bot = websocket.query_params.get("bot")
    if not bot:
        print("❌ Conexión rechazada: falta parámetro ?bot=")
        await websocket.close(code=403)
        return

    await websocket.accept()
    print(f"🟢 [Twilio] Conexión WS iniciada para bot={bot}")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY en variables de entorno")
        await websocket.close()
        return

    # 2) Cargar configuración de bot
    try:
        cfg = load_bot_config(bot)
    except Exception as e:
        print(f"❌ Error cargando bot '{bot}': {e}")
        await websocket.close()
        return

    model = cfg.get("model", DEFAULT_MODEL)
    voice = cfg.get("voice", DEFAULT_VOICE)
    instructions = (cfg.get("instructions") or "").strip()
    temperature = float(cfg.get("temperature", 0.8))
    rt = cfg.get("realtime", {}) or {}
    turn_detection_type = rt.get("turn_detection", "server_vad")
    input_fmt  = rt.get("input_audio_format",  "g711_ulaw")
    output_fmt = rt.get("output_audio_format", "g711_ulaw")

    # 3) Conectar a OpenAI Realtime con tu API KEY (no efímero)
    uri = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    # Pacer + cola hacia Twilio
    stream_sid: Optional[str] = None
    outbound_queue: asyncio.Queue[str] = asyncio.Queue()

    async def send_media_to_twilio(b64_ulaw: str):
        if websocket.application_state != WebSocketState.CONNECTED:
            return
        payload = { "event": "media", "media": {"payload": b64_ulaw} }
        if stream_sid:
            payload["streamSid"] = stream_sid
        await websocket.send_text(json.dumps(payload))

    async def paced_sender():
        """Envía frames a ~20ms; si no hay, manda silencio para mantener jitterbuffer de Twilio."""
        silence20 = b64_silence_ulaw_8k(20)
        while websocket.application_state == WebSocketState.CONNECTED:
            try:
                b64 = await asyncio.wait_for(outbound_queue.get(), timeout=0.06)
                await send_media_to_twilio(b64)
                await asyncio.sleep(0.02)  # ~20ms
            except asyncio.TimeoutError:
                await send_media_to_twilio(silence20)

    try:
        async with websockets.connect(
            uri,
            extra_headers=headers,
            subprotocols=["realtime"],
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=10_000_000,
        ) as oai:
            print("🔗 [OpenAI] Realtime CONNECTED")

            # 4) Configurar sesión (μ-law E2E + VAD + voz + sistema)
            session_update = {
                "type": "session.update",
                "session": {
                    "turn_detection": {"type": turn_detection_type},
                    "input_audio_format":  input_fmt,
                    "output_audio_format": output_fmt,
                    "voice": voice,
                    "modalities": ["text", "audio"],
                    "temperature": temperature,
                    "instructions": instructions
                }
            }
            await oai.send(json.dumps(session_update))

            # (Opcional) respuesta inicial automática del asistente (sin <Say> en TwiML):
            # await oai.send(json.dumps({"type": "response.create"}))

            sender_task = asyncio.create_task(paced_sender())

            # 5A) Twilio -> OpenAI
            async def twilio_to_openai():
                nonlocal stream_sid
                try:
                    while True:
                        raw = await websocket.receive_text()
                        data = json.loads(raw)
                        ev = data.get("event")
                        if ev == "start":
                            stream_sid = (data.get("start") or {}).get("streamSid")
                            print(f"🎧 [Twilio] stream START sid={stream_sid}")
                        elif ev == "media":
                            ulaw_b64 = data["media"]["payload"]
                            await oai.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": ulaw_b64  # μ-law 8k en base64, directo
                            }))
                        elif ev == "stop":
                            print("🛑 [Twilio] stream STOP")
                            try:
                                await oai.close()
                            except Exception:
                                pass
                            break
                        # 'mark' y otros eventos no son necesarios aquí
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")

            # 5B) OpenAI -> Twilio
            async def openai_to_twilio():
                try:
                    async for raw in oai:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue

                        t = evt.get("type")
                        # Logs útiles para depurar
                        if t and t not in ("response.audio.delta", "response.output_audio.delta",
                                           "input_audio_buffer.speech_started",
                                           "input_audio_buffer.speech_stopped"):
                            print(f"ℹ️ [OpenAI] {t}")

                        # Al detectar fin de habla del usuario => commit & response
                        if t == "input_audio_buffer.speech_stopped":
                            await oai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await oai.send(json.dumps({"type": "response.create"}))

                        # Audio saliente (nombres pueden variar por release)
                        if t in ("response.audio.delta", "response.output_audio.delta"):
                            b64_audio = evt.get("delta") or evt.get("audio")
                            if b64_audio:
                                await outbound_queue.put(b64_audio)

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

# ------------- Local run -------------
if __name__ == "__main__":
    import uvicorn, json
    print(f"🔌 Uvicorn en 0.0.0.0:{PORT}")
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
