# -*- coding: utf-8 -*-
"""
App raíz (FastAPI) — Integraciones con ElevenLabs por módulos/carpetas.
- Lee bots/*.json para email/followups por bot.
- Monta el router de 'llamadas_elevenlab' (webhooks post-call y eventos).

Inbound de Twilio (A call/message comes in): -> URL de ElevenLabs.
Este servicio maneja SOLO integraciones (emails, follow-ups, logging).
"""

import os
import pathlib
from fastapi import FastAPI
from dotenv import load_dotenv

# Carga .env
ROOT_DIR = pathlib.Path(__file__).resolve().parent
load_dotenv(dotenv_path=ROOT_DIR / ".env")

# App
app = FastAPI(title="INH Integrations (FastAPI) — ElevenLabs only")

# Routers
from integrations.llamadas_elevenlab.router import router as llamadas_router
from integrations.common.router import router as common_router

app.include_router(llamadas_router, prefix="")   # /elevenlabs/*
app.include_router(common_router, prefix="")     # /bots, /bots/reload

@app.get("/health")
def health():
    from integrations.common.registry import registry
    return {
        "status": "ok",
        "env": os.getenv("FLASK_ENV", "development"),
        "bots_count": len(registry.all()),
        "modules": ["llamadas_elevenlab", "common"]
    }

if __name__ == "__main__":
    import uvicorn
    PORT = int(os.getenv("PORT", "5000"))
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
