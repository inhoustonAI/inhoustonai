# -*- coding: utf-8 -*-
import os
import pathlib
from fastapi import FastAPI
from dotenv import load_dotenv

ROOT_DIR = pathlib.Path(__file__).resolve().parent
load_dotenv(dotenv_path=ROOT_DIR / ".env")

app = FastAPI(title="INH Integrations (FastAPI)")

# Routers
from integrations.llamadas_elevenlab.router import router as llamadas_router
from integrations.common.router import router as common_router

app.include_router(llamadas_router, prefix="")
app.include_router(common_router, prefix="")

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
