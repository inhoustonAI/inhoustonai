# main_fastapi.py
# Versión 2025-10-08 — Twilio ↔ OpenAI Realtime ESTABLE
# Pide audio explícito (voice+format) y maneja response.audio.delta

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
print(f"🧩 DEBUG — OPENAI_API_KEY cargada: {OPENAI_API_KEY[:10]}...")

app = FastAPI(title="In Houston AI — Twilio Realtime Bridge")

# CORS (Twilio)
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

# TwiML para el número
@app.post("/twiml")
async def twiml_webhook(request: Request):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://inhouston-ai-api.onrender.com/media" />
  </Connect>
</Response>"""
    return Response(content=xml, media_type="application/xml")

@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [Twilio] WebSocket /media ACCEPTED")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY en Render")
        await websocket.close()
        return

    realtime_uri = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    oa_headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    subprotocols = ["realtime", "openai-realtime"]

    print(f"🌐 Intentando conectar con OpenAI Realtime → {realtime_uri}")
    try:
        async with websockets.connect(
            realtime_uri,
            extra_headers=oa_headers,
            subprotocols=subprotocols,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=10_000_000,
        ) as openai_ws:
            print("🔗 [OpenAI] Realtime CONNECTED")

            # Solicita respuesta de audio explícitamente (voz+formato)
            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "instructions": (
                        "Habla con voz natural, cálida y profesional en español. "
                        "Eres el asistente de In Houston Texas. "
                        "Saluda brevemente y pregunta en qué puedes ayudar."
                    ),
                    "audio": {
                        "voice": "alloy",     # voz TTS
                        "format": "pcm16"     # imprescindible para recibir audio en frames
                    }
                }
            }))

            buffer_has_audio = False

            async def safe_send_to_twilio(payload_b64: str):
                """Envía un frame μ-law a Twilio si el socket sigue abierto."""
                if websocket.application_state != WebSocketState.CONNECTED:
                    return
                try:
                    await websocket.send_text(json.dumps({
                        "event": "media",
                        "media": {"payload": payload_b64}
                    }))
                except Exception as e:
                    print(f"⚠️ [Twilio] send_text falló: {e}")

            # -------- Twilio -> OpenAI --------
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
                            # μ-law 8k → PCM16 8k
                            ulaw = base64.b64decode(data["media"]["payload"])
                            pcm8k = audioop.ulaw2lin(ulaw, 2)
                            # 8k → 16k
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
                                        "audio": {"voice": "alloy", "format": "pcm16"}
                                    }
                                }))
                            else:
                                print("⚠️ [OpenAI] buffer vacío, omitiendo commit")
                            buffer_has_audio = False
                            # no cerramos; Twilio puede reiniciar otro ciclo
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")

            # -------- OpenAI -> Twilio --------
            async def openai_to_twilio():
                """
                Reenvía el audio de OpenAI a Twilio.
                Soporta:
                  - output_audio.delta (antiguo)
                  - response.audio.delta (actual)
                """
                try:
                    async for raw in openai_ws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue

                        t = evt.get("type")
                        if t and t not in ("output_audio.delta", "response.audio.delta"):
                            print(f"ℹ️ [OpenAI] {t}")

                        if t in ("output_audio.delta", "response.audio.delta"):
                            audio_b64 = evt.get("audio") or evt.get("delta")
                            if not audio_b64:
                                continue

                            # PCM16 16k → μ-law 8k para Twilio
                            pcm16 = base64.b64decode(audio_b64)
                            pcm8k, _ = audioop.ratecv(pcm16, 2, 1, 16000, 8000, None)
                            ulaw = audioop.lin2ulaw(pcm8k, 2)
                            await safe_send_to_twilio(base64.b64encode(ulaw).decode("utf-8"))
                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            await asyncio.gather(
                asyncio.create_task(twilio_to_openai()),
                asyncio.create_task(openai_to_twilio())
            )

    except Exception as e:
        import traceback
        print("❌ [OpenAI] Fallo al conectar:", e)
        traceback.print_exc()
    finally:
        print("🔴 [Twilio] WebSocket CLOSED")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=8080, reload=True)
