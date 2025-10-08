# app/services/openai_realtime.py
import os
import json
import base64
import threading
import time
import audioop  # stdlib para mu-law/PCM y resampling
from typing import Callable, Optional

import websocket  # websocket-client

OPENAI_WS_URL = "wss://api.openai.com/v1/realtime"

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")

def _b64d(data_b64: str) -> bytes:
    return base64.b64decode(data_b64)

def mulaw8k_b64_to_pcm16_16k(audio_b64: str) -> bytes:
    """Twilio -> OpenAI: μ-law 8kHz (b64) -> PCM16 16kHz (bytes)."""
    ulaw_8k = _b64d(audio_b64)
    pcm16_8k = audioop.ulaw2lin(ulaw_8k, 2)           # μlaw -> PCM16 8kHz
    pcm16_16k, _ = audioop.ratecv(pcm16_8k, 2, 1, 8000, 16000, None)  # -> 16k
    return pcm16_16k

def pcm16_16k_to_mulaw8k_b64(pcm16_16k: bytes) -> str:
    """OpenAI -> Twilio: PCM16 16kHz -> μ-law 8kHz (b64)."""
    pcm16_8k, _ = audioop.ratecv(pcm16_16k, 2, 1, 16000, 8000, None)
    ulaw_8k = audioop.lin2ulaw(pcm16_8k, 2)
    return _b64(ulaw_8k)

class OpenAIRealtimeBridge:
    """
    WS con OpenAI Realtime. Envía/recibe audio.
    send_to_twilio(payload_b64) se inyecta desde el WS de Twilio.
    """
    def __init__(self, *, api_key: str, model: str, voice: str, send_to_twilio: Callable[[str], None]):
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.send_to_twilio = send_to_twilio

        self.ws: Optional[websocket.WebSocketApp] = None
        self.thread: Optional[threading.Thread] = None
        self._openai_connected = threading.Event()
        self._stop = threading.Event()

    def start(self):
        params = f"?model={self.model}"
        headers = [
            f"Authorization: Bearer {self.api_key}",
            "OpenAI-Beta: realtime=v1",
        ]

        def _on_open(ws):
            session = {
                "type": "session.update",
                "session": {
                    "voice": self.voice,
                }
            }
            ws.send(json.dumps(session))
            self._openai_connected.set()

        def _on_message(ws, message):
            # Procesa eventos de salida (audio)
            try:
                data = json.loads(message)
            except Exception:
                return

            typ = data.get("type", "")
            if "audio" in typ and ("delta" in typ or "chunk" in typ or "frame" in typ):
                b64_audio = data.get("audio") or data.get("data")
                if isinstance(b64_audio, str):
                    try:
                        pcm16_16k = base64.b64decode(b64_audio)
                        ulaw_b64 = pcm16_16k_to_mulaw8k_b64(pcm16_16k)
                        self.send_to_twilio(ulaw_b64)
                    except Exception:
                        pass

        def _on_error(ws, err):
            print("[OpenAI WS ERROR]", err)

        def _on_close(ws, *args):
            print("[OpenAI WS] closed")

        self.ws = websocket.WebSocketApp(
            OPENAI_WS_URL + params,
            header=headers,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )

        def run():
            while not self._stop.is_set():
                try:
                    self.ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as e:
                    print("[OpenAI WS] exception, retrying:", e)
                if not self._stop.is_set():
                    time.sleep(1)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self._openai_connected.wait(timeout=10)

    def stop(self):
        self._stop.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)

    def send_twilio_audio_b64(self, ulaw_b64: str):
        """μ-law 8k -> PCM16 16k y append a OpenAI."""
        if not self._openai_connected.is_set() or not self.ws:
            return
        try:
            pcm16_16k = mulaw8k_b64_to_pcm16_16k(ulaw_b64)
            msg = {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm16_16k).decode("ascii")}
            self.ws.send(json.dumps(msg))
        except Exception:
            pass

    def request_response(self, instructions: Optional[str] = None):
        if not self._openai_connected.is_set() or not self.ws:
            return
        try:
            self.ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            req = {"type": "response.create", "response": {}}
            if instructions:
                req["response"]["instructions"] = instructions
            self.ws.send(json.dumps(req))
        except Exception:
            pass
