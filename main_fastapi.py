# main_fastapi.py — MEI (Matriz por JSON) para Twilio Media Streams ↔ OpenAI Realtime
# - TODO sale de bots/<slug>.json (voz, modelo, prompt, VAD, formatos, saludo opcional, idiomas).
# - Rutas: /twiml, /outgoing-call (TwiML), /media-stream (WS), /whoami (debug).
# - Barge-in real: cancelar TTS + limpiar buffer en Twilio con {"event":"clear","streamSid":...}.
# - Audio g711_ulaw 8 kHz end-to-end con pacing 20 ms.
# - Idiomas: languages.allowed/default/auto_switch → inyectados a instructions.
# - Saludo inicial EXACTO: desactivamos create_response al inicio, enviamos first_message literal, reactivamos luego.

import os, json, base64, asyncio, websockets
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState, WebSocketDisconnect

# ================== ENV ==================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PORT = int(os.getenv("PORT", "5050"))
BOTS_DIR = Path(os.getenv("BOTS_DIR", "bots"))
DEFAULT_BOT = os.getenv("DEFAULT_BOT", "sundin")  # solo decide QUÉ JSON cargar si no llega ?bot=
TWILIO_BOT_MAP: Dict[str, str] = {}
try:
    TWILIO_BOT_MAP = json.loads(os.getenv("TWILIO_BOT_MAP", "{}"))
except Exception:
    TWILIO_BOT_MAP = {}

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY es obligatorio")

