# main_fastapi.py
# Versión 2025-10-08 — Twilio ↔ OpenAI Realtime ESTABLE con diagnóstico detallado

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
print(f"🧩 DEBUG — OPENAI_API_KEY cargada: {OPENAI_API_KEY[:10]}...")  # imprime solo el inicio

app = FastAPI(title="In Houston AI — Twilio Realtime Bridge")

# CORS general (Twilio)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================
# ENDPOINT RAÍZ (salud)
# ==============================
@app.get("/")
async def root():
    return PlainTextResponse("✅ In Houston AI — FastAPI listo para Twilio Realtime")

# ==============================
# ENDPOINT /twiml (POST)
# ==============================
@app.post("/twiml")
async def twiml_webhook(request: Request):
    """
    Twilio Voice → Webhook. Devuelve TwiML que abre un Media Stream
    hacia el WebSocket /media de este servicio.
    """
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://inhouston-ai-api.onrender.com/media" />
  </Connect>
</Response>"""
    return Response(content=xml, media_type="application/xml")

# ==============================
# ENDPOINT /media — Twilio ↔ OpenAI
# ==============================
@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [Twilio] WebSocket /media ACCEPTED")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY en Render")
        await websocket.close()
        return

    # URI del Realtime API
    realtime_uri = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    oa_headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    subprotocols = ["realtime", "openai-realtime"]  # compatibles

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

            # ⚙️ Saludo / arranque (modalidades válidas)
            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "instructions": (
                        "Habla con voz natural, cálida y profesional en español. "
                        "Eres el asistente de In Houston Texas. "
                        "Saluda de forma breve y pregunta en qué puedes ayudar."
                    )
                }
            }))

            buffer_has_audio = False  # evita commits vacíos

            # ---------- Twilio -> OpenAI ----------
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
                            # Twilio envía audio μ-law (8 kHz) en base64
                            ulaw = base64.b64decode(data["media"]["payload"])
                            # μ-law 8k → PCM16 8k
                            pcm8k = audioop.ulaw2lin(ulaw, 2)
                            # PCM16 8k → PCM16 16k (lo que espera OpenAI)
                            pcm16k, _ = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)
                            audio_b64 = base64.b64encode(pcm16k).decode("utf-8")

                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": audio_b64
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
                            else:
                                print("⚠️ [OpenAI] buffer vacío, omitiendo commit")
                            buffer_has_audio = False
                            # NO cerramos; Twilio puede reiniciar otro ciclo start/media/stop
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")

            # ---------- OpenAI -> Twilio ----------
            async def openai_to_twilio():
                try:
                    async for raw in openai_ws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            # Mensajes binarios u otros: ignorar
                            continue

                        evt_type = evt.get("type")

                        # Log útil (sin inundar)
                        if evt_type and evt_type not in ("output_audio.delta",):
                            print(f"ℹ️ [OpenAI] {evt_type}")

                        if evt_type == "output_audio.delta":
                            # Audio PCM16 16k de OpenAI → μ-law 8k para Twilio
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

            # Lanzar tareas en paralelo
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

# ==============================
# DEBUG LOCAL
# ==============================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=8080, reload=True)
