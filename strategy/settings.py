import json
import os
import threading
from pathlib import Path
from typing import Dict, Any

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"

DEFAULT_SETTINGS = {
    "max_lot_size": 10,
    "max_leverage_cap": 20,
    "leverage_very_low": 2,
    "leverage_low": 5,
    "leverage_medium": 10,
    "leverage_high": 15,
    "leverage_very_high": 20,
    "lot_pct_very_low": 10,
    "lot_pct_low": 25,
    "lot_pct_medium": 50,
    "lot_pct_high": 75,
    "lot_pct_very_high": 100,
    "starting_virtual_balance": 100000,
    "daily_loss_limit_pct": 20,
    "max_open_positions": 5
}

class SettingsManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._settings = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                data = json.loads(self.path.read_text() if hasattr(self, 'path') else SETTINGS_PATH.read_text())
                for k, v in data.items():
                    if k in self._settings:
                        try:
                            if isinstance(DEFAULT_SETTINGS[k], int):
                                v = int(v)
                            elif isinstance(DEFAULT_SETTINGS[k], float):
                                v = float(v)
                            self._settings[k] = v
                        except (ValueError, TypeError):
                            pass
            except (FileNotFoundError, json.JSONDecodeError):
                self.save()

    def save(self) -> None:
        try:
            path = self.path if hasattr(self, 'path') else SETTINGS_PATH
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=4)
        except Exception as e:
            print(f"[settings] failed to save settings.json: {e}")

    def get(self, key: str) -> Any:
        with self._lock:
            return self._settings.get(key, DEFAULT_SETTINGS.get(key))

    def update(self, new_settings: Dict[str, Any]) -> None:
        validated = {}
        for k, v in new_settings.items():
            if k not in self._settings:
                continue
            try:
                if isinstance(DEFAULT_SETTINGS[k], int):
                    v = int(v)
                elif isinstance(DEFAULT_SETTINGS[k], float):
                    v = float(v)
                validated[k] = v
            except (ValueError, TypeError):
                pass
        with self._lock:
            self._settings.update(validated)
            self.save()

    def all(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._settings)

manager = SettingsManager()
