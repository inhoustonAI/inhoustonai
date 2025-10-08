# app/voice_bridge.py
from flask import Blueprint, request, Response

bp = Blueprint("voice_bridge", __name__, url_prefix="/twilio")

@bp.post("/voice")
def voice():
    bot = request.args.get("bot", "carlos")
    ws_url = f"wss://inhouston-ai-api.onrender.com/twilio/bridge?bot={bot}"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Lucia-Neural" language="es-MX">
    Hola, soy tu asistente de In Houston Texas. ¿En qué puedo ayudarte?
  </Say>
  <Connect>
    <Stream url="{ws_url}"/>
  </Connect>
</Response>"""
    return Response(twiml, content_type="application/xml")
