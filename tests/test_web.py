"""Web dashboard API test - starts the real server on a free port and drives
the whole pipeline through HTTP, exactly as the browser does.

    python tests/test_web.py
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import tempfile

# Isolate from the operator's real database and settings - ALWAYS.
_tmp = tempfile.mkdtemp(prefix="smartscale_test_")
os.environ["SMARTSCALE_DB"] = os.path.join(_tmp, "test.db")
os.environ["SMARTSCALE_CONFIG"] = os.path.join(_tmp, "test_config.json")

# Deterministic seed for tests, independent of the operator's real library.
import json as _json
_seed = os.path.join(_tmp, "seed.json")
with open(_seed, "w", encoding="utf-8") as _fh:
    _json.dump([
        {"name": "Eraser", "weight_g": 18.0, "tolerance_g": 2.0},
        {"name": "Marker Pen", "weight_g": 12.0, "tolerance_g": 2.0},
        {"name": "Phone", "weight_g": 227.0, "tolerance_g": 4.5},
        {"name": "Notebook", "weight_g": 240.0, "tolerance_g": 5.0},
        {"name": "Stapler", "weight_g": 155.0, "tolerance_g": 3.1},
    ], _fh)
os.environ["SMARTSCALE_SEED"] = _seed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smartscale.controller import ScaleController
from smartscale.web import DashboardHandler


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    port = free_port()
    controller = ScaleController()
    controller.start()
    DashboardHandler.controller = controller
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port

    def call(path, method="POST", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def state():
        return call("/api/state", "GET")[1]

    try:
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert r.status == 200 and b"<title>Smart Scale</title>" in r.read()
        print("PASS  dashboard page served")

        st, s = call("/api/state", "GET")
        assert st == 200 and "library" in s and "basket" in s
        print("PASS  /api/state snapshot (%d library items)" % len(s["library"]))

        call("/api/connect", body={"source": "SIMULATOR"})
        time.sleep(1.5)
        assert state()["connected"], "simulator did not connect"
        print("PASS  connect to simulator")

        lib = {i["name"]: i for i in state()["library"]}
        call("/api/sim/place", body={"grams": lib["Phone"]["weight_g"]})
        time.sleep(3.5)
        call("/api/sim/place", body={"grams": lib["Phone"]["weight_g"]})
        time.sleep(3.5)
        call("/api/sim/place", body={"grams": lib["Eraser"]["weight_g"]})
        time.sleep(3.5)
        s = state()
        basket = {b["name"]: b["count"] for b in s["basket"]}
        assert s["quantity"] == 3 and s["distinct"] == 2, (s["quantity"], s["distinct"])
        assert basket == {"Phone": 2, "Eraser": 1}, basket
        print("PASS  2 phones + 1 eraser -> Quantity 3, Items 2  %s" % basket)

        call("/api/sim/place", body={"grams": 512})
        time.sleep(3.5)
        s = state()
        assert s["pending_unknown_g"] is not None, "unknown item not flagged"
        assert s["history"][-1]["kind"] == "UNKNOWN_ADD"
        print("PASS  unknown weight flagged (%.1f g)" % s["pending_unknown_g"])

        st, r = call("/api/teach", body={"name": "Bottle"})
        assert r.get("ok"), r
        s = state()
        assert s["quantity"] == 4 and s["distinct"] == 3
        assert "Bottle" in {b["name"] for b in s["basket"]}
        print("PASS  teach -> Bottle learned and recognised (Qty 4 / Items 3)")

        st, r = call("/api/items", body={"name": "Clone", "weight_g": 228, "tolerance_g": 3})
        assert r.get("clash") and r["clashes"][0]["name"] == "Phone", r
        print("PASS  overlap detection refuses a 228 g item next to the 227 g Phone")

        st, r = call("/api/items", body={"name": "", "weight_g": 0})
        assert st == 400 and not r.get("ok")
        print("PASS  validation error surfaced: %s" % r["error"])

        call("/api/undo")
        assert state()["quantity"] == 3
        print("PASS  undo")

        req = urllib.request.Request(base + "/api/export.csv")
        with urllib.request.urlopen(req, timeout=5) as r:
            disp = r.headers.get("Content-Disposition", "")
            rows = r.read().decode().strip().splitlines()
        assert "attachment" in disp and rows[0].startswith("Timestamp,")
        assert len(rows) >= 4, rows
        print("PASS  CSV download: %d rows, %s" % (len(rows) - 1, disp))

        st, r = call("/api/calibrate/read", body={"known_g": 227})
        assert r.get("ok") and r["raw"] and r["factor"], r
        print("PASS  calibration read via simulator: raw %.0f -> factor %.1f" % (r["raw"], r["factor"]))

        call("/api/settings", body={"stability_band_g": 4.0})
        assert state()["config"]["stability_band_g"] == 4.0
        print("PASS  settings saved and applied")

        call("/api/tare")
        s = state()
        assert s["quantity"] == 0 and not s["basket"]
        print("PASS  tare clears basket and baseline")

        bottle = next(i for i in state()["library"] if i["name"] == "Bottle")
        call("/api/items/%d" % bottle["id"], "DELETE")
        assert "Bottle" not in {i["name"] for i in state()["library"]}
        print("PASS  item delete")

        call("/api/disconnect")
        print("PASS  disconnect")
    finally:
        server.shutdown()
        server.server_close()
        controller.stop()


if __name__ == "__main__":
    main()
