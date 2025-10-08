# wsgi.py — punto de entrada principal para Render (modo ASGI)
# Soporta Flask (WSGI) + FastAPI (ASGI) + WebSocket (Twilio Realtime)

import logging
from app import create_app

# Crear la app combinada (Flask + FastAPI)
asgi_app = create_app()

# Configuración de logs detallada
logging.basicConfig(level=logging.INFO)
logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("websockets").setLevel(logging.INFO)
logging.getLogger("gunicorn").setLevel(logging.INFO)

print("✅ FastAPI y Flask integrados con compatibilidad ASGI.")
print("🟢 ASGI inicializado con soporte FastAPI + Flask")
print("👉 Esperando conexión Twilio / multibot en /ws ...")

# Render ejecuta automáticamente:
# gunicorn -k uvicorn.workers.UvicornWorker wsgi:asgi_app
