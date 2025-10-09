# main_fastapi.py
# =============================================
# IN HOUSTON AI — ESTRUCTURA MULTIBOT (OCT 2025)
# =============================================
# Núcleo FastAPI para múltiples bots de voz.
# Cada bot tiene su configuración JSON en /bots/<nombre>.json
# Ejemplo: /twiml?bot=sundin → usa bots/sundin.json

import os
import json
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request, Query
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

# ========= CONFIG GLOBAL =========
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
BOTS_DIR = "bots"
DEFAULT_MODEL = "gpt-4o-realtime-preview-2024-12-17"
DEFAULT_VOICE = "alloy"

app = FastAPI(title="IN HOUSTON AI — Multibot Realtime")

# ========= CORS (Twilio → FastAPI) =========
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========= UTILIDAD: Cargar configuración JSON =========
def load_bot_config(bot_name: str):
    """Carga la configuración JSON de un bot."""
    path = os.path.join(BOTS_DIR, f"{bot_name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No existe el archivo {path}")
    with open(path, "r") as f:
        return json.load(f)

# ========= HEALTH CHECK =========
@app.get("/")
async def root():
    return PlainTextResponse("✅ IN HOUSTON AI — Multibot listo y operativo")

# ========= TWIML (entrada de llamada) =========
@app.post("/twiml")
async def twiml_webhook(request: Request, bot: str = Query(...)):
    """
    Twilio → /twiml?bot=sundin
    Devuelve TwiML con saludo y conexión al WebSocket del bot.
    """
    try:
        cfg = load_bot_config(bot)
    except Exception as e:
        print(f"⚠️ Error cargando bot '{bot}': {e}")
        return PlainTextResponse("Bot no encontrado", status_code=404)

    host = request.url.hostname or "inhouston-ai-api.onrender.com"
    greeting = cfg.get("greeting", "Hola, soy tu asistente de In Houston Texas.")
    twilio_voice = cfg.get("twilio", {}).get("voice", "Polly.Lucia-Neural")
    twilio_lang = cfg.get("twilio", {}).get("language", "es-MX")

    # 🔥 CORRECCIÓN CLAVE: pasar bot también en el URL del Stream
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="{twilio_voice}" language="{twilio_lang}">{greeting}</Say>
  <Connect>
    <Stream url="wss://{host}/media?bot={bot}" />
  </Connect>
</Response>"""

    return Response(content=xml.strip(), media_type="application/xml")

# ========= WEBSOCKET /media =========
@app.websocket("/media")
async def media_socket(websocket: WebSocket, bot: str = Query(...)):
    """
    WebSocket: Twilio ↔ OpenAI Realtime ↔ Bot específico.
    """
    await websocket.accept()
    print(f"🟢 [Twilio] WS conectado (bot={bot})")

    try:
        cfg = load_bot_config(bot)
    except Exception as e:
        print(f"❌ Error cargando config del bot '{bot}': {e}")
        await websocket.close()
        return

    # === Extraer parámetros del JSON ===
    model = cfg.get("model", DEFAULT_MODEL)
    voice = cfg.get("voice", DEFAULT_VOICE)
    instructions = cfg.get("instructions", "Eres un asistente profesional.")
    temperature = cfg.get("temperature", 0.7)
    realtime_cfg = cfg.get("realtime", {})
    input_fmt = realtime_cfg.get("input_audio_format", "g711_ulaw")
    output_fmt = realtime_cfg.get("output_audio_format", "g711_ulaw")
    turn_detection = realtime_cfg.get("turn_detection", "server_vad")

    # === Conexión a OpenAI Realtime ===
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }
    uri = f"wss://api.openai.com/v1/realtime?model={model}"

    try:
        async with websockets.connect(uri, extra_headers=headers, subprotocols=["realtime"]) as oai:
            print(f"🔗 [OpenAI] Realtime conectado (bot={bot})")

            # --- Actualizar sesión con configuración del bot ---
            session_update = {
                "type": "session.update",
                "session": {
                    "turn_detection": {"type": turn_detection},
                    "input_audio_format": input_fmt,
                    "output_audio_format": output_fmt,
                    "voice": voice,
                    "temperature": temperature,
                    "modalities": ["text", "audio"],
                    "instructions": instructions
                }
            }

            await oai.send(json.dumps(session_update))
            await oai.send(json.dumps({"type": "response.create"}))

            # --- Función auxiliar ---
            async def send_media(ulaw_b64):
                if websocket.application_state == WebSocketState.CONNECTED:
                    try:
                        await websocket.send_text(json.dumps({
                            "event": "media",
                            "media": {"payload": ulaw_b64}
                        }))
                    except Exception:
                        pass

            # --- Twilio → OpenAI ---
            async def twilio_to_openai():
                while True:
                    try:
                        data = json.loads(await websocket.receive_text())
                        ev = data.get("event")
                        if ev == "media":
                            await oai.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"]
                            }))
                        elif ev == "stop":
                            print(f"🛑 [Twilio] stream STOP ({bot})")
                            await oai.close()
                            break
                    except Exception as e:
                        print(f"⚠️ [Twilio→OpenAI] Error: {e}")
                        break

            # --- OpenAI → Twilio ---
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

    except Exception as e:
        import traceback
        print(f"❌ [OpenAI] Error global ({bot}): {e}")
        traceback.print_exc()
    finally:
        print(f"🔴 [Twilio] WS cerrado ({bot})")

# ========= LOCAL DEBUG =========
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=port, reload=True)
