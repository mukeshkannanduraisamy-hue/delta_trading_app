import json
import os
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
        self._settings = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> None:
        if SETTINGS_PATH.exists():
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        if k in self._settings:
                            self._settings[k] = v
            except Exception as e:
                print(f"[settings] failed to load settings.json: {e}")
        else:
            self.save()

    def save(self) -> None:
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=4)
        except Exception as e:
            print(f"[settings] failed to save settings.json: {e}")

    def get(self, key: str) -> Any:
        return self._settings.get(key, DEFAULT_SETTINGS.get(key))

    def update(self, new_settings: Dict[str, Any]) -> None:
        changed = False
        for k, v in new_settings.items():
            if k in self._settings:
                # Type cast based on default
                if isinstance(DEFAULT_SETTINGS[k], int):
                    v = int(v)
                elif isinstance(DEFAULT_SETTINGS[k], float):
                    v = float(v)
                self._settings[k] = v
                changed = True
        if changed:
            self.save()

    def all(self) -> Dict[str, Any]:
        return dict(self._settings)

manager = SettingsManager()
