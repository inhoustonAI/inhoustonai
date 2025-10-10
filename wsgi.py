# wsgi.py — (no usado en Render con startCommand=uvicorn ...)
# Este archivo existe sólo para evitar errores si alguien intenta importarlo.
# Render inicia con: uvicorn main_fastapi:app --host 0.0.0.0 --port $PORT
asgi_app = None
print("ℹ️ wsgi.py: no se usa; la app corre con Uvicorn (main_fastapi:app).")
