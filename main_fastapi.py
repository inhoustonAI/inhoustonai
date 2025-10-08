# main_fastapi.py
# Twilio ↔ OpenAI Realtime (versión estable 2025)
# Corrige estructura: "voice" y "format" dentro de "response"

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

@app.get("/")
async def root():
    return PlainTextResponse("✅ In Houston AI — FastAPI listo para Twilio Realtime")

@app.post("/twiml")
async def twiml_webhook(_: Request):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://inhouston-ai-api.onrender.com/media" />
  </Connect>
</Response>"""
    return Response(content=xml, media_type="application/xml")

async def send_twilio_media(ws: WebSocket, ulaw_b64: str):
    if ws.application_state == WebSocketState.CONNECTED:
        await ws.send_text(json.dumps({"event": "media", "media": {"payload": ulaw_b64}}))

def ulaw_silence_frame_base64(ms: int = 20):
    samples = 8_000 * ms // 1000
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")

@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [Twilio] WebSocket /media ACCEPTED")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY en Render")
        await websocket.close()
        return

    realtime_uri = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}

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

            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "instructions": (
                        "Habla con voz natural, cálida y profesional en español. "
                        "Eres el asistente de In Houston Texas. "
                        "Saluda y pregunta cómo puedes ayudar."
                    ),
                    "voice": "alloy",
                    "format": "pcm16"
                }
            }))

            buffer_has_audio = False
            got_first_audio = False

            async def keepalive():
                while not got_first_audio and websocket.application_state == WebSocketState.CONNECTED:
                    await asyncio.sleep(0.05)
                    await send_twilio_media(websocket, ulaw_silence_frame_base64(20))

            asyncio.create_task(keepalive())

            async def twilio_to_openai():
                nonlocal buffer_has_audio
                try:
                    while True:
                        msg_txt = await websocket.receive_text()
                        data = json.loads(msg_txt)
                        ev = data.get("event")

                        if ev == "start":
                            print("🎧 [Twilio] stream START")

                        elif ev == "media":
                            ulaw = base64.b64decode(data["media"]["payload"])
                            pcm8k = audioop.ulaw2lin(ulaw, 2)
                            pcm16k, _ = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(pcm16k).decode("utf-8")
                            }))
                            buffer_has_audio = True

                        elif ev == "stop":
                            print("🛑 [Twilio] stream STOP → commit & response")
                            if buffer_has_audio:
                                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                await openai_ws.send(json.dumps({
                                    "type": "response.create",
                                    "response": {
                                        "modalities": ["audio", "text"],
                                        "voice": "alloy",
                                        "format": "pcm16"
                                    }
                                }))
                                buffer_has_audio = False
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")

            async def openai_to_twilio():
                nonlocal got_first_audio
                try:
                    async for raw in openai_ws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue

                        t = evt.get("type")
                        if t and t not in ("response.audio.delta", "output_audio.delta"):
                            if t == "error":
                                print(f"❌ [OpenAI] {evt.get('error')}")
                            else:
                                print(f"ℹ️ [OpenAI] {t}")

                        if t in ("response.audio.delta", "output_audio.delta"):
                            audio_b64 = evt.get("audio") or evt.get("delta")
                            if not audio_b64:
                                continue

                            pcm16 = base64.b64decode(audio_b64)
                            pcm8k, _ = audioop.ratecv(pcm16, 2, 1, 16000, 8000, None)
                            ulaw = audioop.lin2ulaw(pcm8k, 2)
                            await send_twilio_media(websocket, base64.b64encode(ulaw).decode("utf-8"))
                            got_first_audio = True
                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except Exception as e:
        import traceback
        print("❌ [OpenAI] Fallo al conectar:", e)
        traceback.print_exc()
    finally:
        print("🔴 [Twilio] WebSocket CLOSED")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=8080, reload=True)
