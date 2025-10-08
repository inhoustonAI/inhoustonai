# app/routes/__init__.py
from app.voice_bridge import bp as voice_bridge_bp

def register_routes(app):
    app.register_blueprint(voice_bridge_bp)
