# app/extensions.py
from flask_sock import Sock

sock = Sock()

def init_extensions(app):
    """Inicializa las extensiones globales"""
    sock.init_app(app)
