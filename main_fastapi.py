# main_fastapi.py
# Versión FINAL — Twilio ↔ OpenAI Realtime (oct-2025)
# Bidireccional estable con conversión PCM16→μ-law (8k) y manejo de cierre limpio

import os
import json
import base64
import asyncio
import audioop
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

# ==============================
# CONFIG
# ==============================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
print(f"🧩 DEBUG OPENAI_API_KEY: {OPENAI_API_KEY[:10]}...")

app = FastAPI(title="In Houston AI — Twilio Realtime Bridge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================
# HEALTH
# ==============================
@app.get("/")
async def root():
    return PlainTextResponse("✅ In Houston AI — FastAPI listo para Twilio Realtime")

# ==============================
# TWIML (POST)
# ==============================
@app.post("/twiml")
async def twiml_webhook(_: Request):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://inhouston-ai-api.onrender.com/media" />
  </Connect>
</Response>"""
    return Response(content=xml.strip(), media_type="application/xml")

# ==============================
# HELPERS
# ==============================
def pcm16_to_mulaw_8k(pcm16_16k: bytes) -> bytes:
    """PCM16 (16kHz) -> μ-law (8kHz)"""
    pcm8k = audioop.ratecv(pcm16_16k, 2, 1, 16000, 8000, None)[0]
    return audioop.lin2ulaw(pcm8k, 2)

def mulaw_to_pcm16_16k(mulaw_8k: bytes) -> bytes:
    """μ-law (8kHz) -> PCM16 (16kHz)"""
    pcm8k = audioop.ulaw2lin(mulaw_8k, 2)
    return audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)[0]

def ulaw_silence_base64(ms: int = 20) -> str:
    """Frame de silencio μ-law 8k (por defecto 20ms)"""
    samples = 8000 * ms // 1000
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")

async def twilio_send_ulaw(ws: WebSocket, ulaw_bytes: bytes):
    """Envía un frame μ-law a Twilio (si el socket sigue conectado)."""
    if ws.application_state == WebSocketState.CONNECTED:
        payload = base64.b64encode(ulaw_bytes).decode("utf-8")
        try:
            await ws.send_text(json.dumps({"event": "media", "media": {"payload": payload}}))
        except Exception as e:
            print(f"⚠️ [Twilio] envío fallido: {e}")

# ==============================
# MEDIA WS (Twilio <-> OpenAI)
# ==============================
@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [Twilio] WebSocket /media ACCEPTED")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY")
        await websocket.close()
        return

    openai_ws_url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    oa_headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}

    try:
        async with websockets.connect(
            openai_ws_url,
            extra_headers=oa_headers,
            subprotocols=["realtime"],
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=10_000_000,
        ) as openai_ws:
            print("🔗 [OpenAI] Realtime CONNECTED")

            # Mensaje inicial (sin parámetros obsoletos)
            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "instructions": (
                        "Habla con voz natural, clara y profesional en español latino. "
                        "Eres el asistente de In Houston Texas. Saluda y pregunta cómo puedes ayudar."
                    )
                }
            }))

            buffer_has_audio = False
            keepalive_stop = False

            async def keepalive():
                """Envia silencio μ-law cada 200ms para evitar que Twilio corte por inactividad."""
                nonlocal keepalive_stop
                try:
                    while not keepalive_stop and websocket.application_state == WebSocketState.CONNECTED:
                        await asyncio.sleep(0.2)
                        await websocket.send_text(json.dumps({
                            "event": "media",
                            "media": {"payload": ulaw_silence_base64(20)}
                        }))
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    print(f"⚠️ keepalive: {e}")

            ka_task = asyncio.create_task(keepalive())

            # -------- Twilio -> OpenAI --------
            async def twilio_to_openai():
                nonlocal buffer_has_audio, keepalive_stop
                try:
                    while True:
                        msg = await websocket.receive_text()
                        data = json.loads(msg)
                        ev = data.get("event")

                        if ev == "start":
                            print("🎧 [Twilio] stream START")

                        elif ev == "media":
                            mulaw_b64 = data["media"]["payload"]
                            mulaw = base64.b64decode(mulaw_b64)
                            pcm16_16k = mulaw_to_pcm16_16k(mulaw)
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(pcm16_16k).decode("utf-8")
                            }))
                            buffer_has_audio = True

                        elif ev == "stop":
                            print("🛑 [Twilio] stream STOP → commit & response")
                            if buffer_has_audio:
                                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                await openai_ws.send(json.dumps({
                                    "type": "response.create",
                                    "response": {"modalities": ["audio", "text"]}
                                }))
                                buffer_has_audio = False
                            else:
                                print("⚠️ buffer vacío: omitimos commit")
                            break
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")
                finally:
                    keepalive_stop = True

            # -------- OpenAI -> Twilio --------
            async def openai_to_twilio():
                nonlocal keepalive_stop
                try:
                    async for raw in openai_ws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue

                        t = evt.get("type")

                        # Logs útiles (omitir spam de audio)
                        if t and t not in ("response.audio.delta", "output_audio.delta"):
                            if t == "error":
                                print(f"❌ [OpenAI] {evt.get('error')}")
                            else:
                                print(f"ℹ️ [OpenAI] {t}")

                        # OpenAI puede enviar response.audio.delta (nuevo) o output_audio.delta (previo)
                        if t in ("response.audio.delta", "output_audio.delta"):
                            audio_b64 = evt.get("audio") or evt.get("delta")
                            if not audio_b64:
                                continue
                            pcm16_16k = base64.b64decode(audio_b64)
                            mulaw_8k = pcm16_to_mulaw_8k(pcm16_16k)
                            await twilio_send_ulaw(websocket, mulaw_8k)
                            keepalive_stop = True  # ya hay audio real; cortamos silencio
                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")
                finally:
                    keepalive_stop = True

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

            # Apagar keepalive si siguiera activo
            keepalive_stop = True
            if not ka_task.done():
                ka_task.cancel()

    except Exception as e:
        import traceback
        print("❌ [OpenAI] Fallo al conectar:", e)
        traceback.print_exc()
    finally:
        print("🔴 [Twilio] WebSocket CLOSED")

# ==============================
# LOCAL DEBUG
# ==============================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=8080, reload=True)
