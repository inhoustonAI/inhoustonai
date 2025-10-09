# main_fastapi.py — Multibot Twilio ↔ OpenAI Realtime (Oct 2025)
# - Un teléfono = un bot (estricto opcional)
# - Lee voz/temperatura/saludo/prompt/modelo desde bots/<slug>.json
# - μ-law 8 kHz end-to-end, VAD server y pacing ~20 ms
# - Logs de diagnóstico: ruta del JSON y resumen de claves

import os, json, base64, asyncio, websockets
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

# ===== CONFIG GLOBAL =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PORT = int(os.getenv("PORT", "8080"))

# Carpeta con los JSON de bots (relativa a la raíz del repo en Render)
BOTS_DIR = Path(os.getenv("BOTS_DIR", "bots")).resolve()

# Enrutado por número: { "+18326213202": "sundin", ... }
try:
    TWILIO_BOT_MAP: Dict[str, str] = json.loads(os.getenv("TWILIO_BOT_MAP", "{}"))
except Exception:
    TWILIO_BOT_MAP = {}

# Estricto: si el número no está en el mapa y no hay ?bot= → cuelga (sin fallback)
ROUTING_STRICT = os.getenv("ROUTING_STRICT", "1").lower() in ("1", "true", "yes")

# Permitir forzar bot con query ?bot= (útil en pruebas)
ALLOW_QUERY_OVERRIDE = os.getenv("ALLOW_QUERY_OVERRIDE", "1").lower() in ("1", "true", "yes")

print(f"🧩 BASE DIR: {Path.cwd()}")
print(f"🧩 BOTS_DIR: {BOTS_DIR} (exists={BOTS_DIR.exists()})")
print(f"🧩 TWILIO_BOT_MAP: {TWILIO_BOT_MAP}")
print(f"🧩 ROUTING_STRICT={ROUTING_STRICT}  ALLOW_QUERY_OVERRIDE={ALLOW_QUERY_OVERRIDE}")

app = FastAPI(title="In Houston AI — Twilio Realtime Bridge (Multibot)")

# CORS para Twilio
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== Utilidades de audio =====
def ulaw_silence_b64(ms: int = 20) -> str:
    """Frame de silencio μ-law 8k (20 ms). μ-law silencio = 0xFF."""
    samples = 8_000 * ms // 1000
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")

# ===== Utilidades de bots (JSON) =====
_JSON_CACHE: Dict[str, Dict[str, Any]] = {}

def _load_bot_json(slug: str) -> Optional[Dict[str, Any]]:
    """
    Carga bots/<slug>.json desde BOTS_DIR.
    Acepta alias: greeting->first_message, instructions->system_prompt.
    Imprime ruta y resumen para diagnóstico.
    """
    slug = (slug or "").strip().lower()
    if not slug:
        print("❌ [CFG] slug vacío")
        return None
    if slug in _JSON_CACHE:
        return _JSON_CACHE[slug]

    path = BOTS_DIR / f"{slug}.json"
    print(f"🔎 [CFG] Intentando cargar: {path.resolve()}")
    if not path.exists():
        print(f"❌ [CFG] NO existe: {path.resolve()}")
        return None

    try:
        with path.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"❌ [CFG] JSON inválido en {path.resolve()}: {e}")
        return None

    # Alias de compatibilidad
    if "first_message" not in cfg and "greeting" in cfg:
        cfg["first_message"] = cfg.get("greeting")
    if "system_prompt" not in cfg and "instructions" in cfg:
        cfg["system_prompt"] = cfg.get("instructions")

    # Defaults razonables
    cfg.setdefault("voice", "alloy")
    cfg.setdefault("temperature", 0.8)
    cfg.setdefault("first_message", "")
    cfg.setdefault("system_prompt", "")
    cfg.setdefault("model", "gpt-4o-realtime-preview")

    # Log de resumen (80 chars máx)
    fm = (cfg.get("first_message") or "")[:80]
    sp = (cfg.get("system_prompt") or "")[:80]
    print(f"✅ [CFG] Cargado {slug}.json | voice={cfg.get('voice')} temp={cfg.get('temperature')} model={cfg.get('model')}")
    print(f"📝 [CFG] first_message: {fm!r}")
    print(f"📝 [CFG] system_prompt: {sp!r}")

    _JSON_CACHE[slug] = cfg
    return cfg

