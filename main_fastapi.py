# main_fastapi.py
# Versión estable 2025-10-08 — Twilio ↔ OpenAI Realtime (oficial) con arranque de voz y logs claros

import os
import json
import base64
import asyncio
import audioop
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# ==============================
# CONFIGURACIÓN GLOBAL
# ==============================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

app = FastAPI(title="In Houston AI — Twilio Realtime Bridge")

# --- CORS general para Twilio ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================
# ENDPOINT RAÍZ (sanidad)
# ==============================
@app.get("/")
async def root():
    return PlainTextResponse("✅ In Houston AI — FastAPI activo y listo para Twilio Realtime")


# ==============================
# ENDPOINT /twiml — Twilio Webhook (POST)
#   Devuelve TwiML que abre el Stream hacia /media.
#   Quitamos <Say> para que NO hable Twilio y SOLO hable OpenAI.
# ==============================
@app.post("/twiml")
async def twiml_webhook(request: Request):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://inhouston-ai-api.onrender.com/media" />
    </Connect>
</Response>"""
    return Response(content=xml, media_type="application/xml")


# ==============================
# ENDPOINT /media — WebSocket Twilio ↔ OpenAI
#   - Twilio envía JSON con eventos start/media/stop (payload mu-law 8k en base64).
#   - Enviamos a OpenAI por Realtime WS (PCM16 16k en base64).
#   - Recibimos output_audio.delta de OpenAI (PCM16 16k) y lo convertimos a mu-law 8k para Twilio.
# ==============================
@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [Twilio] WebSocket /media ACCEPTED")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY en variables de entorno")
        await websocket.close()
        return

    # Handshake con OpenAI Realtime
    # Nota: usamos subprotocols para alinearnos con servidores que lo requieren.
    realtime_uri = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    oa_headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    try:
        async with websockets.connect(
            realtime_uri,
            extra_headers=oa_headers,
            subprotocols=["openai-realtime", "realtime"],
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ) as openai_ws:
            print("🔗 [OpenAI] Realtime CONNECTED")

            # === Arranque de conversación: hacer que OpenAI HABLE sin esperar audio ===
            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "modalities": ["audio"],
                    "instructions": (
                        "Eres el agente de voz de In Houston Texas. "
                        "Saluda de forma cálida y profesional, di que puedes dar información "
                        "sobre servicios en español en Houston y ayudar a agendar citas. "
                        "Habla claro y breve y luego pregunta cómo puedes ayudar."
                    )
                }
            }))

            # -------- Twilio -> OpenAI --------
            async def twilio_to_openai():
                try:
                    while True:
                        msg_txt = await websocket.receive_text()
                        data = json.loads(msg_txt)
                        event = data.get("event")

                        if event == "start":
                            print("🎧 [Twilio] START stream")

                        elif event == "media":
                            # Twilio manda mu-law 8k en base64
                            ulaw = base64.b64decode(data["media"]["payload"])
                            # mu-law 8k -> PCM16 8k
                            pcm8k = audioop.ulaw2lin(ulaw, 2)
                            # PCM16 8k -> PCM16 16k
                            pcm16k, _ = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)
                            b64 = base64.b64encode(pcm16k).decode("utf-8")

                            # Mandamos chunk a OpenAI
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": b64
                            }))

                        elif event == "stop":
                            print("🛑 [Twilio] STOP stream → commit y solicitar respuesta")
                            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await openai_ws.send(json.dumps({
                                "type": "response.create",
                                "response": {"modalities": ["audio"]}
                            }))
                            break
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")

            # -------- OpenAI -> Twilio --------
            async def openai_to_twilio():
                try:
                    async for raw in openai_ws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            # Mensajes binarios u otros formatos: ignorar silenciosamente
                            continue

                        # Frames de audio incremental desde OpenAI
                        if evt.get("type") == "output_audio.delta":
                            pcm16 = base64.b64decode(evt.get("audio", ""))
                            # PCM16 16k -> PCM16 8k
                            pcm8k, _ = audioop.ratecv(pcm16, 2, 1, 16000, 8000, None)
                            # PCM16 8k -> mu-law 8k
                            ulaw = audioop.lin2ulaw(pcm8k, 2)
                            payload = base64.b64encode(ulaw).decode("utf-8")

                            # Enviar de vuelta a Twilio
                            await websocket.send_text(json.dumps({
                                "event": "media",
                                "media": {"payload": payload}
                            }))

                        # Logs útiles
                        t = evt.get("type")
                        if t and t not in ("output_audio.delta",):
                            print(f"ℹ️ [OpenAI] evt: {t}")
                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except Exception as e:
        print(f"❌ [OpenAI] Fallo al conectar al Realtime WS: {e}")

    finally:
        print("🔴 [Twilio] WebSocket CLOSED")


# ==============================
# DEBUG LOCAL
# ==============================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=8080, reload=True)
