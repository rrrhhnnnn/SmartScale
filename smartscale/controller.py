"""Headless application controller.

Owns the link, engine, database and cloud sync, runs the poll loop on its own
thread, and publishes changes to any number of subscribers. Both front ends
(the web dashboard and the Tkinter fallback) are thin views over this.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from .cloud import CloudSync
from .config import DB_PATH, Config
from .engine import RecognitionEngine
from .link import SerialLink, SimulatorLink, list_ports
from .models import ItemDef, ScaleEvent
from .storage import Database

SIMULATOR = "SIMULATOR"


def _item_dict(i: ItemDef) -> dict:
    return {"id": i.id, "name": i.name, "weight_g": round(i.weight_g, 1),
            "tolerance_g": round(i.tolerance_g, 1),
            "low": round(i.low, 1), "high": round(i.high, 1)}


def _event_dict(ev: ScaleEvent) -> dict:
    return {"ts": ev.ts, "clock": ev.clock, "iso": ev.iso, "kind": ev.kind,
            "item": ev.item_name, "count": ev.count,
            "delta_g": ev.delta_g, "total_g": ev.total_g,
            "quantity": ev.quantity, "distinct": ev.distinct,
            "status": ev.status, "text": ev.describe()}


class ScaleController:
    POLL_S = 0.03

    def __init__(self) -> None:
        self.cfg = Config.load()
        self.db = Database(DB_PATH)
        self.db.seed_demo_items()
        self.engine = RecognitionEngine(self.cfg, self.db.items)
        self.cloud = CloudSync(self.cfg, self.db, notify=self._notify)

        self.link = None
        self.source: str = ""
        self.samples: deque = deque(maxlen=240)     # (t, grams, stable)
        self.status_msg = "Ready. Pick a source and connect."
        self.last_error: Optional[str] = None
        self.connecting = False

        self._subs: List[queue.Queue] = []
        self._subs_lock = threading.Lock()
        self._raw_value: Optional[float] = None
        self._raw_ready = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ----------------------------------------------------------- pub / sub
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._subs_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subs_lock:
            if q in self._subs:
                self._subs.remove(q)

    def _broadcast(self, kind: str, data) -> None:
        with self._subs_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait((kind, data))
            except queue.Full:
                pass                              # slow client: drop, not block

    def _notify(self, msg: str) -> None:
        self.status_msg = msg
        self._broadcast("status", {"text": msg})

    def _push_state(self) -> None:
        self._broadcast("state", self.state())

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.disconnect()
        self.cfg.save()
        self.db.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            link = self.link
            if link is None:
                time.sleep(self.POLL_S)
                continue
            drained = 0
            while drained < 60:
                try:
                    kind, payload = link.inbox.get_nowait()
                except queue.Empty:
                    break
                drained += 1
                self._handle(kind, payload)
            time.sleep(self.POLL_S)

    def _handle(self, kind: str, payload) -> None:
        if kind == "W":
            self._on_sample(float(payload))
        elif kind == "RAW":
            self._raw_value = float(payload)
            self._raw_ready.set()
        elif kind == "ACK":
            self._notify("Device: %s" % payload)
        elif kind == "PROTOCOL":
            if payload == "legacy":
                self.engine.set_window(4)      # original sketch: ~1 reading/s
                self._notify("Original firmware detected. Tare runs in software; "
                             "live calibration needs firmware v2.")
            else:
                self.engine.apply_config()
                self._notify("Firmware v2 detected.")
            self._push_state()
        elif kind == "ERROR":
            self.last_error = str(payload)
            self.connecting = False
            self._notify(str(payload))
            self._broadcast("error", {"text": str(payload)})
            self._push_state()
        else:
            text = str(payload)
            if "READY" in text or "Connected" in text or "SIMULATOR" in text:
                self.connecting = False
                self._push_state()
            self._notify(text)

    def _on_sample(self, grams: float) -> None:
        events = self.engine.on_sample(grams)
        stable = self.engine.is_stable
        shown = self.engine.last_g                     # tare-corrected, >= 0 unless allowed
        if abs(shown) < self.cfg.zero_display_g:       # display zero band
            shown = 0.0
        self.samples.append((time.time(), round(shown, 1), stable))
        self._broadcast("sample", {"g": round(shown, 1), "stable": stable,
                                   "quantity": self.engine.quantity,
                                   "distinct": self.engine.distinct,
                                   "expected_g": round(self.engine.expected_g, 1),
                                   "drift_g": round(self.engine.drift_g, 1)})
        for ev in events:
            self._on_event(ev)
        if events:
            self._push_state()

    def _on_event(self, ev: ScaleEvent) -> None:
        self.db.log_event(ev)
        self.status_msg = ev.describe()
        self._broadcast("event", _event_dict(ev))
        self.cloud.push_async()
        if ev.kind == "UNKNOWN_ADD":
            self._broadcast("unknown", {"weight_g": round(ev.delta_g, 1)})

    # ----------------------------------------------------------- connection
    @property
    def connected(self) -> bool:
        return self.link is not None and self.link.is_open

    def ports(self) -> List[str]:
        return list_ports()

    def connect(self, source: str) -> None:
        self.disconnect()
        self.last_error = None
        if source == SIMULATOR:
            self.link = SimulatorLink()
        else:
            self.cfg.port = source
            self.cfg.save()
            self.link = SerialLink(source, self.cfg.baud)
        self.source = source
        self.engine.reset_after_tare()
        self.samples.clear()
        self.connecting = True
        self.link.start()
        self._notify("Connecting to %s..." % source)
        self._push_state()

    def disconnect(self) -> None:
        if self.link:
            self.link.stop()
            self.link = None
        self.connecting = False
        self.source = ""
        self._push_state()

    # ------------------------------------------------------------- commands
    def tare(self) -> None:
        if not self.connected:
            raise RuntimeError("Not connected.")
        if self.link.protocol == "v2":
            self.link.send("T")
            self.engine.reset_after_tare()  # keep PC state in step with hardware
            self._notify("Tared on the device. Baseline and basket reset.")
        else:
            self.engine.software_tare()     # original sketch has no T command
            self._notify("Tared in software. Baseline and basket reset.")
        self._push_state()

    def undo(self) -> List[dict]:
        events = self.engine.undo()
        if not events:
            self._notify("Nothing to undo.")
            return []
        for ev in events:
            self.db.delete_event(ev.ts, ev.kind)
        self._notify("Undone: %s" % "; ".join(ev.describe() for ev in events))
        self._push_state()
        return [_event_dict(ev) for ev in events]

    def new_session(self) -> None:
        self.engine.clear_session()
        self.db.session = time.strftime("%Y%m%d_%H%M%S")
        self._notify("New session started.")
        self._push_state()

    def dismiss_unknown(self) -> None:
        self.engine.pending_unknown_g = None
        self._push_state()

    # --------------------------------------------------------------- library
    def items(self) -> List[ItemDef]:
        return self.db.items()

    def check_overlaps(self, name: str, weight_g: float, tol: float,
                       exclude_id: Optional[int] = None) -> List[ItemDef]:
        return self.db.find_overlaps(ItemDef(-1, name, weight_g, tol), exclude_id)

    def add_item(self, name: str, weight_g: float,
                 tol: Optional[float] = None) -> ItemDef:
        if tol is None:
            tol = self.cfg.tolerance_for(weight_g)
        item = self.db.add_item(name, weight_g, tol)
        self._notify("Added '%s' at %.1f g." % (name, weight_g))
        self._push_state()
        return item

    def update_item(self, item_id: int, name: str, weight_g: float,
                    tol: float) -> None:
        self.db.update_item(item_id, name, weight_g, tol)
        self._notify("Updated '%s'." % name)
        self._push_state()

    def delete_item(self, item_id: int) -> None:
        self.db.delete_item(item_id)
        self._notify("Item deleted.")
        self._push_state()

    def teach(self, name: str, weight_g: Optional[float] = None,
              tol: Optional[float] = None) -> ItemDef:
        """Name the pending unknown weight, or whatever is on the platform now."""
        pending = self.engine.pending_unknown_g
        if weight_g is None:
            if pending is not None:
                weight_g = pending
            elif self.engine.is_stable:
                weight_g = self.engine.baseline_g - self.engine.expected_g
            else:
                raise RuntimeError("Wait for the reading to settle first.")
        if weight_g < self.cfg.min_event_g:
            raise RuntimeError("Place the item on the platform first.")
        item = self.add_item(name, round(weight_g, 1), tol)
        if pending is not None:
            ev = self.engine.resolve_pending(item)
            if ev:
                self._on_event(ev)
        self._push_state()
        return item

    # ----------------------------------------------------------- calibration
    def calibrate_read(self, known_g: float, timeout: float = 4.0) -> Tuple[float, float]:
        """Return (raw, factor). HX711: units = raw / factor, and we want kg."""
        if not self.connected:
            raise RuntimeError("Not connected.")
        if known_g <= 0:
            raise ValueError("Known weight must be positive.")
        if self.link.protocol != "v2":
            raise RuntimeError("Live calibration needs firmware v2 on the Arduino. "
                               "With the original sketch, edit calibration_factor "
                               "in the .ino and re-upload.")
        self._raw_ready.clear()
        self._raw_value = None
        self.link.send("R")
        if not self._raw_ready.wait(timeout):
            raise RuntimeError("No RAW reply from the device. Is the new "
                               "firmware flashed?")
        raw = float(self._raw_value)
        factor = raw / (known_g / 1000.0)
        return raw, factor

    def calibrate_apply(self, factor: float) -> None:
        if not self.connected:
            raise RuntimeError("Not connected.")
        self.link.send("C:%.1f" % factor)
        self.engine.reset_after_tare()
        self._notify("Calibration factor %.1f sent to the device." % factor)
        self._push_state()

    # ------------------------------------------------------------- simulator
    def sim(self) -> Optional[SimulatorLink]:
        return self.link if isinstance(self.link, SimulatorLink) else None

    def sim_place(self, grams: float) -> None:
        sim = self.sim()
        if not sim:
            raise RuntimeError("Connect with source = SIMULATOR first.")
        sim.place(grams)

    def sim_clear(self) -> None:
        sim = self.sim()
        if not sim:
            raise RuntimeError("Connect with source = SIMULATOR first.")
        sim.clear_platform()

    # -------------------------------------------------------------- settings
    SETTING_KEYS = ("stability_window", "stability_band_g", "min_event_g",
                    "zero_track_g", "zero_display_g", "allow_negative", "empty_zero_g",
                    "default_tolerance_g", "tolerance_pct",
                    "max_multiple", "teach_samples", "device_id",
                    "cloud_enabled", "cloud_url", "baud")

    def save_settings(self, values: Dict[str, object]) -> None:
        for key in self.SETTING_KEYS:
            if key not in values:
                continue
            current = getattr(self.cfg, key)
            raw = values[key]
            if isinstance(current, bool):
                val = bool(raw) if not isinstance(raw, str) else raw.lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int):
                val = int(float(raw))
            elif isinstance(current, float):
                val = float(raw)
            else:
                val = str(raw)
            setattr(self.cfg, key, val)
        self.cfg.save()
        self.engine.apply_config()
        self._notify("Settings saved.")
        self._push_state()

    # ------------------------------------------------------------- snapshot
    def state(self) -> dict:
        eng = self.engine
        lamp = "OFFLINE"
        if self.connecting:
            lamp = "CONNECTING"
        elif self.connected:
            lamp = "STABLE" if eng.is_stable else "MOVING"
        if self.last_error and not self.connected:
            lamp = "ERROR"
        return {
            "connected": self.connected,
            "connecting": self.connecting,
            "source": self.source,
            "simulator": self.sim() is not None,
            "lamp": lamp,
            "status": self.status_msg,
            "error": self.last_error,
            "weight_g": round(0.0 if abs(eng.last_g) < self.cfg.zero_display_g
                              else eng.last_g, 1),
            "stable": eng.is_stable,
            "quantity": eng.quantity,
            "distinct": eng.distinct,
            "expected_g": round(eng.expected_g, 1),
            "drift_g": round(eng.drift_g, 1),
            "pending_unknown_g": (round(eng.pending_unknown_g, 1)
                                  if eng.pending_unknown_g is not None else None),
            "basket": [{"id": e.item.id, "name": e.item.name, "count": e.count,
                        "unit_g": round(e.item.weight_g, 1),
                        "total_g": round(e.total_g, 1)} for e in eng.entries()],
            "history": [_event_dict(ev) for ev in eng.history[-200:]],
            "library": [_item_dict(i) for i in self.db.items()],
            "ports": self.ports(),
            "session": self.db.session,
            "samples": [[round(t, 2), g, s] for t, g, s in self.samples],
            "config": {k: getattr(self.cfg, k) for k in self.SETTING_KEYS},
        }