# ================== APP ==================
app = FastAPI(title="MEI — Twilio MediaStreams ↔ OpenAI Realtime (JSON Matrix)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ================== UTILS ==================
def _ulaw_silence_b64(ms: int = 20) -> str:
    """Frame de silencio μ-law 8k (0xFF). 20ms = 160 bytes."""
    samples = 8_000 * ms // 1000
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")

def _strict_load_json(slug: str) -> Dict[str, Any]:
    """Carga bots/<slug>.json y valida campos. Sin defaults mágicos."""
    path = BOTS_DIR / f"{slug}.json"
    if not path.exists():
        raise HTTPException(400, f"Config no encontrada: bots/{slug}.json")
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    # Requeridos
    for k in ["voice", "temperature", "model", "system_prompt", "realtime"]:
        if k not in cfg:
            raise HTTPException(400, f"Falta '{k}' en bots/{slug}.json")

    # Normalizar VAD
    rt = cfg["realtime"] or {}
    td = rt.get("turn_detection") or {}
    if "silence_ms" in td and "silence_duration_ms" not in td:
        td["silence_duration_ms"] = td.pop("silence_ms")
    if "prefix_ms" in td and "prefix_padding_ms" not in td:
        td["prefix_padding_ms"] = td.pop("prefix_ms")
    td.setdefault("type", "server_vad")
    td.setdefault("threshold", 0.5)             # sensibilidad moderada (ajustable en JSON)
    td.setdefault("silence_duration_ms", 700)
    td.setdefault("prefix_padding_ms", 150)
    td.setdefault("create_response", True)      # server VAD crea respuesta (sin commit manual)
    td.setdefault("interrupt_response", True)   # permite interrupción
    rt["turn_detection"] = td

    # Formatos μ-law 8k (Twilio)
    rt.setdefault("input_audio_format", "g711_ulaw")
    rt.setdefault("output_audio_format", "g711_ulaw")
    cfg["realtime"] = rt

    return cfg

async def _resolve_slug(request: Request) -> str:
    """Prioridad: ?bot=...  →  TWILIO_BOT_MAP por número  →  DEFAULT_BOT"""
    qp = dict(request.query_params)
    if qp.get("bot"):
        return qp["bot"].strip().lower()
    try:
        form = await request.form()
        to_number = (form.get("To") or form.get("Called") or "").strip()
        if to_number and to_number in TWILIO_BOT_MAP:
            return TWILIO_BOT_MAP[to_number].strip().lower()
    except Exception:
        pass
    return DEFAULT_BOT

# ================== ROUTES ==================
@app.get("/", response_class=HTMLResponse)
async def index_page():
    return HTMLResponse("<h3>MEI Realtime listo</h3>")

# (A) Ruta compatible con configuración Twilio existente
@app.post("/twiml")
async def twiml(request: Request):
    host = request.url.hostname or "inhouston-ai-api.onrender.com"
    slug = await _resolve_slug(request)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/media-stream?bot={slug}" />
  </Connect>
</Response>"""
    return Response(xml.strip(), media_type="application/xml")

# (B) Alternativa /outgoing-call
@app.api_route("/outgoing-call", methods=["GET","POST"])
async def outgoing_call(request: Request):
    host = request.url.hostname or "inhouston-ai-api.onrender.com"
    slug = await _resolve_slug(request)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/media-stream?bot={slug}" />
  </Connect>
</Response>"""
    return Response(xml.strip(), media_type="application/xml")

# ================== CORE: Twilio WS <-> OpenAI Realtime ==================
@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("🟢 Twilio WS connected")

    if not OPENAI_API_KEY:
        await ws.close(); return

    # --- Cargar JSON estricto ---
    qp = dict(ws.query_params)
    bot_slug = (qp.get("bot") or DEFAULT_BOT).strip().lower()
    try:
        cfg = _strict_load_json(bot_slug)
    except HTTPException as e:
        print("❌ JSON inválido:", e.detail)
        await ws.close(); return

    voice         = cfg["voice"]
    temperature   = float(cfg["temperature"])
    system_prompt = (cfg.get("system_prompt") or "").strip()
    first_message = (cfg.get("first_message") or "").strip()  # opcional
    model         = cfg["model"].strip()

    rt       = cfg["realtime"]
    in_fmt   = rt["input_audio_format"]      # g711_ulaw
    out_fmt  = rt["output_audio_format"]     # g711_ulaw
    turn_det = rt["turn_detection"]

    # Idiomas desde JSON (allowed/default/auto_switch) → se inyectan en instructions
    langs_cfg   = (cfg.get("languages") or {})
    allowed     = langs_cfg.get("allowed") or ["es-US", "en-US"]
    default_lng = langs_cfg.get("default") or "es-US"
    auto_switch = bool(langs_cfg.get("auto_switch", True))
    lang_clause = [
        f"Idiomas permitidos: {', '.join(allowed)}.",
        f"Idioma por defecto: {default_lng}.",
        ("Cambia automáticamente al idioma del usuario si detectas otro de los permitidos."
         if auto_switch else
         "No cambies de idioma automáticamente; mantén el idioma por defecto salvo petición explícita del usuario.")
    ]

    # Instrucciones finales (prompt del JSON + reglas de idioma + anti-personalización del saludo)
    merged_instructions = (system_prompt + " " + " ".join(lang_clause)).strip()
    merged_instructions += (
        " El primer enunciado DEBE ser exactamente el contenido de 'first_message' si existe; "
        "no agregues ni quites palabras, no uses el nombre del cliente a menos que él mismo lo diga. "
        "Nunca saludes con 'Hola, Sarah' ni variantes, porque 'Sara' es el nombre de la agente, no del cliente."
    )

    print(f"🤖 bot={bot_slug} model={model} voice={voice}")
    print(f"🗂️ first_message={'(none)' if not first_message else first_message[:100]!r}")
    print(f"🎛️ realtime={rt}")
    print(f"🌐 languages: allowed={allowed} default={default_lng} auto_switch={auto_switch}")

    # --- Conectar a OpenAI Realtime ---
    ai_url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}

    stream_sid: Optional[str] = None
    user_speaking = False
    out_q: asyncio.Queue[str] = asyncio.Queue()

    async def twilio_send_ulaw(b64: str):
        if ws.application_state != WebSocketState.CONNECTED:
            return
        payload = {"event":"media","media":{"payload": b64}}
        if stream_sid: payload["streamSid"] = stream_sid
        try:
            await ws.send_json(payload)
        except Exception as e:
            print("⚠️ send media fail:", e)

    async def twilio_clear():
        """Vacía el buffer de reproducción en Twilio (imprescindible para barge-in real)."""
        if ws.application_state != WebSocketState.CONNECTED or not stream_sid:
            return
        try:
            await ws.send_json({"event":"clear","streamSid":stream_sid})
            print("🧹 Twilio buffer CLEAR enviado")
        except Exception as e:
            print("⚠️ clear fail:", e)

    async def drain_out():
        try:
            while True:
                out_q.get_nowait()
                out_q.task_done()
        except asyncio.QueueEmpty:
            pass

    async def paced_sender():
        SIL = _ulaw_silence_b64(20)
        while ws.application_state == WebSocketState.CONNECTED:
            try:
                if user_speaking:
                    await twilio_send_ulaw(SIL); await asyncio.sleep(0.020); continue
                chunk = await asyncio.wait_for(out_q.get(), timeout=0.060)
                await twilio_send_ulaw(chunk); out_q.task_done()
                await asyncio.sleep(0.020)
            except asyncio.TimeoutError:
                await twilio_send_ulaw(SIL)

    try:
        async with websockets.connect(
            ai_url, extra_headers=headers, subprotocols=["realtime"],
            ping_interval=20, ping_timeout=20, close_timeout=5, max_size=10_000_000
        ) as aiws:
            print("🔗 OpenAI Realtime CONNECTED")

            # ARRANQUE: si hay first_message, desactivar create_response para que NO se adelante el modelo
            td_boot = dict(turn_det)
            initial_greeting = bool(first_message)
            if initial_greeting:
                td_boot["create_response"] = False

            # 1) session.update con instrucciones y td_boot (si aplica)
            session_update = {
                "type": "session.update",
                "session": {
                    "turn_detection": td_boot if initial_greeting else turn_det,
                    "input_audio_format": in_fmt,
                    "output_audio_format": out_fmt,
                    "voice": voice,
                    "modalities": ["audio","text"],
                    "instructions": merged_instructions,
                    "temperature": temperature
                }
            }
            await aiws.send(json.dumps(session_update))
            print("➡️ session.update enviado")

            # 2) Saludo inicial literal (si existe)
            initial_response_id = None
            waiting_initial_done = initial_greeting

            if first_message:
                await aiws.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "modalities": ["audio","text"],
                        "voice": voice,
                        "output_audio_format": out_fmt,
                        "instructions": first_message
                    }
                }))

            sender_task = asyncio.create_task(paced_sender())

            # ---- Twilio → OpenAI ----
            async def t2o():
                nonlocal stream_sid
                try:
                    async for txt in ws.iter_text():
                        data = json.loads(txt)
                        ev = data.get("event")

                        if ev == "start":
                            stream_sid = data.get("start", {}).get("streamSid")
                            print(f"🎧 Twilio START sid={stream_sid}")

                        elif ev == "media":
                            ulaw_b64 = data["media"]["payload"]
                            await aiws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": ulaw_b64
                            }))

                        elif ev == "stop":
                            print("🛑 Twilio STOP")
                            try: await aiws.close()
                            except Exception: pass
                            break
                except WebSocketDisconnect:
                    print("🔴 Twilio WS disconnect")
                    try: await aiws.close()
                    except Exception: pass
                except Exception as e:
                    print("⚠️ Twilio→OpenAI error:", e)
                    try: await aiws.close()
                    except Exception: pass

            # ---- OpenAI → Twilio ----
            async def o2t():
                nonlocal user_speaking, initial_response_id, waiting_initial_done
                try:
                    async for raw in aiws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue

                        t = evt.get("type")
                        if t and t not in ("response.audio.delta","response.output_audio.delta",
                                           "input_audio_buffer.speech_started","input_audio_buffer.speech_stopped"):
                            print("ℹ️", t, "::", evt)

                        if t == "error":
                            print("❌ OpenAI ERROR:", evt)

                        # Identificar el ID del saludo inicial
                        if t == "response.created" and waiting_initial_done and initial_response_id is None:
                            initial_response_id = (evt.get("response") or {}).get("id")

                        # Cuando el saludo inicial termina -> reactivar create_response=True
                        if t == "response.done" and waiting_initial_done:
                            resp = evt.get("response") or {}
                            if resp.get("id") == initial_response_id:
                                new_td = dict(turn_det)
                                new_td["create_response"] = True
                                await aiws.send(json.dumps({
                                    "type": "session.update",
                                    "session": { "turn_detection": new_td }
                                }))
                                print("✅ VAD reactivado: create_response=True tras el saludo literal")
                                waiting_initial_done = False

                        if t == "input_audio_buffer.speech_started":
                            user_speaking = True
                            print("👂 speech_started → cancelar TTS y limpiar buffers")
                            await aiws.send(json.dumps({"type":"response.cancel"}))
                            await drain_out()
                            await twilio_clear()

                        if t == "input_audio_buffer.speech_stopped":
                            user_speaking = False
                            print("🗣️ speech_stopped (server VAD creará respuesta)")

                        if t in ("response.audio.delta","response.output_audio.delta"):
                            if user_speaking:
                                continue
                            audio_b64 = evt.get("delta") or evt.get("audio")
                            if audio_b64:
                                await out_q.put(audio_b64)
                except Exception as e:
                    print("⚠️ OpenAI→Twilio error:", e)

            await asyncio.gather(t2o(), o2t())

            if not sender_task.done():
                sender_task.cancel()
                try: await sender_task
                except asyncio.CancelledError: pass

    except Exception as e:
        import traceback
        print("❌ Conexión Realtime falló:", e)
        traceback.print_exc()
    finally:
        print("🔴 Twilio WS CLOSED")

# ================== DEBUG ==================
@app.get("/whoami")
async def whoami(request: Request):
    slug = await _resolve_slug(request)
    return {"bot": slug}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
