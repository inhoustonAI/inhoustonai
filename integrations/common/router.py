# -*- coding: utf-8 -*-
"""
Router común:
- GET  /bots         -> lista los bots cargados desde ./bots/*.json
- POST /bots/reload  -> recarga los JSON sin reiniciar el servicio
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from .registry import registry

router = APIRouter()

@router.get("/bots")
def bots_list():
    return JSONResponse({"count": len(registry.all()), "bots": registry.all()})

@router.post("/bots/reload")
def bots_reload():
    registry.reload()
    return JSONResponse({"ok": True, "reloaded": True, "count": len(registry.all())})
