# app/config.py
import os

class Config:
    # Entorno
    ENV = os.getenv("FLASK_ENV", "production")
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me")

    # URL pública de Render
    RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://inhouston-ai-api.onrender.com")

    # WebSocket público (mismo dominio que Render, protocolo WSS)
    PUBLIC_WS_URL = RENDER_EXTERNAL_URL.replace("http://", "wss://").replace("https://", "wss://")

    # OpenAI Realtime (sin voz global; la voz será por bot/JSON)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-realtime-preview-2024-12-17")

