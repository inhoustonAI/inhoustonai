# app/routes/twilio_stream.py
# Endpoint informativo (Twilio WS real ahora está en /asgi/ws)

from flask import Blueprint, Response

bp = Blueprint("twilio_stream", __name__, url_prefix="/twilio")

@bp.route("/stream", methods=["GET"])
def stream_info():
    return Response("✅ Usa wss://inhouston-ai-api.onrender.com/asgi/ws para el WebSocket de Twilio.", mimetype="text/plain")
