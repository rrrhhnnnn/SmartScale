"""Transport layer: a real serial link to the Arduino, and a built-in
simulator so the whole application can be developed, tested and demonstrated
with no hardware attached.

Both expose the same tiny interface:
    link.start() / link.stop() / link.send(cmd) / link.inbox  (queue.Queue)

Messages pushed to `inbox` are ("W", grams) | ("ACK", text) | ("RAW", value)
                            | ("INFO", text) | ("ERROR", text)
"""
from __future__ import annotations

import queue
import random
import re
import threading
import time
from typing import List, Optional

# The user's original sketch prints lines like
#   Measurement 3: Weight: 0.227 kg  [00:00:12]
# We accept those as well as the compact "W,seq,grams,millis" v2 protocol,
# so the software works with whichever firmware is on the board.
_LEGACY_RE = re.compile(r"Weight:\s*(-?\d+(?:\.\d+)?)\s*kg", re.IGNORECASE)


def list_ports() -> List[str]:
    """COM ports, or an empty list if pyserial is not installed."""
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    return [p.device for p in lp.comports()]


class BaseLink:
    def __init__(self) -> None:
        self.inbox: "queue.Queue" = queue.Queue()
        self.protocol: Optional[str] = None      # "v2" | "legacy", once known
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def is_open(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    def send(self, command: str) -> None:
        raise NotImplementedError

    def _run(self) -> None:
        raise NotImplementedError

    def _emit(self, line: str) -> None:
        """Parse one protocol line and push a message to the UI thread."""
        line = line.strip()
        if not line:
            return
        parts = line.split(",")
        tag = parts[0]
        try:
            if tag == "W" and len(parts) >= 3:
                self._set_protocol("v2")
                self.inbox.put(("W", float(parts[2])))
            elif tag == "RAW" and len(parts) >= 2:
                self.inbox.put(("RAW", float(parts[1])))
            elif tag == "ACK":
                self.inbox.put(("ACK", line))
            else:
                m = _LEGACY_RE.search(line)
                if m:
                    self._set_protocol("legacy")
                    self.inbox.put(("W", float(m.group(1)) * 1000.0))   # kg -> g
                else:
                    self.inbox.put(("INFO", line))
        except ValueError:
            self.inbox.put(("INFO", line))

    def _set_protocol(self, name: str) -> None:
        if self.protocol != name:
            self.protocol = name
            self.inbox.put(("PROTOCOL", name))


class SerialLink(BaseLink):
    """Reads the Arduino's line protocol on a background thread."""

    def __init__(self, port: str, baud: int = 9600, reset_on_open: bool = True):
        super().__init__()
        self.port = port
        self.baud = baud
        self.reset_on_open = reset_on_open
        self._ser = None
        self._tx_lock = threading.Lock()

    def _run(self) -> None:
        try:
            import serial
        except ImportError:
            self.inbox.put(("ERROR", "pyserial is not installed. "
                                     "Run:  pip install pyserial"))
            return

        try:
            self._ser = serial.Serial(
                self.port, self.baud, timeout=1.0,
                # Opening the port toggles DTR, which resets the Arduino.
                # That is usually what we want (clean tare), but it costs ~2 s.
                dsrdtr=False,
            )
            self._ser.dtr = self.reset_on_open
        except Exception as exc:                      # noqa: BLE001 - surfaced to UI
            self.inbox.put(("ERROR", "Could not open %s: %s" % (self.port, exc)))
            return

        if self.reset_on_open:
            self.inbox.put(("INFO", "Waiting for Arduino reset..."))
            time.sleep(2.0)
            try:
                self._ser.reset_input_buffer()
            except Exception:                          # noqa: BLE001
                pass

        self.inbox.put(("INFO", "Connected to %s @ %d" % (self.port, self.baud)))

        while not self._stop.is_set():
            try:
                raw = self._ser.readline()
                if raw:
                    self._emit(raw.decode("utf-8", errors="replace"))
            except Exception as exc:                   # noqa: BLE001
                self.inbox.put(("ERROR", "Serial read failed: %s" % exc))
                break

        try:
            self._ser.close()
        except Exception:                              # noqa: BLE001
            pass
        self.inbox.put(("INFO", "Disconnected."))

    def send(self, command: str) -> None:
        if not self._ser:
            return
        try:
            with self._tx_lock:
                self._ser.write((command.strip() + "\n").encode("ascii"))
        except Exception as exc:                       # noqa: BLE001
            self.inbox.put(("ERROR", "Serial write failed: %s" % exc))


class SimulatorLink(BaseLink):
    """A virtual scale.

    Emits samples at the same rate as the firmware and reproduces the two
    behaviours that matter for testing: sensor noise, and the unstable ramp
    while an item is being placed. Use place()/lift() from the UI.
    """

    RATE_HZ = 3.0
    NOISE_G = 0.6
    RAMP_S = 0.8

    def __init__(self, noise_g: float = NOISE_G):
        super().__init__()
        self.noise_g = noise_g
        self._actual = 0.0            # true weight on the virtual platform
        self._shown = 0.0             # what the ramp has reached so far
        self._ramp_until = 0.0
        self._offset = 0.0            # tare offset
        self._drift_per_s = 0.05      # a slow creep, like a real load cell
        self._t0 = time.time()
        self._lock = threading.Lock()

    # ---- virtual platform controls (called from the UI) ----
    def place(self, grams: float) -> None:
        with self._lock:
            self._actual += grams
            self._ramp_until = time.time() + self.RAMP_S

    def lift(self, grams: float) -> None:
        self.place(-grams)

    def clear_platform(self) -> None:
        with self._lock:
            self._actual = 0.0
            self._ramp_until = time.time() + self.RAMP_S

    # ---- link interface ----
    def send(self, command: str) -> None:
        command = command.strip()
        if command == "T":
            with self._lock:
                self._offset = self._shown + self._drift()
            self.inbox.put(("ACK", "ACK,TARE"))
        elif command.startswith("C:"):
            self.inbox.put(("ACK", "ACK,CAL,%s" % command[2:]))
        elif command == "R":
            with self._lock:
                raw = (self._actual - self._offset) / 1000.0 * 218500.0
            self.inbox.put(("RAW", round(raw, 0)))

    def _drift(self) -> float:
        return (time.time() - self._t0) * self._drift_per_s

    def _run(self) -> None:
        self.inbox.put(("INFO", "SIMULATOR mode - no hardware required."))
        self._set_protocol("v2")                  # the simulator speaks v2
        self.inbox.put(("INFO", "READY"))
        period = 1.0 / self.RATE_HZ

        while not self._stop.is_set():
            with self._lock:
                now = time.time()
                if now < self._ramp_until:
                    # mid-placement: move toward the target and add extra
                    # jitter, so the stability gate correctly refuses to fire
                    span = max(1e-6, self._ramp_until - (now - self.RAMP_S))
                    frac = 1.0 - (self._ramp_until - now) / span
                    self._shown += (self._actual - self._shown) * min(1.0, frac)
                    jitter = self.noise_g * 8
                else:
                    self._shown = self._actual
                    jitter = self.noise_g

                value = (self._shown - self._offset + self._drift()
                         + random.uniform(-jitter, jitter))

            self.inbox.put(("W", round(value, 1)))
            time.sleep(period)

        self.inbox.put(("INFO", "Simulator stopped."))
