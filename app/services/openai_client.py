# app/services/openai_client.py
import os, json, base64, threading, time, audioop
from websocket import create_connection, WebSocketConnectionClosedException

__all__ = ["OpenAIRealtimeBridge"]

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

class OpenAIRealtimeBridge:
    """Puente WS full-duplex con OpenAI Realtime."""

    def __init__(self, model: str, voice: str, locale: str, system_prompt: str, temperature: float = 0.4):
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY no está seteada en el entorno")

        self.model = model
        self.voice = voice
        self.locale = locale
        self.system_prompt = system_prompt
        self.temperature = float(temperature)

        url = f"wss://api.openai.com/v1/realtime?model={self.model}"
        headers = [
            f"Authorization: Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta: realtime=v1"
        ]
        self.ws = create_connection(url, header=headers)

        self._lock = threading.Lock()
        self._closed = False
        self._outgoing_audio = []
        self._reader = threading.Thread(target=self._recv_loop, daemon=True)
        self._reader.start()

        self._send_session()

    def _send(self, obj: dict):
        with self._lock:
            if not self._closed:
                try:
                    self.ws.send(json.dumps(obj))
                except WebSocketConnectionClosedException:
                    self._closed = True

    def _recv_loop(self):
        """Escucha eventos del modelo en tiempo real."""
        try:
            while not self._closed:
                raw = self.ws.recv()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                t = msg.get("type", "")

                if t in [
                    "response.output_audio.delta",
                    "response.output_audio_chunk",
                    "output_audio.delta"
                ]:
                    b64_audio = msg.get("audio") or msg.get("data")
                    if b64_audio:
                        b = base64.b64decode(b64_audio)
                        with self._lock:
                            self._outgoing_audio.append(b)
                        print(f"🔈 [OpenAIRealtimeBridge] Audio recibido ({len(b)} bytes)")

                elif t in ["response.completed", "response.stop"]:
                    print("✅ [OpenAIRealtimeBridge] Respuesta completada")

        except Exception as e:
            print(f"⚠️ [OpenAIRealtimeBridge] Error de conexión: {e}")
        finally:
            self.close()

    def _send_session(self):
        """Configura la sesión del modelo Realtime."""
        session_data = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": self.voice,
                "language": self.locale,
                "temperature": self.temperature,
                "instructions": self.system_prompt,
                "input_audio_format": {"type": "pcm16", "sample_rate_hz": 16000},
                "output_audio_format": {"type": "pcm16", "sample_rate_hz": 16000}
            }
        }
        self._send(session_data)
        print(f"🟢 [OpenAIRealtimeBridge] Sesión configurada con voz={self.voice}, idioma={self.locale}")

    def send_twilio_ulaw8k(self, ulaw_bytes: bytes):
        """Convierte μ-law 8kHz → PCM16 16kHz y lo manda al modelo."""
        if not ulaw_bytes:
            return
        try:
            pcm8k = audioop.ulaw2lin(ulaw_bytes, 2)
            pcm16k, _ = audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)
            b64 = base64.b64encode(pcm16k).decode("utf-8")
            self._send({"type": "input_audio_buffer.append", "audio": b64})
        except Exception as e:
            print(f"⚠️ [OpenAIRealtimeBridge] Error procesando audio: {e}")

    def commit_input(self):
        """Cierra el turno de entrada y pide respuesta."""
        self._send({"type": "input_audio_buffer.commit"})
        self._send({
            "type": "response.create",
            "response": {"modalities": ["text", "audio"]}
        })
        print("🗣️ [OpenAIRealtimeBridge] Turno cerrado, esperando respuesta...")

    def get_audio_for_twilio_ulaw8k(self) -> bytes:
        """Convierte PCM16 16k → μ-law 8k para Twilio."""
        with self._lock:
            if not self._outgoing_audio:
                return b""
            pcm16_joined = b"".join(self._outgoing_audio)
            self._outgoing_audio.clear()

        try:
            pcm8k, _ = audioop.ratecv(pcm16_joined, 2, 1, 16000, 8000, None)
            ulaw8k = audioop.lin2ulaw(pcm8k, 2)
            return ulaw8k
        except Exception as e:
            print(f"⚠️ [OpenAIRealtimeBridge] Error al convertir audio: {e}")
            return b""

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self.ws.close()
                    print("🔴 [OpenAIRealtimeBridge] Conexión cerrada correctamente")
                except Exception:
                    pass
