# app/fastapi_voice/main.py
# Twilio + OpenAI Realtime Voice Bridge (FastAPI versión oficial)

import os, json, base64, asyncio, websockets
from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
WS_PUBLIC_URL = os.getenv("WS_PUBLIC_URL", "wss://inhouston-ai-api.onrender.com/media")

app = FastAPI()

@app.get("/", response_class=PlainTextResponse)
async def root():
    return "✅ FastAPI Voice Realtime OK"

@app.post("/twiml", response_class=PlainTextResponse)
async def twiml():
    """Genera TwiML con instrucción de streaming a /media"""
    vr = VoiceResponse()
    connect = Connect()
    connect.stream(url=WS_PUBLIC_URL)
    vr.append(connect)
    return PlainTextResponse(str(vr), media_type="text/xml")

@app.websocket("/media")
async def media(ws: WebSocket):
    """Canal bidireccional Twilio ↔ OpenAI Realtime"""
    await ws.accept()
    print("[WS] Twilio conectado")

    oa_url = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}"
    oa_headers = [("Authorization", f"Bearer {OPENAI_API_KEY}")]

    async with websockets.connect(oa_url, extra_headers=oa_headers) as oa:
        print("[WS] Conectado a OpenAI Realtime")

        async def twilio_to_openai():
            while True:
                msg = await ws.receive_text()
                data = json.loads(msg)
                event = data.get("event")
                if event == "media":
                    payload = base64.b64decode(data["media"]["payload"])
                    b64 = base64.b64encode(payload).decode("utf-8")
                    await oa.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))
                    await oa.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    await oa.send(json.dumps({
                        "type": "response.create",
                        "response": {"modalities": ["audio"], "instructions": "Responde en español, breve y claro."}
                    }))
                elif event == "stop":
                    print("[WS] Twilio stop")
                    break

        async def openai_to_twilio():
            while True:
                raw = await oa.recv()
                try:
                    evt = json.loads(raw)
                    if evt.get("type", "").endswith("audio.delta"):
                        audio_chunk_b64 = evt.get("audio", "")
                        await ws.send_text(json.dumps({
                            "event": "media",
                            "media": {"payload": audio_chunk_b64}
                        }))
                except Exception:
                    pass

        await asyncio.gather(twilio_to_openai(), openai_to_twilio())
    print("[WS] Cierre de conexión")
