# app/routes/twilio_voice.py
from flask import Blueprint, request, Response
from app.utils.bot_loader import load_bot

bp = Blueprint("twilio_voice", __name__, url_prefix="/twilio")

@bp.post("/voice")
def voice():
    bot_id = request.args.get("bot", "carlos")
    bot = load_bot(bot_id)

    greeting_tts = (
        bot.get("telephony", {}).get("greeting_tts")
        or "Conectando con el asistente inteligente de In Houston Texas."
    )
    language = bot.get("language", "es-MX")

    # 🔧 Nuevo endpoint WebSocket
    ws_base = "wss://inhouston-ai-api.onrender.com/asgi"
    stream_url = f"{ws_base}/ws?bot={bot_id}"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="{language}">{greeting_tts}</Say>
  <Connect>
    <Stream url="{stream_url}"/>
  </Connect>
</Response>"""
    return Response(twiml, content_type="application/xml")