async def _resolve_bot_slug_from_twilio(request: Request) -> Optional[str]:
    """
    Prioridad:
      1) query ?bot=slug (si ALLOW_QUERY_OVERRIDE=1)
      2) map por número To (TWILIO_BOT_MAP)
      3) si ROUTING_STRICT=1 → None (mensaje y cuelga), si no → None (sin usar)
    """
    # 1) query
    if ALLOW_QUERY_OVERRIDE:
        q = dict(request.query_params)
        if "bot" in q and q["bot"].strip():
            return q["bot"].strip().lower()

    # 2) cuerpo Twilio (form-urlencoded)
    try:
        form = await request.form()
        to_number = (form.get("To") or form.get("Called") or "").strip()
        if to_number:
            print(f"☎️  [Twilio] To={to_number}")
        if to_number and to_number in TWILIO_BOT_MAP:
            return TWILIO_BOT_MAP[to_number].strip().lower()
    except Exception as e:
        print(f"⚠️ [Twilio] No se pudo leer form: {e}")

    # 3) estricto → None (para devolver TwiML de error)
    return None

# ===== Health =====
@app.get("/")
async def root():
    return PlainTextResponse("✅ In Houston AI — FastAPI multibot (Strict-ready) listo para Twilio Realtime")

# ===== TwiML (recibe la llamada y conecta Media Stream) =====
@app.post("/twiml")
async def twiml_webhook(request: Request):
    host = request.url.hostname or "inhouston-ai-api.onrender.com"
    slug = await _resolve_bot_slug_from_twilio(request)

    if not slug:
        if ROUTING_STRICT:
            # Enrutado estricto: número no mapeado → mensaje y cuelga
            xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="es-MX">Este número no está configurado para un bot. Contacte al administrador.</Say>
  <Hangup/>
</Response>"""
            return Response(content=xml.strip(), media_type="application/xml")
        else:
            # Modo no estricto: usa un fallback "demo"
            slug = "default"

    # Cargar JSON aquí para fallar pronto si no existe
    cfg = _load_bot_json(slug)
    if not cfg:
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="es-MX">No se encontró configuración para el bot {slug}. Revise la carpeta de bots.</Say>
  <Hangup/>
</Response>"""
        return Response(content=xml.strip(), media_type="application/xml")

    # Pasamos el slug a /media por query
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/media?bot={slug}" />
  </Connect>
