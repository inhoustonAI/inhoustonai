# main_fastapi.py
# FastAPI: Twilio <Stream> (G.711 μ-law 8 kHz, 20 ms) ⇄ OpenAI Realtime
# - Evita commits vacíos (>=100 ms por commit)
# - Barge-in: si el usuario habla mientras el bot habla → cancelamos la locución
# Requisitos: fastapi, uvicorn, websockets, python-dotenv (opcional)

import os, json, base64, asyncio
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ────────── Config básica ──────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_REALTIME_URL = os.getenv(
    "OPENAI_REALTIME_URL",
    "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
)
SELF_BASE_WSS = os.getenv("SELF_BASE_WSS", "wss://tu-dominio-publico")  # ej: wss://inhouston-ai-api.onrender.com

# Audio Twilio
PACKET_MS = 20                # Twilio envía 20 ms por paquete
COMMIT_EVERY_MS = 120         # commit cada ~6 paquetes (>100 ms)
INPUT_FORMAT = "g711_ulaw"    # μ-law 8 kHz
OUTPUT_FORMAT = "g711_ulaw"

# VAD server-side
SILENCE_MS = 300
MIN_SPEECH_MS = 150
VAD_THRESHOLD = 0.5

# ────────── App ──────────
app = FastAPI()

@app.get("/")
def health():
    return {"ok": True, "service": "inhouston-ai-api", "ws": "/media", "twiml": "/twiml"}

# ────────── 1) TwiML: inicia el <Stream> a /media ──────────
@app.post("/twiml", response_class=PlainTextResponse)
async def twiml(request: Request):
    q = dict(request.query_params)
    bot = q.get("bot", "sundin")
    stream_url = q.get("stream_url") or f"{SELF_BASE_WSS}/media?bot={bot}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{stream_url}" />
  </Connect>
</Response>"""

# ────────── 2) WS /media (Twilio) ⇄ WS Realtime (OpenAI) ──────────
@app.websocket("/media")
async def media(ws_twilio: WebSocket):
    # Twilio exige este subprotocolo
    await ws_twilio.accept(subprotocol="audio.twilio.com")
    params = dict(ws_twilio.query_params)
    bot_slug = params.get("bot", "sundin")

    # Estado por llamada
    bot_speaking = False
    outbound_queue: asyncio.Queue = asyncio.Queue()  # OpenAI → Twilio

    # Conexión a OpenAI Realtime
    import websockets
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    try:
        ws_openai = await websockets.connect(
            OPENAI_REALTIME_URL,
            extra_headers=headers,
            max_size=2**23
        )
    except Exception:
        await ws_twilio.close(code=1011)
        return

    # Configurar sesión: formatos + VAD + instrucciones
    session = {
        "type": "session.update",
        "session": {
            "modalities": ["audio", "text"],
            "input_audio_format": INPUT_FORMAT,
            "output_audio_format": OUTPUT_FORMAT,
            "turn_detection": {
                "type": "server_vad",
                "silence_duration_ms": SILENCE_MS,
                "min_speech_ms": MIN_SPEECH_MS,
                "vad_threshold": VAD_THRESHOLD
            },
            "input_audio_transcription": {"enabled": True},
            "instructions": (
                f"Eres el agente de {bot_slug}. Habla natural, breve y permite interrupciones. "
                f"Si el usuario interrumpe, cede el turno inmediatamente."
            ),
        }
    }
    await ws_openai.send(json.dumps(session))

    # Lee eventos desde OpenAI y reenvía audio a Twilio
    async def openai_reader():
        nonlocal bot_speaking
        try:
            async for msg in ws_openai:
                # Los eventos suelen ser JSON
                try:
                    data = json.loads(msg)
                except Exception:
                    continue

                t = data.get("type", "")

                if t in ("response.created", "response.output_item.added"):
                    bot_speaking = True

                # Extraer audio (nombres pueden variar por release)
                audio_b64 = None
                if "audio" in data:
                    audio_b64 = data.get("audio")
                elif "delta" in data and isinstance(data["delta"], dict) and "audio" in data["delta"]:
                    audio_b64 = data["delta"]["audio"]
                elif "item" in data and isinstance(data["item"], dict):
                    audio_b64 = data["item"].get("audio")

                if audio_b64:
                    await outbound_queue.put({"event": "media", "media": {"payload": audio_b64}})

                if t in ("response.completed", "response.output_audio.done", "response.canceled", "conversation.item.truncated"):
                    bot_speaking = False
        except Exception:
            pass

    # Escribe audio hacia Twilio
    async def twilio_writer():
        try:
            while True:
                evt = await outbound_queue.get()
                await ws_twilio.send_text(json.dumps(evt))
        except Exception:
            pass

    reader_task = asyncio.create_task(openai_reader())
    writer_task = asyncio.create_task(twilio_writer())

    try:
        # acumulador para commits
        ms_acc = 0

        while True:
            raw = await ws_twilio.receive_text()
            evt = json.loads(raw)
            etype = evt.get("event")

            if etype == "start":
                continue

            elif etype == "media":
                payload_b64 = evt["media"]["payload"]

                # BARGE-IN: si el bot está hablando y entra voz humana, cancelamos la locución actual
                if bot_speaking:
                    await ws_openai.send(json.dumps({"type": "response.cancel"}))
                    bot_speaking = False  # evita múltiples cancels

                # 1) Append del paquete μ-law (ya en g711_ulaw)
                await ws_openai.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": payload_b64
                }))

                # 2) Commit cada ~120 ms (>=100 ms) para evitar error de buffer vacío
                ms_acc += PACKET_MS
                if ms_acc >= COMMIT_EVERY_MS:
                    await ws_openai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    ms_acc = 0

            elif etype == "stop":
                # Flush final si queda algo sin commit
                if ms_acc > 0:
                    await ws_openai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                break

            # marks u otros → ignorar

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            await ws_openai.close()
        except Exception:
            pass
        try:
            await ws_twilio.close()
        except Exception:
            pass
        reader_task.cancel()
        writer_task.cancel()

# ────────── Ejecutar local (opcional) ──────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
