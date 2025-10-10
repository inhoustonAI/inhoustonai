import json
import os
import pathlib
from typing import Any, Dict, List, Optional

BOTS_DIR = os.getenv("BOTS_DIR", str(pathlib.Path(__file__).resolve().parents[2] / "bots"))

class BotRegistry:
    def __init__(self, bots_dir: str):
        self.bots_dir = bots_dir
        self._bots: Dict[str, Dict[str, Any]] = {}
        self.reload()

    def reload(self):
        self._bots.clear()
        p = pathlib.Path(self.bots_dir)
        if not p.is_dir():
            return
        for f in p.glob("*.json"):
            try:
                with f.open("r", encoding="utf-8") as fh:
                    obj = json.load(fh)
                bot_id = obj.get("id") or f.stem
                self._bots[bot_id] = obj
            except Exception as e:
                print(f"[WARN] No se pudo cargar {f.name}: {e}")

    def all(self) -> List[Dict[str, Any]]:
        return list(self._bots.values())

    def get(self, bot_id: str) -> Optional[Dict[str, Any]]:
        return self._bots.get(bot_id)

    def find_by_number(self, number: str) -> Optional[Dict[str, Any]]:
        if not number:
            return None
        n = str(number).strip().lower()
        for bot in self._bots.values():
            pn = bot.get("phone_numbers", {})
            voice = str(pn.get("voice", "")).lower()
            wa = str(pn.get("whatsapp", "")).lower()
            if n == voice or n == wa:
                return bot
        return None

registry = BotRegistry(BOTS_DIR)
