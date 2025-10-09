# main_fastapi.py
# ============================================================
# IN HOUSTON AI — MATRIZ MULTIBOT CON FLUJO EFÍMERO (FINAL OCT 2025)
# ============================================================

import os, json, base64, asyncio, websockets, requests
from fastapi import FastAPI, WebSocket, Request, Query
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

# ============================================================
# CONFIGURACIÓN GLOBAL
# ============================================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
BOTS_DIR = "bots"
DEFAULT_MODEL = "gpt-4o-realtime-preview-2024-12-17"
DEFAULT_VOICE = "alloy"

app = FastAPI(title="IN HOUSTON AI — Multibot Efímero vFinal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

def load_bot_config(bot_name: str):
    """Carga el JSON del bot especificado."""
    path = os.path.join(BOTS_DIR, f"{bot_name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No existe {path}")
    with open(path, "r") as f:
        return json.load(f)

@app.get("/")
async def root():
    return PlainTextResponse("✅ IN HOUSTON AI — MATRIZ MULTIBOT EFÍMERA LISTA")

# ============================================================
# TWIML: CREA TOKEN EFÍMERO ANTES DE DEVOLVER EL <Stream>
# ============================================================
@app.post("/twiml")
async def twiml_webhook(request: Request, bot: str = Query(...)):
    try:
        cfg = load_bot_config(bot)
    except Exception:
        return PlainTextResponse("Bot no encontrado", status_code=404)

    # --- Crear sesión efímera ---
    model = cfg.get("model", DEFAULT_MODEL)
    voice = cfg.get("voice", DEFAULT_VOICE)
    instructions = cfg.get("instructions", "")
    temperature = cfg.get("temperature", 0.8)
    turn_detection = (cfg.get("realtime", {}) or {}).get("turn_detection", "server_vad")

    session_payload = {
        "model": model,
        "voice": voice,
        "modalities": ["text", "audio"],
        "instructions": instructions,
        "turn_detection": {"type": turn_detection},
        "temperature": temperature
    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/realtime/sessions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
                "OpenAI-Beta": "realtime=v1"
            },
            json=session_payload,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        ephemeral_token = data["client_secret"]["value"]

        # ⚠️ Codificar el token para URL
        import urllib.parse
        token_safe = urllib.parse.quote(ephemeral_token, safe='')
        print(f"🔑 Sesión efímera creada para bot={bot}")

    except Exception as e:
        print("❌ Error creando sesión efímera:", e)
        return PlainTextResponse("Error al crear sesión efímera", status_code=500)

    # --- Generar TwiML con token codificado ---
    host = request.url.hostname or "inhouston-ai-api.onrender.com"
    greeting = cfg.get("greeting", "Hola, soy tu asistente de In Houston Texas.")
    twilio_voice = cfg.get("twilio", {}).get("voice", "Polly.Lucia-Neural")
    twilio_lang = cfg.get("twilio", {}).get("language", "es-MX")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="{twilio_voice}" language="{twilio_lang}">{greeting}</Say>
  <Connect>
    <Stream url="wss://{host}/media?bot={bot}&token={token_safe}" />
  </Connect>
</Response>"""
    return Response(content=xml.strip(), media_type="application/xml")


# ============================================================
# MEDIA SOCKET: CONEXIÓN TWILIO ↔ OPENAI (USA TOKEN RECIBIDO)
# ============================================================
@app.websocket("/media")
async def media_socket(ws: WebSocket, bot: str, token: str):
    await ws.accept()
    print(f"🟢 Twilio conectado (bot={bot})")

    if not token:
        print("❌ Falta token efímero")
        await ws.close(code=403)
        return

    try:
        cfg = load_bot_config(bot)
    except Exception as e:
        print(f"❌ Error cargando JSON del bot '{bot}': {e}")
        await ws.close(code=1011)
        return

    # Conectar a OpenAI Realtime usando el token efímero recibido
    headers = {
        "Authorization": f"Bearer {token}",
        "OpenAI-Beta": "realtime=v1"
    }
    model = cfg.get("model", DEFAULT_MODEL)
    uri = f"wss://api.openai.com/v1/realtime?model={model}"

    try:
        async with websockets.connect(
            uri, extra_headers=headers, subprotocols=["realtime"],
            ping_interval=10, ping_timeout=20
        ) as oai:
            print("🔗 [OpenAI] conectado (sesión efímera activa)")

            # --- Twilio → OpenAI ---
            async def twilio_to_openai():
                while True:
                    try:
                        msg = await ws.receive_text()
                        data = json.loads(msg)
                        ev = data.get("event")

                        if ev == "media":
                            ulaw_b64 = data["media"]["payload"]
                            await oai.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": ulaw_b64
                            }))
                        elif ev == "stop":
                            print("🛑 Llamada terminada")
                            await oai.close()
                            break
                    except Exception as e:
                        print("⚠️ Error Twilio→OpenAI:", e)
                        break

            # --- OpenAI → Twilio ---
            async def openai_to_twilio():
                async for raw in oai:
                    try:
                        evt = json.loads(raw)
                        typ = evt.get("type")

                        if typ in ("response.audio.delta", "response.output_audio.delta"):
                            audio_b64 = evt.get("delta") or evt.get("audio")
                            if audio_b64:
                                await ws.send_text(json.dumps({
                                    "event": "media",
                                    "media": {"payload": audio_b64}
                                }))

                        elif typ == "input_audio_buffer.speech_stopped":
                            await oai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await oai.send(json.dumps({"type": "response.create"}))

                    except Exception as e:
                        print("⚠️ Error OpenAI→Twilio:", e)
                        break

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except Exception as e:
        print("❌ Error global WS:", e)
    finally:
        print(f"🔴 Conexión cerrada para bot={bot}")
        await ws.close()

# ============================================================
# LOCAL DEBUG
# ============================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    print("🧠 IN HOUSTON AI — MATRIZ MULTIBOT EFÍMERA INICIADA")
    print(f"📦 BOTS_DIR: {BOTS_DIR}")
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=port, reload=True)
