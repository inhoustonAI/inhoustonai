# main_fastapi.py
# Estructura MULTIBOT — Twilio ↔ OpenAI Realtime (basado en JSON por bot)

import os, json, asyncio, base64, websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

# === CONFIGURACIÓN GLOBAL ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
BOTS_DIR = "bots"
DEFAULT_MODEL = "gpt-4o-realtime-preview-2024-12-17"

app = FastAPI(title="IN HOUSTON AI — MultiBot Realtime")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

# === CARGA JSON DE BOT ===
def load_bot_config(bot_name: str):
    path = os.path.join(BOTS_DIR, f"{bot_name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"❌ No existe configuración para el bot '{bot_name}'")
    with open(path, "r") as f:
        return json.load(f)

# === RUTA DE SALUD ===
@app.get("/")
async def root():
    return PlainTextResponse("✅ IN HOUSTON AI — FastAPI Multibot listo")

# === TWIML ===
@app.post("/twiml")
async def twiml_webhook(request: Request, bot: str = "carlos"):
    try:
        cfg = load_bot_config(bot)
    except Exception:
        return PlainTextResponse("Bot no encontrado", status_code=404)

    host = request.url.hostname or "inhouston-ai-api.onrender.com"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Lucia-Neural" language="es-MX">{cfg.get('greeting', '')}</Say>
  <Connect>
    <Stream url="wss://{host}/media?bot={bot}" />
  </Connect>
</Response>"""
    return Response(content=xml.strip(), media_type="application/xml")

# === WS PRINCIPAL /media ===
@app.websocket("/media")
async def media_socket(websocket: WebSocket, bot: str):
    await websocket.accept()
    print(f"🟢 [Twilio] WS conectado (bot={bot})")

    try:
        cfg = load_bot_config(bot)
    except Exception as e:
        print(e)
        await websocket.close()
        return

    voice = cfg.get("voice", "alloy")
    temperature = cfg.get("temperature", 0.7)
    model = cfg.get("model", DEFAULT_MODEL)
    instructions = cfg.get("instructions", "Eres un asistente de voz profesional en español latino.")

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }
    uri = f"wss://api.openai.com/v1/realtime?model={model}"

    async with websockets.connect(uri, extra_headers=headers, subprotocols=["realtime"]) as oai:
        print(f"🔗 [OpenAI] Conectado ({bot})")

        # Configura sesión según JSON
        session_update = {
            "type": "session.update",
            "session": {
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "voice": voice,
                "temperature": temperature,
                "modalities": ["text", "audio"],
                "instructions": instructions
            }
        }
        await oai.send(json.dumps(session_update))
        await oai.send(json.dumps({"type": "response.create"}))

        # Función enviar audio a Twilio
        async def send_media(ulaw_b64):
            if websocket.application_state == WebSocketState.CONNECTED:
                await websocket.send_text(json.dumps({
                    "event": "media",
                    "media": {"payload": ulaw_b64}
                }))

        async def twilio_to_openai():
            while True:
                data = json.loads(await websocket.receive_text())
                if data.get("event") == "media":
                    await oai.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": data["media"]["payload"]
                    }))
                elif data.get("event") == "stop":
                    print(f"🛑 [Twilio] stream STOP ({bot})")
                    await oai.close()
                    break

        async def openai_to_twilio():
            async for msg in oai:
                evt = json.loads(msg)
                t = evt.get("type")
                if t in ("response.audio.delta", "response.output_audio.delta"):
                    ulaw_b64 = evt.get("delta") or evt.get("audio")
                    if ulaw_b64:
                        await send_media(ulaw_b64)
                if t == "input_audio_buffer.speech_stopped":
                    await oai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    await oai.send(json.dumps({"type": "response.create"}))

        await asyncio.gather(twilio_to_openai(), openai_to_twilio())
        print(f"🔴 [Twilio] WS cerrado ({bot})")

# === LOCAL ===
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), reload=True)
