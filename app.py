# app.py
# FastAPI + Twilio Media Streams + OpenAI Realtime (conversación natural)
import os, json, base64, asyncio, audioop, logging
import websockets
from fastapi import FastAPI, WebSocket
from fastapi.responses import Response

app = FastAPI(title="In Houston AI — Realtime Voice")

# --------- Webhook de voz (TwiML) ---------
@app.post("/twilio/voice")
async def twilio_voice(bot: str = "carlos"):
    """
    Twilio 'A call comes in' -> Webhook (HTTP POST)
    Devuelve TwiML que saluda y abre el Media Stream WebSocket.
    """
    # Saludo inicial natural
    greeting_tts = "Hola, soy tu asistente de In Houston, Texas. ¿En qué puedo ayudarte?"

    # IMPORTANTE: WSS de este mismo servicio
    stream_url = f"wss://{os.getenv('PUBLIC_HOST', 'inhouston-ai-api.onrender.com')}/twilio/bridge?bot={bot}"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Lucia-Neural" language="es-MX">{greeting_tts}</Say>
  <Connect>
    <Stream url="{stream_url}"/>
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# --------- Puente WS Twilio <-> OpenAI ---------
@app.websocket("/twilio/bridge")
async def twilio_bridge(ws: WebSocket):
    """
    Twilio abrirá este WebSocket y enviará audio µ-law 8k en frames 'media'.
    Respondemos con audio µ-law 8k en 'media.payload' (base64).
    """
    await ws.accept()
    print("🟢 Twilio conectado (WS).")

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    if not OPENAI_API_KEY:
        await ws.close(code=4000)
        print("❌ Falta OPENAI_API_KEY")
        return

    # Conexión a OpenAI Realtime
    rt_uri = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    rt_headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    try:
        async with websockets.connect(
            rt_uri,
            extra_headers=rt_headers,
            ping_interval=10,   # evita cierres 1005
            ping_timeout=20,
            max_size=None,
        ) as oai:
            print("🔗 Conectado con OpenAI Realtime")

            # Configuración de sesión: voz natural + VAD en servidor
            session_update = {
                "type": "session.update",
                "session": {
                    "instructions": (
                        "Eres un asistente cálido de Houston, Texas. "
                        "Mantén una conversación fluida, breve y natural en español."
                    ),
                    "voice": "verse",
                    # Vamos a ENVIAR PCM16/16k a OpenAI (convertimos desde µlaw 8k):
                    "input_audio_format": {
                        "type": "wav",  # PCM lineal (equiv a pcm_s16le); 'wav' funciona bien para append
                        "sample_rate_hz": 16000,
                        "channels": 1
                    },
                    # Pedimos audio de salida como PCM16/16k (lo re-muestreamos a µlaw 8k para Twilio)
                    "output_audio_format": {
                        "type": "wav",
                        "sample_rate_hz": 16000,
                        "channels": 1
                    },
                    # Turn-taking automático
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "silence_duration_ms": 600
                    },
                    "modalities": ["text", "audio"]
                },
            }
            await oai.send(json.dumps(session_update))
            print("🧠 Sesión configurada (VAD + voz).")

            # ---- Corutina: Twilio -> OpenAI
            async def twilio_to_openai():
                while True:
                    msg = await ws.receive_text()
                    data = json.loads(msg)

                    # Twilio 'media' con audio µlaw 8k base64
                    if data.get("event") == "media":
                        ulaw_b64 = data["media"]["payload"]
                        if not ulaw_b64:
                            continue

                        # µlaw(8k) -> PCM16 (8k) -> resample a 16k
                        ulaw = base64.b64decode(ulaw_b64)
                        pcm8k = audioop.ulaw2lin(ulaw, 2)  # 16-bit little endian
                        pcm16k, _ = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)

                        await oai.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm16k).decode("utf-8")
                        }))

                    # Nota: con server_vad NO necesitamos commit/manual response.
                    # Si quisieras forzarlo: manda 'input_audio_buffer.commit' y luego 'response.create'.

            # ---- Corutina: OpenAI -> Twilio
            async def openai_to_twilio():
                async for raw in oai:
                    evt = json.loads(raw)

                    # Audio incremental de la respuesta
                    if evt.get("type") == "response.audio.delta":
                        pcm16_b64 = evt["audio"]          # PCM16/16k
                        pcm16 = base64.b64decode(pcm16_b64)

                        # Resample a 8k y codifica µlaw para Twilio
                        pcm8k, _ = audioop.ratecv(pcm16, 2, 1, 16000, 8000, None)
                        ulaw = audioop.lin2ulaw(pcm8k, 2)
                        await ws.send_text(json.dumps({
                            "event": "media",
                            "media": {"payload": base64.b64encode(ulaw).decode("utf-8")}
                        }))

                    # (Opcional) logs del transcript parcial
                    if evt.get("type") == "response.audio_transcript.delta":
                        pass

            # ---- Keepalive Twilio (evita timeout por inactividad)
            async def keepalive():
                try:
                    while True:
                        await ws.send_text(json.dumps({"event": "keepalive"}))
                        await asyncio.sleep(3)
                except Exception:
                    pass

            await asyncio.gather(twilio_to_openai(), openai_to_twilio(), keepalive())

    except websockets.exceptions.ConnectionClosedOK:
        print("🟡 OpenAI cerró la conexión limpiamente.")
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"🔴 OpenAI cerró abruptamente: {e}")
    except Exception as e:
        logging.exception(f"❌ Error global WS: {e}")
    finally:
        print("🔴 Twilio WS cerrado.")
