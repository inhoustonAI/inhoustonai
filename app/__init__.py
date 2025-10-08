# app/__init__.py
import asyncio, base64, json, audioop, os, time
import websockets
from flask import Flask
from fastapi import FastAPI, WebSocket
from asgiref.wsgi import WsgiToAsgi
from .config import Config
from .extensions import init_extensions

def create_app():
    # -------- Flask (HTTP) --------
    app = Flask(__name__)
    app.config.from_object(Config)
    init_extensions(app)

    from .routes import register_routes
    register_routes(app)  # /flask/twilio/voice (TwiML con <Stream>)

    # -------- FastAPI (WS) --------
    fastapi_app = FastAPI(title="In Houston AI — Realtime WS")

    @fastapi_app.websocket("/twilio/bridge")
    async def twilio_bridge(ws: WebSocket):
        await ws.accept()
        print("🟢 Nueva conexión Twilio ↔ OpenAI")

        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
        if not OPENAI_API_KEY:
            print("❌ Falta OPENAI_API_KEY")
            await ws.close(code=4000)
            return

        uri = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"}

        # parámetros VAD (ajustables)
        SILENCE_RMS = 220        # umbral de silencio sobre PCM16
        FRAME_MS    = 20         # Twilio envía ~20ms por frame
        COMMIT_MS   = 800        # silencio para cortar turno

        async def keepalive():
            try:
                while True:
                    await ws.send_text(json.dumps({"event": "keepalive"}))
                    await asyncio.sleep(3)
            except Exception:
                return

        try:
            async with websockets.connect(uri, extra_headers=headers) as openai_ws:
                print("🔗 Conectado con OpenAI Realtime")

                # Ajuste de sesión para conversación natural
                await openai_ws.send(json.dumps({
                    "type": "session.update",
                    "session": {
                        "voice": "verse",
                        "conversation": "continue",
                        "instructions": (
                            "Te llamas Asistente de In Houston Texas. "
                            "Habla en español con calidez, de forma breve y conversacional. "
                            "Haz preguntas de seguimiento cuando tenga sentido."
                        ),
                    },
                }))
                print("🧠 Configuración de sesión enviada a OpenAI.")

                # Estado VAD
                silent_ms = 0
                had_voice_since_last_commit = False

                async def twilio_to_openai():
                    nonlocal silent_ms, had_voice_since_last_commit
                    while True:
                        msg = await ws.receive_text()
                        data = json.loads(msg)

                        if data.get("event") == "media":
                            payload_b64 = data["media"]["payload"]
                            if not payload_b64:
                                continue

                            # μ-law(8k) -> PCM16(8k) -> PCM16(16k)
                            ulaw = base64.b64decode(payload_b64)
                            pcm8k = audioop.ulaw2lin(ulaw, 2)
                            pcm16k, _ = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)

                            # VAD: medir RMS en 16k
                            rms = audioop.rms(pcm16k, 2)
                            if rms < SILENCE_RMS:
                                silent_ms += FRAME_MS
                            else:
                                silent_ms = 0
                                had_voice_since_last_commit = True

                            # enviar audio a OpenAI
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(pcm16k).decode("utf-8"),
                            }))

                            # si hubo voz y ahora silencio prolongado ⇒ cerrar turno
                            if had_voice_since_last_commit and silent_ms >= COMMIT_MS:
                                print("🗣️ Fin de turno por silencio ⇒ commit + response.create")
                                had_voice_since_last_commit = False
                                silent_ms = 0
                                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                await openai_ws.send(json.dumps({"type": "response.create"}))

                        # Si Twilio cortara el stream
                        elif data.get("event") == "stop":
                            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await openai_ws.send(json.dumps({"type": "response.create"}))
                            print("⛔ stop recibido; commit final")

                async def openai_to_twilio():
                    AUDIO_TYPES = {"response.audio.delta", "output_audio.delta"}
                    try:
                        async for raw in openai_ws:
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                continue

                            if msg.get("type") in AUDIO_TYPES and "audio" in msg:
                                # PCM16(16k) -> PCM16(8k) -> μ-law(8k)
                                pcm16 = base64.b64decode(msg["audio"])
                                pcm8k, _ = audioop.ratecv(pcm16, 2, 1, 16000, 8000, None)
                                ulaw = audioop.lin2ulaw(pcm8k, 2)
                                await ws.send_text(json.dumps({
                                    "event": "media",
                                    "media": {"payload": base64.b64encode(ulaw).decode("utf-8")}
                                }))
                    finally:
                        await asyncio.sleep(0.2)  # drenar buffers

                await asyncio.gather(twilio_to_openai(), openai_to_twilio(), keepalive())

        except websockets.exceptions.ConnectionClosed:
            print("🟡 Conexión con OpenAI cerrada.")
        except Exception as e:
            print(f"❌ Error global en el WebSocket: {e}")
        finally:
            try:
                await ws.close()
            except Exception:
                pass
            print("🔴 Conexión WS cerrada correctamente.")

    # Montar Flask (WSGI) dentro de FastAPI (ASGI)
    asgi_flask = WsgiToAsgi(app)
    fastapi_app.mount("/flask", asgi_flask)

    print("✅ FastAPI y Flask integrados con compatibilidad ASGI.")
    return fastapi_app
