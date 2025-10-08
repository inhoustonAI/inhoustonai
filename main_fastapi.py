# main_fastapi.py
# Versión 2025-10-08 — Realtime con handshake estable y logs extendidos

import os
import json
import base64
import asyncio
import audioop
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
app = FastAPI(title="In Houston AI — Twilio Realtime Bridge")

# CORS global
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

# Twilio webhook: genera TwiML
@app.post("/twiml")
async def twiml_webhook(request: Request):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://inhouston-ai-api.onrender.com/media" />
    </Connect>
</Response>"""
    return Response(content=xml, media_type="application/xml")

# WebSocket Twilio <-> OpenAI
@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [Twilio] WebSocket conectado")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY en Render")
        await websocket.close()
        return

    realtime_uri = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    try:
        async with websockets.connect(
            realtime_uri,
            extra_headers=headers,
            subprotocols=["realtime"],
            ping_interval=15,
            ping_timeout=15,
        ) as openai_ws:
            print("🔗 [OpenAI] Conectado correctamente")

            # Enviar saludo inicial
            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "modalities": ["audio"],
                    "instructions": (
                        "Habla en voz natural, en español, representando a In Houston Texas. "
                        "Preséntate como asistente de servicios en Houston, Texas, "
                        "y pregunta cómo puedes ayudar."
                    )
                }
            }))

            async def twilio_to_openai():
                """Twilio -> OpenAI"""
                try:
                    while True:
                        msg_txt = await websocket.receive_text()
                        data = json.loads(msg_txt)
                        ev = data.get("event")

                        if ev == "start":
                            print("🎧 [Twilio] stream iniciado")

                        elif ev == "media":
                            ulaw = base64.b64decode(data["media"]["payload"])
                            pcm8k = audioop.ulaw2lin(ulaw, 2)
                            pcm16k, _ = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)
                            b64 = base64.b64encode(pcm16k).decode("utf-8")
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": b64
                            }))

                        elif ev == "stop":
                            print("🛑 [Twilio] stream detenido — solicitando respuesta")
                            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await openai_ws.send(json.dumps({
                                "type": "response.create",
                                "response": {"modalities": ["audio"]}
                            }))
                            break
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")

            async def openai_to_twilio():
                """OpenAI -> Twilio"""
                try:
                    async for raw in openai_ws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue

                        if evt.get("type") == "response.error":
                            print(f"❌ [OpenAI] Error de respuesta: {evt}")
                        elif evt.get("type") == "session.error":
                            print(f"🚫 [OpenAI] Error de sesión: {evt}")
                        elif evt.get("type") == "error":
                            print(f"🚨 [OpenAI] Error general: {evt}")

                        if evt.get("type") == "output_audio.delta":
                            pcm16 = base64.b64decode(evt.get("audio", ""))
                            pcm8k, _ = audioop.ratecv(pcm16, 2, 1, 16000, 8000, None)
                            ulaw = audioop.lin2ulaw(pcm8k, 2)
                            payload = base64.b64encode(ulaw).decode("utf-8")
                            await websocket.send_text(json.dumps({
                                "event": "media",
                                "media": {"payload": payload}
                            }))
                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            # Tareas paralelas (bidireccional)
            await asyncio.gather(
                asyncio.create_task(twilio_to_openai()),
                asyncio.create_task(openai_to_twilio())
            )

    except Exception as e:
        print(f"❌ [OpenAI] Fallo al conectar: {e}")

    finally:
        print("🔴 [Twilio] WebSocket cerrado")

# Debug local
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=8080, reload=True)