</Response>"""
    return Response(content=xml.strip(), media_type="application/xml")

# ===== WebSocket principal (Twilio <-> OpenAI) =====
@app.websocket("/media")
async def media_socket(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [Twilio] WebSocket /media ACCEPTED")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY en variables de entorno")
        await websocket.close()
        return

    # Resolvemos el bot por query param en el WS (viene de /twiml)
    q = dict(websocket.query_params)
    bot_slug = (q.get("bot") or "").strip().lower()
    cfg = _load_bot_json(bot_slug)
    if not cfg:
        print(f"❌ Bot JSON no encontrado para slug={bot_slug}")
        await websocket.close()
        return

    voice = cfg.get("voice", "alloy")
    temperature = float(cfg.get("temperature", 0.8))
    system_prompt = (cfg.get("system_prompt") or "").strip()
    first_message = (cfg.get("first_message") or "").strip()
    model = (cfg.get("model") or "gpt-4o-realtime-preview").strip()

    print(f"🤖 [BOT] slug={bot_slug} voice={voice} temp={temperature} model={model}")

    realtime_uri = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    stream_sid: Optional[str] = None

    # Cola de salida para Twilio (μ-law b64) y emisor ritmado (20 ms)
    outbound_queue: asyncio.Queue[str] = asyncio.Queue()

    async def _twilio_send_ulaw_b64(ulaw_b64: str):
        """Envía un frame μ-law a Twilio (con streamSid si está disponible)."""
        if websocket.application_state != WebSocketState.CONNECTED:
            return
        payload = {
            "event": "media",
            "media": {"payload": ulaw_b64},
        }
        if stream_sid:
            payload["streamSid"] = stream_sid
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception as e:
            print(f"⚠️ [Twilio] envío fallido: {e}")

    async def paced_sender():
        """
        Consumidor de la cola de salida que envía a Twilio a ~20 ms por frame.
        Si la cola se queda vacía por >60 ms, envía un silencio de 20 ms (keepalive).
        """
        SILENCE_20 = ulaw_silence_b64(20)
        while websocket.application_state == WebSocketState.CONNECTED:
            try:
                b64 = await asyncio.wait_for(outbound_queue.get(), timeout=0.06)
                await _twilio_send_ulaw_b64(b64)
                await asyncio.sleep(0.02)
            except asyncio.TimeoutError:
                await _twilio_send_ulaw_b64(SILENCE_20)

    try:
        async with websockets.connect(
            realtime_uri,
            extra_headers=headers,
            subprotocols=["realtime"],
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=10_000_000,
        ) as openai_ws:
            print("🔗 [OpenAI] Realtime CONNECTED")

            # ---- Configurar sesión con parámetros del JSON ----
            session_update = {
                "type": "session.update",
                "session": {
                    "turn_detection": {"type": "server_vad"},
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "voice": voice,
                    "modalities": ["text", "audio"],
                    "instructions": system_prompt or (
                        "Eres un asistente de voz en español latino. "
                        "Saluda breve, sé útil, agenda citas cuando aplique."
                    ),
                    "temperature": float(temperature),
                }
            }
            print(f"➡️  [OpenAI] session.update (voice={voice}, temp={temperature})")
            await openai_ws.send(json.dumps(session_update))

            # (Opcional) Saludo inicial controlado por JSON:
            if first_message:
                await openai_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"instructions": first_message}
                }))
            else:
                await openai_ws.send(json.dumps({"type": "response.create"}))

            # Lanzamos el emisor ritmado hacia Twilio
            sender_task = asyncio.create_task(paced_sender())

            # ---- Twilio -> OpenAI (μ-law directo) ----
            async def twilio_to_openai():
                nonlocal stream_sid
                try:
                    while True:
                        msg_txt = await websocket.receive_text()
                        data = json.loads(msg_txt)
                        ev = data.get("event")

                        if ev == "start":
                            stream_sid = data.get("start", {}).get("streamSid")
                            print(f"🎧 [Twilio] stream START sid={stream_sid}")

                        elif ev == "media":
                            # μ-law base64 tal cual hacia OpenAI
                            ulaw_b64 = data["media"]["payload"]
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": ulaw_b64  # g711_ulaw base64
                            }))

                        elif ev == "stop":
                            print("🛑 [Twilio] stream STOP (fin de la llamada)")
                            try:
                                await openai_ws.close()
                            except Exception:
                                pass
                            break
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")

            # ---- OpenAI -> Twilio ----
            async def openai_to_twilio():
                try:
                    async for raw in openai_ws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue

                        t = evt.get("type")

                        # Logs útiles (omitimos frames de audio para no inundar)
                        if t and t not in (
                            "response.audio.delta",
                            "response.output_audio.delta",
                            "input_audio_buffer.speech_started",
                            "input_audio_buffer.speech_stopped",
                        ):
                            print(f"ℹ️ [OpenAI] {t}")

                        # Al detectar fin de habla: commit + pedir respuesta
                        if t == "input_audio_buffer.speech_stopped":
                            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await openai_ws.send(json.dumps({"type": "response.create"}))

                        # Audio saliente (nombres varían entre previews)
                        if t in ("response.audio.delta", "response.output_audio.delta"):
                            audio_b64 = evt.get("delta") or evt.get("audio")
                            if not audio_b64:
                                continue
                            await outbound_queue.put(audio_b64)

                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

            # Cerrar emisor ritmado
            if not sender_task.done():
                sender_task.cancel()
                try:
                    await sender_task
                except asyncio.CancelledError:
                    pass

    except Exception as e:
        import traceback
        print("❌ [OpenAI] Fallo al conectar:", e)
        traceback.print_exc()
    finally:
        print("🔴 [Twilio] WebSocket CLOSED")

# ===== Debug mínimo =====
@app.get("/whoami")
async def whoami(request: Request):
    # Devuelve el bot que se resolvería para esta petición (útil para pruebas)
    slug = None
    if ALLOW_QUERY_OVERRIDE:
        q = dict(request.query_params)
        if "bot" in q and q["bot"].strip():
            slug = q["bot"].strip().lower()
    return {"bot": slug}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
