# -*- coding: utf-8 -*-
"""
App raíz (FastAPI) — Integraciones con ElevenLabs por módulos/carpetas.
- Lee bots/*.json para email/followups por bot.
- Monta:
    * /elevenlabs/*  (webhooks post-call y events de ElevenLabs)
    * /bots, /bots/reload  (registry de bots)
    * /twilio/voice/status y /twilio/messaging/status  (callbacks de estado Twilio)
"""

import os
import sys
import pathlib
from fastapi import FastAPI
from dotenv import load_dotenv

APP_VERSION = "1.0.1"

# -------- Carga .env --------
ROOT_DIR = pathlib.Path(__file__).resolve().parent
load_dotenv(dotenv_path=ROOT_DIR / ".env")

# -------- Fallback de import (por si el working dir cambia en producción) --------
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

# -------- App --------
app = FastAPI(title="INH Integrations (FastAPI) — ElevenLabs only", version=APP_VERSION)

# -------- Routers --------
from integrations.llamadas_elevenlab.router import router as llamadas_router
from integrations.common.router import router as common_router
from integrations.twilio_status.router import router as twilio_router  # ← agregado

app.include_router(llamadas_router, prefix="")  # /elevenlabs/*
app.include_router(common_router, prefix="")    # /bots, /bots/reload
app.include_router(twilio_router, prefix="")    # /twilio/* status  ← agregado

# -------- Rutas básicas --------
@app.get("/")
def root():
    return {
        "ok": True,
        "service": "INH Integrations (FastAPI)",
        "version": APP_VERSION
    }

@app.get("/health")
def health():
    # import local para evitar ciclos al arrancar
    from integrations.common.registry import registry
    return {
        "status": "ok",
        "env": os.getenv("FLASK_ENV", "development"),
        "bots_count": len(registry.all()),
        "modules": ["llamadas_elevenlab", "common", "twilio_status"],  # ← agregado
        "version": APP_VERSION
    }

# -------- Dev local --------
if __name__ == "__main__":
    import uvicorn
    PORT = int(os.getenv("PORT", "5000"))
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
