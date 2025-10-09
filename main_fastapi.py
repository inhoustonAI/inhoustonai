# main_fastapi.py
# Versión multibot Twilio ↔ OpenAI Realtime (oct 2025)
# - μ-law 8 kHz end-to-end (sin resampling)
# - VAD de servidor (turn_detection) en OpenAI (parametrizable por JSON)
# - Ritmo 20 ms hacia Twilio para audio estable (keepalive si no hay cola)
# - MATRIZ: saludo, instrucciones, voz, temperatura, modelo y VAD vienen del JSON en bots/<slug>.json

import os
import json
import base64
import asyncio
import websockets
import time
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState, WebSocketDisconnect

# ===== CONFIG GLOBAL =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PORT = int(os.getenv("PORT", "8080"))

# Carpeta de bots y fallback
BOTS_DIR = Path(os.getenv("BOTS_DIR", "bots"))
DEFAULT_BOT = os.getenv("DEFAULT_BOT", "sundin")

# Mapa opcional: número de destino (To) → slug de bot
_TWILIO_BOT_MAP_RAW = os.getenv("TWILIO_BOT_MAP", "{}")
try:
    TWILIO_BOT_MAP: Dict[str, str] = json.loads(_TWILIO_BOT_MAP_RAW)
except Exception:
    TWILIO_BOT_MAP = {}

print(f"🧩 Using PORT: {PORT}")
print(f"🧩 BOTS_DIR: {BOTS_DIR.resolve()} (exists={BOTS_DIR.exists()})")
print(f"🧩 DEFAULT_BOT: {DEFAULT_BOT}")
print(f"🧩 TWILIO_BOT_MAP: {TWILIO_BOT_MAP}")

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
    samples = 8_000 * ms // 1000  # 160 bytes para 20 ms
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")

# ===== Utilidades de bots (JSON) =====
_JSON_CACHE: Dict[str, Dict[str, Any]] = {}

def _load_bot_json(slug: str) -> Dict[str, Any]:
    """
    Carga bots/<slug>.json. Si falla, intenta DEFAULT_BOT.
    Estructura mínima esperada:
      {
        "system_prompt": "texto...",
        "first_message": "Hola, soy ... ¿en qué te ayudo?",
        "voice": "alloy",
        "temperature": 0.8,
        "model": "gpt-4o-realtime-preview",
        "vad": {
            "type": "server_vad",
            "silence_ms": 550,
            "prefix_ms": 150
        }
      }
    """
    slug = (slug or "").strip().lower() or DEFAULT_BOT
    if slug in _JSON_CACHE:
        return _JSON_CACHE[slug]

    def read_json(s: str) -> Optional[Dict[str, Any]]:
        path = BOTS_DIR / f"{s}.json"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    cfg = read_json(slug)
    if cfg is None and slug != DEFAULT_BOT:
        cfg = read_json(DEFAULT_BOT)

    if cfg is None:
        # Último recurso: configuración muy básica inline
        cfg = {
            "system_prompt": (
                "Eres el asistente de voz de In Houston Texas. "
                "Hablas en español latino, cálido y profesional. "
                "Respondes breve, haces preguntas claras y ayudas a agendar citas."
            ),
            "first_message": "Hola, soy el asistente de In Houston Texas. ¿En qué te ayudo?",
            "voice": "alloy",
            "temperature": 0.8,
            "model": "gpt-4o-realtime-preview",
            "vad": {"type": "server_vad", "silence_ms": 550, "prefix_ms": 150}
        }

    # Normalización de claves obligatorias + defaults
    cfg.setdefault("voice", "alloy")
    cfg.setdefault("temperature", 0.8)
    cfg.setdefault("system_prompt", "")
    cfg.setdefault("model", "gpt-4o-realtime-preview")
    cfg.setdefault("vad", {"type": "server_vad", "silence_ms": 550, "prefix_ms": 150})
    _JSON_CACHE[slug] = cfg
    return cfg

def _canon_e164(num: str) -> str:
    """Normaliza un número de teléfono al formato E.164 básico para mapear TWILIO_BOT_MAP."""
    s = (num or "").strip()
    # Twilio envía +1XXXXXXXXXX; aceptamos con o sin guiones/espacios
    s = s.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if s.startswith("00"):
        s = "+" + s[2:]
    return s

