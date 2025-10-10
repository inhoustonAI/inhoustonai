# main_fastapi.py — MEI (Minimal, Estricto, Inmutable por JSON)
# Twilio Media Streams (μ-law 8k) ↔ OpenAI Realtime (g711_ulaw)
# - TODO VIENE del bots/<slug>.json. Si falta algo crítico → 400 y se corta.
# - server_vad con create_response/interrupt_response (NO commits manuales).
# - Saludo inicial SOLO si hay "first_message" en el JSON.
# - Barge-in: response.cancel + CLEAR a Twilio (vaciar buffer en su lado).

import os, json, base64, asyncio, websockets
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.responses import Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState, WebSocketDisconnect

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PORT = int(os.getenv("PORT", "8080"))
BOTS_DIR = Path(os.getenv("BOTS_DIR", "bots"))
DEFAULT_BOT = os.getenv("DEFAULT_BOT", "sundin")  # solo para ruteo si no llega ?bot
ENV_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "").strip()

# Mapa opcional de número Twilio → slug
try:
    TWILIO_BOT_MAP: Dict[str, str] = json.loads(os.getenv("TWILIO_BOT_MAP", "{}"))
except Exception:
    TWILIO_BOT_MAP = {}

app = FastAPI(title="In Houston AI — MEI Realtime Bridge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ---------- Utils ----------
def ulaw_silence_b64(ms: int = 20) -> str:
    """μ-law 8k silencio (0xFF). 20ms = 160 bytes."""
    samples = 8_000 * ms // 1000
    return base64.b64encode(b"\xFF" * samples).decode("utf-8")

def _strict_load_json(slug: str) -> Dict[str, Any]:
    """Carga bots/<slug>.json sin defaults 'mágicos'. Si falta, 400."""
    path = BOTS_DIR / f"{slug}.json"
    if not path.exists():
        raise HTTPException(400, f"Config JSON no encontrado: bots/{slug}.json")

    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    # Validación estricta de campos mínimos
    must = ["voice", "temperature", "model", "system_prompt", "realtime"]
    for k in must:
        if k not in cfg:
            raise HTTPException(400, f"Falta '{k}' en bots/{slug}.json")

    # realtime.turn_detection con claves válidas
    rt = cfg.get("realtime") or {}
    td = rt.get("turn_detection") or {}
    # Normalizar posibles nombres antiguos
    if "silence_ms" in td and "silence_duration_ms" not in td:
        td["silence_duration_ms"] = td.pop("silence_ms")
    if "prefix_ms" in td and "prefix_padding_ms" not in td:
        td["prefix_padding_ms"] = td.pop("prefix_ms")
    td.setdefault("type", "server_vad")
    td.setdefault("create_response", True)
    td.setdefault("interrupt_response", True)
    rt["turn_detection"] = td

    # Formatos de audio: Twilio requiere g711 μ-law 8k; el modelo lo soporta.
    # output_audio_format: pcm16 | g711_ulaw | g711_alaw (doc)
    # input_audio_format idem. Usamos g711_ulaw.  :contentReference[oaicite:3]{index=3}
    rt.setdefault("input_audio_format", "g711_ulaw")
    rt.setdefault("output_audio_format", "g711_ulaw")
    cfg["realtime"] = rt

    # Si el modelo no viene en JSON, como último recurso ENV_MODEL (pero no otro default)
    if not cfg.get("model"):
        cfg["model"] = ENV_MODEL
    if not cfg["model"]:
        raise HTTPException(400, f"Falta 'model' en bots/{slug}.json y OPENAI_REALTIME_MODEL no está definido.")

    return cfg

async def _resolve_slug(request: Request) -> str:
    # 1) ?bot=slug
    q = dict(request.query_params)
    if q.get("bot"):
        return q["bot"].strip().lower()

    # 2) Body Twilio (To/Called)
    try:
        form = await request.form()
        to_number = (form.get("To") or form.get("Called") or "").strip()
        if to_number and to_number in TWILIO_BOT_MAP:
            return TWILIO_BOT_MAP[to_number].strip().lower()
    except Exception:
        pass

    # 3) DEFAULT_BOT sólo para seleccionar archivo; TODO se toma del JSON
    return DEFAULT_BOT

# ---------- Endpoints ----------
@app.get("/")
async def root():
    return PlainTextResponse("✅ MEI Realtime listo")

@app.post("/twiml")
async def twiml(request: Request):
    host = request.url.hostname or "inhouston-ai-api.onrender.com"
    slug = await _resolve_slug(request)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/media?bot={slug}" />
  </Connect>
</Response>"""
    return Response(content=xml.strip(), media_type="application/xml")

@app.websocket("/media")
async def media(websocket: WebSocket):
    await websocket.accept()
    print("🟢 [/media] Twilio WS ACCEPTED")

    if not OPENAI_API_KEY:
        print("❌ Falta OPENAI_API_KEY")
        await websocket.close(); return

    # --- Cargar config estricta desde JSON ---
    q = dict(websocket.query_params)
    bot_slug = (q.get("bot") or DEFAULT_BOT).strip().lower()
    try:
        cfg = _strict_load_json(bot_slug)
    except HTTPException as e:
        print(f"❌ Config inválida: {e.detail}")
        await websocket.close(); return

    voice         = cfg["voice"]
    temperature   = float(cfg["temperature"])
    system_prompt = (cfg.get("system_prompt") or "").strip()
    first_message = (cfg.get("first_message") or "").strip()  # opcional
    model         = cfg["model"].strip()

    rt       = cfg["realtime"]
    in_fmt   = rt["input_audio_format"]       # g711_ulaw
    out_fmt  = rt["output_audio_format"]      # g711_ulaw
    turn_det = rt["turn_detection"]           # dict completo (server_vad)

    print(f"🤖 BOT={bot_slug} model={model} voice={voice} temp={temperature}")
    print(f"🗂️ JSON first_message={'(none)' if not first_message else first_message[:120]!r}")
    print(f"🎛️ realtime={rt}")

    # --- Conectar a OpenAI Realtime ---
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}

    stream_sid: Optional[str] = None
    user_speaking = False
    q_out: asyncio.Queue[str] = asyncio.Queue()

    async def twilio_send(ulaw_b64: str):
        if websocket.application_state != WebSocketState.CONNECTED: return
        payload = {"event": "media", "media": {"payload": ulaw_b64}}
        if stream_sid: payload["streamSid"] = stream_sid
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception as e:
            print(f"⚠️ Twilio send fail: {e}")

    async def twilio_clear_buffer():
        # Twilio clear: limpia el buffer de reproducción (barge-in real)  :contentReference[oaicite:4]{index=4}
        try:
            await websocket.send_text(json.dumps({"event": "clear"}))
        except Exception:
            pass

    async def drain_queue():
        try:
            while True:
                q_out.get_nowait(); q_out.task_done()
        except asyncio.QueueEmpty:
            pass

    async def paced_sender():
        SIL20 = ulaw_silence_b64(20)
        while websocket.application_state == WebSocketState.CONNECTED:
            try:
                if user_speaking:
                    await twilio_send(SIL20); await asyncio.sleep(0.020); continue
                chunk = await asyncio.wait_for(q_out.get(), timeout=0.060)
                await twilio_send(chunk); q_out.task_done()
                await asyncio.sleep(0.020)
            except asyncio.TimeoutError:
                await twilio_send(SIL20)

    try:
        async with websockets.connect(
            url, extra_headers=headers, subprotocols=["realtime"],
            ping_interval=20, ping_timeout=20, close_timeout=5, max_size=10_000_000
        ) as aiws:
            print("🔗 OpenAI Realtime CONNECTED")

            # 1) session.update — aplica VAD, formatos y voz DESDE JSON
            sess = {
                "type": "session.update",
                "session": {
                    "turn_detection": turn_det,
                    "input_audio_format": in_fmt,
                    "output_audio_format": out_fmt,
                    "voice": voice,
                    "modalities": ["audio", "text"],
                    "instructions": system_prompt,
                    "temperature": temperature
                }
            }
            await aiws.send(json.dumps(sess))
            print("➡️ session.update enviado")

            # 2) saludo inicial SOLO si JSON lo trae
            if first_message:
                await aiws.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "modalities": ["audio", "text"],
                        "voice": voice,
                        "output_audio_format": out_fmt,
                        "instructions": first_message
                    }
                }))

            sender_task = asyncio.create_task(paced_sender())

            # --- Twilio → OpenAI (μ-law base64 tal cual) ---
            async def t2o():
                nonlocal stream_sid
                try:
                    while True:
                        txt = await websocket.receive_text()
                        data = json.loads(txt)
                        ev = data.get("event")

                        if ev == "start":
                            stream_sid = data.get("start", {}).get("streamSid")
                            print(f"🎧 Twilio START sid={stream_sid}")

                        elif ev == "media":
                            b64 = data["media"]["payload"]
                            await aiws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))

                        elif ev == "stop":
                            print("🛑 Twilio STOP"); 
                            try: await aiws.close()
                            except Exception: pass
                            break
                except WebSocketDisconnect:
                    print("🔴 Twilio WS disconnect")
                    try: await aiws.close()
                    except Exception: pass
                except Exception as e:
                    print(f"⚠️ Twilio→OpenAI error: {e}")
                    try: await aiws.close()
                    except Exception: pass

            # --- OpenAI → Twilio ---
            async def o2t():
                nonlocal user_speaking
                try:
                    async for raw in aiws:
                        try:
                            evt = json.loads(raw)
                        except Exception:
                            continue
                        t = evt.get("type")

                        # Logs útiles (omitimos audio delta para no inundar)
                        if t and t not in ("response.audio.delta","response.output_audio.delta",
                                           "input_audio_buffer.speech_started","input_audio_buffer.speech_stopped"):
                            print("ℹ️", t, "::", evt)

                        if t == "error":
                            print("❌ OpenAI ERROR:", evt)

                        if t == "input_audio_buffer.speech_started":
                            user_speaking = True
                            # cortar TTS y limpiar buffers
                            await aiws.send(json.dumps({"type": "response.cancel"}))
                            await drain_queue()
                            await twilio_clear_buffer()

                        if t == "input_audio_buffer.speech_stopped":
                            # NO commit: server_vad + create_response maneja el turno  :contentReference[oaicite:5]{index=5}
                            user_speaking = False

                        if t in ("response.audio.delta","response.output_audio.delta"):
                            if user_speaking: 
                                continue
                            audio_b64 = evt.get("delta") or evt.get("audio")
                            if audio_b64:
                                await q_out.put(audio_b64)
                except Exception as e:
                    print("⚠️ OpenAI→Twilio error:", e)

            await asyncio.gather(t2o(), o2t())

            if not sender_task.done():
                sender_task.cancel()
                try: await sender_task
                except asyncio.CancelledError: pass

    except Exception as e:
        import traceback
        print("❌ FALLO conectar a Realtime:", e)
        traceback.print_exc()
    finally:
        print("🔴 Twilio WS CLOSED")

@app.get("/whoami")
async def whoami(request: Request):
    slug = await _resolve_slug(request); return {"bot": slug}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
