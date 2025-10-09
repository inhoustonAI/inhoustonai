# main_fastapi.py
# Versión estable Twilio ↔ OpenAI Realtime (oct 2025)
# - μ-law 8 kHz end-to-end (sin resampling)
# - Server VAD (turn_detection) en OpenAI
# - Envío con temporización (20 ms) hacia Twilio para evitar audio entrecortado
# - Reenvío de streamSid en los eventos "media" hacia Twilio
# - Respuestas creadas automáticamente al detectar fin de habla (speech_stopped)

import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

# ===== CONFIG =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PORT_ENV = os.getenv("PORT")
PORT = int(PORT_ENV) if PORT_ENV else 8080

print(f"🧩 DEBUG OPENAI_API_KEY: {OPENAI_API_KEY[:10]}...")
print(f"🧩 Using PORT: {PORT}")

VOICE = "alloy"  # Puedes cambiar la voz si tu cuenta lo permite

app = FastAPI(title="In Houston AI — Twilio Realtime Bridge")

# CORS para Twilio
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== Health =====
@app.get("/")
async def root():
    return PlainTextResponse("✅ In Houston AI — FastAPI listo para Twilio Realtime")

# ===== TwiML (recibe la llamada y conecta Media Stream) =====
@app.post("/twiml")
async def twiml_webhook(request: Request):
    host = request.url.hostname or "inhouston-ai-api.onrender.com"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/media" />
  </Connect>
</Response>"""
    return Response(content=xml.strip(), media_type="application/xml")

# ===== Utilidades =====
def ulaw_silence_b64(ms: int = 20) -> str:
    """Frame de silencio μ-law 8k (20 ms). μ-law silencio = 0xFF."""
    samples = 8_000 * ms // 1000
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")

# ===== WebSocket principal (Twilio <-> OpenAI) =====
@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [Twilio] WebSocket /media ACCEPTED")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY en variables de entorno")
        await websocket.close()
        return

    realtime_uri = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    stream_sid: str | None = None

    # Cola de salida para Twilio (μ-law b64) y emisor ritmado (20 ms)
    outbound_queue: asyncio.Queue[str] = asyncio.Queue()

    async def _twilio_send_ulaw_b64(ulaw_b64: str):
        """Envía un frame μ-law a Twilio (con streamSid si está disponible)."""
        if websocket.application_state != WebSocketState.CONNECTED:
            return
        payload = {
            "event": "media",
            "media": {"payload": ulaw_b64},
        }
        if stream_sid:
            payload["streamSid"] = stream_sid
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception as e:
            print(f"⚠️ [Twilio] envío fallido: {e}")

    async def paced_sender():
        """
        Consumidor de la cola de salida que envía a Twilio a ~20 ms por frame.
        Si la cola se queda vacía por >60 ms, envía un silencio de 20 ms (keepalive).
        """
        SILENCE_20 = ulaw_silence_b64(20)
        while websocket.application_state == WebSocketState.CONNECTED:
            try:
                # Espera un frame hasta 60 ms; si no hay, manda silencio
                b64 = await asyncio.wait_for(outbound_queue.get(), timeout=0.06)
                await _twilio_send_ulaw_b64(b64)
                # Ritmo objetivo ~20 ms
                await asyncio.sleep(0.02)
            except asyncio.TimeoutError:
                # Mantiene viva la llamada y el jitter buffer de Twilio estable
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

            # ---- Configurar sesión: μ-law end-to-end + VAD + voz + sistema ----
            session_update = {
                "type": "session.update",
                "session": {
                    "turn_detection": {"type": "server_vad"},
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "voice": VOICE,
                    "modalities": ["text", "audio"],
                    "instructions": (
                        "Eres el asistente de voz de In Houston Texas. "
                        "Habla en español latino, cálido y profesional. "
                        "Saluda breve y pregunta cómo puedes ayudar."
                    ),
                    "temperature": 0.8,
                }
            }
            print("➡️  [OpenAI] session.update")
            await openai_ws.send(json.dumps(session_update))

            # (Opcional) Saludo inicial inmediato:
            await openai_ws.send(json.dumps({"type": "response.create"}))

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
                            # Marca de Twilio para fin de un bloque reproducido (opcional)
                            pass

                        elif ev == "stop":
                            print("🛑 [Twilio] stream STOP (fin de la llamada)")
                            try:
                                await openai_ws.close()
                            except Exception:
                                pass
                            break
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")

            # ---- OpenAI -> Twilio ----
            async def openai_to_twilio():
                try:
                    async for raw in openai_ws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue

                        t = evt.get("type")

                        # Logs útiles
                        if t and t not in ("response.audio.delta", "response.output_audio.delta",
                                           "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"):
                            print(f"ℹ️ [OpenAI] {t}")

                        # Al detectar fin de habla: commit + pedir respuesta
                        if t == "input_audio_buffer.speech_stopped":
                            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await openai_ws.send(json.dumps({"type": "response.create"}))

                        # Audio saliente (los nombres pueden variar por release)
                        if t in ("response.audio.delta", "response.output_audio.delta"):
                            # Campo puede ser 'delta' (nuevo) o 'audio' (algunas previews)
                            audio_b64 = evt.get("delta") or evt.get("audio")
                            if not audio_b64:
                                continue
                            # Encolamos el frame para que el sender lo pacee a 20 ms
                            await outbound_queue.put(audio_b64)

                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

            # Cerrar emisor ritmado
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

# ===== Local debug =====
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
