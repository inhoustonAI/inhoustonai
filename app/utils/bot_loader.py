# app/utils/bot_loader.py
import os
import json

def _bots_dir():
    """
    Devuelve la carpeta 'bots' del proyecto (hermana de 'app').
    /Users/.../In Houston AI/
      ├── app/
      └── bots/
    """
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../In Houston AI/app
    project_root = os.path.dirname(app_dir)                                # .../In Houston AI
    bots = os.path.join(project_root, "bots")
    return bots

def load_bot(bot_id: str = "carlos") -> dict:
    """
    Carga el JSON del bot por id desde la carpeta 'bots'.
    Ej: bots/carlos.json
    """
    bots_dir = _bots_dir()
    path = os.path.join(bots_dir, f"{bot_id}.json")

    if not os.path.exists(path):
        raise FileNotFoundError(f"No existe el archivo del bot: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Validaciones mínimas opcionales
    if "openai" not in data:
        data["openai"] = {}
    if "model" not in data["openai"]:
        data["openai"]["model"] = "gpt-4o-realtime-preview-2024-12-17"

    return data
