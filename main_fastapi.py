# -*- coding: utf-8 -*-
"""
App raíz (FastAPI) — Integraciones Level Up (ElevenLabs) por módulos/carpetas.
- Lee bots/*.json para email/followups por bot.
- Monta el router de 'llamadas_elevenlab' (webhooks post-call y eventos).
"""

import os
import pathlib
from fastapi import FastAPI
from dotenv import load_dotenv

# Carga .env
ROOT_DIR = pathlib.Path(__file__).resolve().parent
load_dotenv(dotenv_path=ROOT_DIR / ".env")

# App
app = FastAPI(title="INH Integrations (FastAPI)")

# Routers
from integrations.llamadas_elevenlab.router import router as llamadas_router
app.include_router(llamadas_router, prefix="")

@app.get("/health")
def health():
    from integrations.common.registry import registry
    return {
        "status": "ok",
        "env": os.getenv("FLASK_ENV", "development"),
        "bots_count": len(registry.all()),
        "modules": ["llamadas_elevenlab"]
    }

if __name__ == "__main__":
    import uvicorn
    PORT = int(os.getenv("PORT", "5000"))
    uvicorn.run("main_fastapi:app", host="0.0.0.0", port=PORT, reload=True)
