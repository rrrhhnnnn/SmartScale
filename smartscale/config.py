"""Tunable settings, persisted to config.json next to the project root."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass

if getattr(sys, "frozen", False):
    # Packaged as a PyInstaller .exe: store the database and settings next to
    # the executable so they persist (the bundle's own temp dir is wiped on
    # exit). ROOT is the folder that contains SmartScale.exe.
    ROOT = os.path.dirname(sys.executable)
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Tests point these at temp files via environment variables so they can
# never touch the operator's real item library or settings.
CONFIG_PATH = os.environ.get("SMARTSCALE_CONFIG", os.path.join(ROOT, "config.json"))
DB_PATH = os.environ.get("SMARTSCALE_DB", os.path.join(ROOT, "smartscale.db"))


@dataclass
class Config:
    # --- serial ---
    port: str = ""
    baud: int = 9600

    # --- layer 1: stability gate ---
    # A reading only counts as an event once `stability_window` consecutive
    # samples all sit inside a `stability_band_g` spread. This is what stops
    # the display flickering between items while a hand is on the platform.
    stability_window: int = 6
    stability_band_g: float = 3.0

    # --- layer 2: dead-band ---
    # Changes smaller than this are treated as thermal drift, never as items.
    min_event_g: float = 3.0
    zero_track_g: float = 2.0      # auto re-zero band when the basket is empty

    # --- zero handling ---
    # allow_negative=False clamps every reading at 0 g: the scale can never
    # show or act on a value below zero (matches the Arduino sketch, which
    # does `if (kg < 0) kg = 0`). Readings below zero_display_g are SHOWN as
    # 0.000 (display only).
    allow_negative: bool = False
    zero_display_g: float = 1.0
    # When Quantity and Items are both 0, any residual up to this many grams
    # is silently re-zeroed so the display reads 0.000 kg. Larger residuals
    # are left visible - something unrecognised is still on the platform.
    empty_zero_g: float = 10.0

    # --- layer 4: tolerance matching ---
    default_tolerance_g: float = 2.0
    tolerance_pct: float = 2.0     # effective tol = max(default, weight * pct%)
    max_multiple: int = 3          # detect up to N identical items placed at once

    # --- teaching ---
    teach_samples: int = 5

    # --- cloud (phase 2) ---
    device_id: str = "scale-01"
    cloud_enabled: bool = False
    cloud_url: str = ""

    def tolerance_for(self, weight_g: float) -> float:
        """Default tolerance window for an item of this weight."""
        return round(max(self.default_tolerance_g,
                         abs(weight_g) * self.tolerance_pct / 100.0), 2)

    # --- persistence ---
    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                for key, value in data.items():
                    if hasattr(cfg, key):
                        setattr(cfg, key, value)
            except (OSError, ValueError):
                pass          # corrupt config -> fall back to defaults
        return cfg

    def save(self) -> None:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
