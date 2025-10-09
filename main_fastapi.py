# main_fastapi.py
# ======================================================
# 🧠 IN HOUSTON AI — MATRIZ MULTIBOT (OCT 2025)
# ======================================================
# Esta versión permite manejar múltiples asistentes (bots)
# Cada número telefónico o identidad tiene su propio JSON
# Ejemplo: /twiml?bot=sundin → usa bots/sundin.json
# ======================================================

import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request, Query
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

# ===== CONFIGURACIÓN GLOBAL =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PORT = int(os.getenv("PORT", 8080))
BOTS_DIR = "bots"

print(f"🧠 IN HOUSTON AI — MATRIZ MULTIBOT INICIADA")
print(f"📦 BOTS_DIR: {BOTS_DIR}")

# ===== INICIO FASTAPI =====
app = FastAPI(title="IN HOUSTON AI — Multibot Realtime System")

# ===== CORS =====
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

# ======================================================
# 🔹 CARGA DE CONFIGURACIÓN DEL BOT
# ======================================================
def load_bot_config(bot_name: str):
    """Carga el JSON del bot desde la carpeta /bots"""
    bot_path = os.path.join(BOTS_DIR, f"{bot_name.lower()}.json")
    if not os.path.exists(bot_path):
        raise FileNotFoundError(f"❌ No se encontró el archivo {bot_path}")
    with open(bot_path, "r", encoding="utf-8") as f:
        return json.load(f)

# ======================================================
# 🔹 ENDPOINT DE SALUD
# ======================================================
@app.get("/")
async def root():
    return PlainTextResponse("✅ IN HOUSTON AI — MATRIZ MULTIBOT ACTIVA")

# ======================================================
# 🔹 TWIML: ENTRADA DE LLAMADA TWILIO
# ======================================================
@app.post("/twiml")
async def twiml_webhook(request: Request, bot: str = Query(...)):
    """
    Twilio llama a /twiml?bot=sundin
    Cargamos el JSON de ese bot y devolvemos el TwiML correspondiente
    """
    try:
        cfg = load_bot_config(bot)
    except Exception as e:
        print(f"⚠️ Error cargando bot '{bot}': {e}")
        return PlainTextResponse("Bot no encontrado", status_code=404)

    host = request.url.hostname or "inhouston-ai-api.onrender.com"
    twilio_cfg = cfg.get("twilio", {})
    twilio_voice = twilio_cfg.get("voice", "Polly.Lucia-Neural")
    twilio_lang = twilio_cfg.get("language", "es-MX")
    greeting = cfg.get("greeting", f"Hola, soy {bot.capitalize()} de In Houston Texas.")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="{twilio_voice}" language="{twilio_lang}">{greeting}</Say>
  <Connect>
    <Stream url="wss://{host}/media?bot={bot.lower()}" />
  </Connect>
</Response>"""

    return Response(content=xml.strip(), media_type="application/xml")

# ======================================================
# 🔹 FUNCIONES AUXILIARES
# ======================================================
def ulaw_silence_b64(ms: int = 20) -> str:
    """Frame de silencio μ-law 8k (20 ms)."""
    samples = 8_000 * ms // 1000
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")

# ======================================================
# 🔹 WEBSOCKET PRINCIPAL — CONEXIÓN TWILIO <-> OPENAI
# ======================================================
@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    bot = websocket.query_params.get("bot")
    if not bot:
        print("❌ Conexión rechazada: falta parámetro ?bot=")
        await websocket.close(code=403)
        return

    await websocket.accept()
    print(f"🟢 [Twilio] Conexión WS iniciada para bot={bot}")


    # Configuración dinámica según el bot
    model = cfg.get("model", "gpt-4o-realtime-preview")
    voice = cfg.get("voice", "alloy")
    temperature = cfg.get("temperature", 0.8)
    instructions = cfg.get("instructions", "Eres un asistente profesional de In Houston Texas.")
    turn_detection = cfg.get("realtime", {}).get("turn_detection", "server_vad")
    input_fmt = cfg.get("realtime", {}).get("input_audio_format", "g711_ulaw")
    output_fmt = cfg.get("realtime", {}).get("output_audio_format", "g711_ulaw")

    # Conexión con OpenAI Realtime
    uri = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }

    # Cola de audio saliente (para Twilio)
    outbound_queue: asyncio.Queue[str] = asyncio.Queue()

    async def twilio_send(ulaw_b64: str):
        """Envía audio μ-law a Twilio."""
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_text(json.dumps({
                "event": "media",
                "media": {"payload": ulaw_b64}
            }))

    async def paced_sender():
        """Mantiene el ritmo de envío hacia Twilio (~20ms por frame)."""
        SILENCE_20 = ulaw_silence_b64(20)
        while websocket.application_state == WebSocketState.CONNECTED:
            try:
                frame = await asyncio.wait_for(outbound_queue.get(), timeout=0.06)
                await twilio_send(frame)
                await asyncio.sleep(0.02)
            except asyncio.TimeoutError:
                await twilio_send(SILENCE_20)

    try:
        async with websockets.connect(uri, extra_headers=headers, subprotocols=["realtime"]) as oai:
            print(f"🔗 [OpenAI] Conectado — Bot: {bot}")

            # Configuración de sesión del bot
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

            # Iniciar el emisor ritmado
            sender_task = asyncio.create_task(paced_sender())

            # Twilio -> OpenAI
            async def twilio_to_openai():
                try:
                    while True:
                        msg_txt = await websocket.receive_text()
                        data = json.loads(msg_txt)
                        ev = data.get("event")
                        if ev == "media":
                            await oai.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"]
                            }))
                        elif ev == "stop":
                            print(f"🛑 [Twilio] llamada finalizada (bot={bot})")
                            break
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")

            # OpenAI -> Twilio
            async def openai_to_twilio():
                try:
                    async for raw in oai:
                        evt = json.loads(raw)
                        t = evt.get("type")

                        if t == "input_audio_buffer.speech_stopped":
                            await oai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await oai.send(json.dumps({"type": "response.create"}))

                        if t in ("response.audio.delta", "response.output_audio.delta"):
                            b64 = evt.get("delta") or evt.get("audio")
                            if b64:
                                await outbound_queue.put(b64)
                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

            # Detener el emisor
            if not sender_task.done():
                sender_task.cancel()
                try:
                    await sender_task
                except asyncio.CancelledError:
                    pass

    except Exception as e:
        import traceback
        print(f"❌ [OpenAI] Error en conexión del bot {bot}: {e}")
        traceback.print_exc()

    finally:
        print(f"🔴 [Twilio] WebSocket cerrado ({bot})")

# ======================================================
# 🔹 LOCAL DEBUG
# ======================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
