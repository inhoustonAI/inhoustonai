# =============================================================
# IN HOUSTON AI — MATRIZ MULTIBOT ESTABLE FINAL (Oct 2025)
# =============================================================
# - Conecta Twilio ↔ OpenAI Realtime (g711_ulaw 8k)
# - Fluidez total: inicia OpenAI antes de recibir audio
# - Soporta múltiples bots (uno por JSON en /bots/)
# =============================================================

import os, json, base64, asyncio, websockets
from fastapi import FastAPI, WebSocket, Request, Query
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
BOTS_DIR = "bots"
PORT = int(os.getenv("PORT", 8080))
APP_NAME = "IN HOUSTON AI — MATRIZ MULTIBOT"
ACTIVE_BOT = {"last": "sundin"}  # Valor por defecto

app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

print("🧠 IN HOUSTON AI — MATRIZ MULTIBOT INICIADA")
print(f"📦 BOTS_DIR: {BOTS_DIR}")

# ---------- Utilidades ----------
def load_bot_config(bot_name: str):
    path = os.path.join(BOTS_DIR, f"{bot_name.lower()}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No existe {path}")
    with open(path, "r") as f:
        return json.load(f)

def ulaw_silence_b64(ms=20):
    samples = 8_000 * ms // 1000
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")

# ---------- Salud ----------
@app.get("/")
async def root():
    return PlainTextResponse("✅ IN HOUSTON AI — MATRIZ MULTIBOT ACTIVA")

# ---------- TwiML ----------
@app.post("/twiml")
async def twiml_webhook(request: Request, bot: str = Query(...)):
    try:
        cfg = load_bot_config(bot)
    except Exception as e:
        print(f"⚠️ Error cargando bot {bot}: {e}")
        return PlainTextResponse("Bot no encontrado", status_code=404)

    host = request.url.hostname or "inhouston-ai-api.onrender.com"
    voice = cfg.get("twilio", {}).get("voice", "Polly.Lucia-Neural")
    lang = cfg.get("twilio", {}).get("language", "es-MX")
    greeting = cfg.get("greeting", f"Hola, soy {bot.capitalize()} de In Houston Texas.")

    ACTIVE_BOT["last"] = bot
    print(f"📞 Llamada entrante para bot={bot}")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="{voice}" language="{lang}">{greeting}</Say>
  <Connect>
    <Stream url="wss://{host}/media" />
  </Connect>
</Response>"""

    return Response(content=xml.strip(), media_type="application/xml")

# ---------- WebSocket principal ----------
@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [Twilio] Conexión WS abierta")

    bot = ACTIVE_BOT.get("last", "sundin")
    cfg = load_bot_config(bot)

    model = cfg.get("model", "gpt-4o-realtime-preview-2024-12-17")
    voice = cfg.get("voice", "alloy")
    instructions = cfg.get("instructions", "")
    temp = cfg.get("temperature", 0.8)

    # Conectamos OpenAI antes de recibir audio
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}
    uri = f"wss://api.openai.com/v1/realtime?model={model}"

    try:
        async with websockets.connect(uri, extra_headers=headers, subprotocols=["realtime"]) as openai_ws:
            print(f"🤖 [OpenAI] Conectado — bot={bot}")

            # Configuración inicial
            await openai_ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "turn_detection": {"type": "server_vad"},
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "voice": voice,
                    "temperature": temp,
                    "modalities": ["text", "audio"],
                    "instructions": instructions
                }
            }))
            await openai_ws.send(json.dumps({"type": "response.create"}))

            async def twilio_to_openai():
                try:
                    while True:
                        msg = json.loads(await websocket.receive_text())
                        ev = msg.get("event")

                        if ev == "media":
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": msg["media"]["payload"]
                            }))

                        elif ev == "stop":
                            print("🛑 [Twilio] STOP recibido — cerrando OpenAI")
                            await openai_ws.close()
                            break

                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")

            async def openai_to_twilio():
                try:
                    async for raw in openai_ws:
                        evt = json.loads(raw)
                        t = evt.get("type")

                        if t == "input_audio_buffer.speech_stopped":
                            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await openai_ws.send(json.dumps({"type": "response.create"}))

                        elif t in ("response.audio.delta", "response.output_audio.delta"):
                            ulaw_b64 = evt.get("delta") or evt.get("audio")
                            if ulaw_b64:
                                await websocket.send_text(json.dumps({
                                    "event": "media",
                                    "media": {"payload": ulaw_b64}
                                }))
                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except Exception as e:
        import traceback
        print("❌ Error global en /media:", e)
        traceback.print_exc()

    finally:
        print("🔴 [Twilio] WS cerrado")

# ---------- Local debug ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