async def _resolve_bot_slug_from_twilio(request: Request) -> str:
    """
    Prioridad:
      1) query ?bot=slug
      2) map por número To (TWILIO_BOT_MAP)
      3) DEFAULT_BOT
    """
    # 1) query
    q = dict(request.query_params)
    if "bot" in q and q["bot"].strip():
        return q["bot"].strip().lower()

    # 2) cuerpo Twilio (form-urlencoded)
    try:
        form = await request.form()
        to_number = _canon_e164(form.get("To") or form.get("Called") or "")
        if to_number and to_number in TWILIO_BOT_MAP:
            return TWILIO_BOT_MAP[to_number].strip().lower()
    except Exception:
        pass

    # 3) fallback
    return DEFAULT_BOT

# ===== Health =====
@app.get("/")
async def root():
    return PlainTextResponse("✅ In Houston AI — FastAPI multibot listo para Twilio Realtime")

# ===== TwiML (recibe la llamada y conecta Media Stream) =====
@app.post("/twiml")
async def twiml_webhook(request: Request):
    host = request.url.hostname or "inhouston-ai-api.onrender.com"
    slug = await _resolve_bot_slug_from_twilio(request)
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

    # Resolvemos el bot por query param en el WS
    q = dict(websocket.query_params)
    bot_slug = (q.get("bot") or DEFAULT_BOT).strip().lower()
    cfg = _load_bot_json(bot_slug)

    voice = cfg.get("voice", "alloy")
    temperature = float(cfg.get("temperature", 0.8))
    system_prompt = (cfg.get("system_prompt") or "").strip()
    first_message = (cfg.get("first_message") or "").strip()
    model = (cfg.get("model") or "gpt-4o-realtime-preview").strip()

    # VAD opcional por JSON
    vad_cfg = cfg.get("vad") or {}
    vad_type = (vad_cfg.get("type") or "server_vad").strip()
    vad_silence_ms = int(vad_cfg.get("silence_ms", 550))
    vad_prefix_ms = int(vad_cfg.get("prefix_ms", 150))

    print(f"🤖 [BOT] slug={bot_slug} model={model} voice={voice} temp={temperature}")

    realtime_uri = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    stream_sid: Optional[str] = None

    # Cola de salida para Twilio (μ-law b64) y emisor ritmado (20 ms)
    outbound_queue: asyncio.Queue[str] = asyncio.Queue()

    # ---- Estado de conversación para barge-in, métricas y latencia baja ----
    speaking_out = False        # True mientras el bot está emitiendo TTS
    barge_cancel_sent = False   # evita enviar cancel más de una vez por respuesta
    last_tts_ts = 0.0           # timestamp del último frame TTS enviado
    last_audio_rx_ts = 0.0      # timestamp del último frame de audio ENTRANTE (usuario)
    pending_audio_since = 0.0   # primer instante en que recibimos audio de usuario no comiteado

    # NUEVO: contadores y umbrales para evitar commits vacíos y barge-in falso
    USER_FRAME_MS = 20                 # Twilio Media Streams → ~20 ms por frame μ-law
    user_ms_since_last_commit = 0      # audio acumulado desde el último commit
    user_ms_while_bot_talking = 0      # audio del usuario que coincidió con TTS (para barge-in)

    BARGE_MIN_USER_MS = 120            # ms mínimos de voz del usuario para cancelar TTS
    COMMIT_MIN_MS = 120                # ms mínimos para poder commit sin error de buffer vacío
    FORCED_COMMIT_INACT_MS = 0.8       # s de inactividad desde que empezó input antes de forzar
    FORCED_COMMIT_GAP_MS = 0.6         # s de gap sin frames para forzar

    async def _twilio_send_ulaw_b64(ulaw_b64: str):
        """Envía un frame μ-law a Twilio (con streamSid si está disponible)."""
        if websocket.application_state != WebSocketState.CONNECTED:
            return
        payload = {"event": "media", "media": {"payload": ulaw_b64}}
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
        try:
            while websocket.application_state == WebSocketState.CONNECTED:
                try:
                    b64 = await asyncio.wait_for(outbound_queue.get(), timeout=0.06)
                    await _twilio_send_ulaw_b64(b64)
                    await asyncio.sleep(0.02)
                except asyncio.TimeoutError:
                    await _twilio_send_ulaw_b64(SILENCE_20)
        except Exception as e:
            print(f"⚠️ [paced_sender] detenido: {e}")

    async def forced_committer(openai_ws):
        """
        Si hubo audio de usuario y pasan umbrales sin speech_stopped,
        forzamos commit para reducir latencia percibida, PERO solo si el buffer supera COMMIT_MIN_MS.
        """
        nonlocal pending_audio_since, last_audio_rx_ts, user_ms_since_last_commit
        try:
            while websocket.application_state == WebSocketState.CONNECTED:
                await asyncio.sleep(0.2)
                now = time.time()

                # 1) Inactividad desde que empezó el input del usuario
                if pending_audio_since and (now - pending_audio_since) >= FORCED_COMMIT_INACT_MS:
                    if user_ms_since_last_commit >= COMMIT_MIN_MS:
                        print(f"⏱️ [Force] commit por inactividad VAD ({FORCED_COMMIT_INACT_MS}s)")
                        await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                        user_ms_since_last_commit = 0
                        pending_audio_since = 0.0
                        await openai_ws.send(json.dumps({"type": "response.create"}))
                    else:
                        # No hay suficiente audio; rearmamos reloj para no spamear commits vacíos
                        pending_audio_since = now

                # 2) Gap sin recibir audio entrante
                if last_audio_rx_ts and (now - last_audio_rx_ts) >= FORCED_COMMIT_GAP_MS and pending_audio_since:
                    if user_ms_since_last_commit >= COMMIT_MIN_MS:
                        print(f"⏱️ [Force] commit por gap de audio ({FORCED_COMMIT_GAP_MS}s)")
                        await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                        user_ms_since_last_commit = 0
                        pending_audio_since = 0.0
                        await openai_ws.send(json.dumps({"type": "response.create"}))
                    else:
                        # Gap pero sin suficiente audio: limpia estado para evitar commits vacíos
                        pending_audio_since = 0.0
        except Exception as e:
            print(f"⚠️ [forced_committer] {e}")

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
                    "turn_detection": {
                        "type": vad_type,
                        # Algunos previews aceptan estos campos:
                        "silence_duration_ms": vad_silence_ms,
                        "prefix_padding_ms": vad_prefix_ms,
                    },
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "voice": voice,
                    "modalities": ["text", "audio"],
                    "instructions": system_prompt or (
                        "Eres un asistente de voz en español latino. "
                        "Saluda breve, sé útil, agenda citas cuando aplique."
                    ),
                    "temperature": temperature,
                }
            }
            print(f"➡️  [OpenAI] session.update (voice={voice}, temp={temperature}, vad={vad_type})")
            await openai_ws.send(json.dumps(session_update))

            # (Opcional) Saludo inicial controlado por JSON
            if first_message:
                await openai_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"instructions": first_message}
                }))
                print("👋 [OpenAI] first_message disparado")
            else:
                await openai_ws.send(json.dumps({"type": "response.create"}))
                print("👋 [OpenAI] response.create inicial sin first_message")

            # Lanzamos el emisor ritmado hacia Twilio + commit forzado
            sender_task = asyncio.create_task(paced_sender())
            force_task = asyncio.create_task(forced_committer(openai_ws))

            # ---- Twilio -> OpenAI (μ-law directo) ----
            async def twilio_to_openai():
                nonlocal stream_sid, last_audio_rx_ts, pending_audio_since
                nonlocal speaking_out, barge_cancel_sent
                nonlocal user_ms_since_last_commit, user_ms_while_bot_talking
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

                            # --- contabilizar audio entrante ---
                            last_audio_rx_ts = time.time()
                            user_ms_since_last_commit += USER_FRAME_MS
                            if speaking_out:
                                user_ms_while_bot_talking += USER_FRAME_MS

                            # --------- BAR­GE-IN con debounce (≥120 ms) ---------
                            if speaking_out and not barge_cancel_sent and user_ms_while_bot_talking >= BARGE_MIN_USER_MS:
                                try:
                                    print("🛑 [BARGE-IN] user speaking ≥120ms → response.cancel + flush queue")
                                    # 1) Cancelar respuesta en curso
                                    await openai_ws.send(json.dumps({"type": "response.cancel"}))
                                    barge_cancel_sent = True
                                    # 2) Vaciar cola de salida para dejar de hablar YA
                                    try:
                                        while True:
                                            outbound_queue.get_nowait()
                                            outbound_queue.task_done()
                                    except asyncio.QueueEmpty:
                                        pass
                                    # 3) Estados
                                    speaking_out = False
                                    user_ms_while_bot_talking = 0
                                except Exception as e:
                                    print(f"⚠️ [BARGE-IN] fallo al cancelar: {e}")

                            # Enviar audio del usuario
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": ulaw_b64  # g711_ulaw base64
                            }))

                            if not pending_audio_since:
                                pending_audio_since = last_audio_rx_ts

                        elif ev == "mark":
                            pass  # opcional

                        elif ev == "stop":
                            print("🛑 [Twilio] stream STOP (fin de la llamada)")
                            try:
                                await openai_ws.close()
                            except Exception:
                                pass
                            break
                except WebSocketDisconnect:
                    print("🔻 [Twilio] WebSocket disconnect")
                    try:
                        await openai_ws.close()
                    except Exception:
                        pass
                except Exception as e:
                    print(f"⚠️ [Twilio→OpenAI] Error: {e}")
                    try:
                        await openai_ws.close()
                    except Exception:
                        pass

            # ---- OpenAI -> Twilio ----
            async def openai_to_twilio():
                nonlocal speaking_out, barge_cancel_sent
                nonlocal user_ms_since_last_commit, user_ms_while_bot_talking, pending_audio_since
                try:
                    async for raw in openai_ws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue

                        t = evt.get("type")

                        # Errores con detalle (para depurar "ℹ️ error")
                        if t == "error":
                            print(f"🛑 [OpenAI ERROR] {json.dumps(evt, ensure_ascii=False)}")
                            continue

                        # Logs útiles (omitimos frames de audio para no inundar)
                        if t and t not in (
                            "response.audio.delta",
                            "response.output_audio.delta",
                            "input_audio_buffer.speech_started",
                            "input_audio_buffer.speech_stopped",
                        ):
                            print(f"ℹ️ [OpenAI] {t}")

                        # Fin de habla detectado por el VAD del modelo
                        if t == "input_audio_buffer.speech_stopped":
                            print("✂️  [OpenAI] speech_stopped → commit + response.create")
                            if user_ms_since_last_commit >= COMMIT_MIN_MS:
                                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                user_ms_since_last_commit = 0
                                pending_audio_since = 0.0
                                await openai_ws.send(json.dumps({"type": "response.create"}))
                            else:
                                print(f"ℹ️ [Guard] speech_stopped con buffer < {COMMIT_MIN_MS}ms; no commit")

                        # Audio saliente (nombres varían entre previews)
                        if t in ("response.audio.delta", "response.output_audio.delta"):
                            audio_b64 = evt.get("delta") or evt.get("audio")
                            if not audio_b64:
                                continue
                            speaking_out = True
                            last_tts_ts = time.time()
                            await outbound_queue.put(audio_b64)

                        # Fin de item/respuesta → dejar de "hablar" y resetear barge
                        if t in ("response.output_item.done", "response.done"):
                            speaking_out = False
                            barge_cancel_sent = False
                            user_ms_while_bot_talking = 0  # ya no solapa

                except Exception as e:
                    print(f"⚠️ [OpenAI→Twilio] Error: {e}")

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

            # Cerrar tareas auxiliares
            for task in (sender_task, force_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    except Exception as e:
        import traceback
        print("❌ [OpenAI] Fallo al conectar:", e)
        traceback.print_exc()
    finally:
        print("🔴 [Twilio] WebSocket CLOSED")
        if websocket.application_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass

# ===== Local debug =====
@app.get("/whoami")
async def whoami(request: Request):
    # Pequeña ayuda para ver qué bot se resolvería por esta petición
    slug = await _resolve_bot_slug_from_twilio(request)
    return {"bot": slug}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
