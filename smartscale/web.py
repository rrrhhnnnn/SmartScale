"""Local web dashboard - standard library only, no Flask required.

    GET  /                      dashboard
    GET  /events                Server-Sent Events: sample | state | event | unknown | status | error
    GET  /api/state             full snapshot
    GET  /api/export.csv        download this session's history
    POST /api/connect           {"source": "COM3" | "SIMULATOR"}
    POST /api/disconnect
    POST /api/tare  /api/undo  /api/new_session  /api/dismiss_unknown
    POST /api/items             {"name","weight_g","tolerance_g"?,"force"?}
    PUT  /api/items/<id>        {"name","weight_g","tolerance_g"}
    DELETE /api/items/<id>
    POST /api/teach             {"name","weight_g"?,"tolerance_g"?,"force"?}
    POST /api/calibrate/read    {"known_g"}       -> {"raw","factor"}
    POST /api/calibrate/apply   {"factor"}
    POST /api/sim/place         {"grams"}         (negative to lift)
    POST /api/sim/clear
    POST /api/settings          {key: value, ...}
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .controller import ScaleController, _item_dict

if getattr(sys, "frozen", False):
    # PyInstaller extracts bundled data files under sys._MEIPASS.
    STATIC_DIR = os.path.join(sys._MEIPASS, "smartscale", "static")
else:
    STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEFAULT_PORT = 8765


class DashboardHandler(BaseHTTPRequestHandler):
    controller: ScaleController = None      # injected by serve()
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt, *args):       # silence per-request logging
        pass

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            return {}

    def _static(self, rel: str) -> None:
        rel = rel.lstrip("/") or "index.html"
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8"
                                                  if ctype.startswith("text/")
                                                  or "javascript" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ----------------------------------------------------------------- SSE
    def _events(self) -> None:
        c = self.controller
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = c.subscribe()
        try:
            self._sse("state", c.state())
            while True:
                try:
                    kind, data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse(kind, data)
        except (OSError, ValueError):
            pass                                  # client went away
        finally:
            c.unsubscribe(q)

    def _sse(self, kind: str, data) -> None:
        self.wfile.write(("event: %s\ndata: %s\n\n"
                          % (kind, json.dumps(data))).encode("utf-8"))
        self.wfile.flush()

    # --------------------------------------------------------------- routes
    def do_GET(self) -> None:                     # noqa: N802
        path = urlparse(self.path).path
        c = self.controller
        if path == "/events":
            return self._events()
        if path == "/api/state":
            return self._json(c.state())
        if path == "/api/export.csv":
            tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
            tmp.close()
            try:
                c.db.export_csv(tmp.name, session_only=True)
                with open(tmp.name, "rb") as fh:
                    data = fh.read()
            finally:
                os.unlink(tmp.name)
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="weigh_session_%s.csv"'
                             % c.db.session)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return self._static(path)

    def do_POST(self) -> None:                    # noqa: N802
        path = urlparse(self.path).path
        c = self.controller
        body = self._body()
        try:
            if path == "/api/connect":
                c.connect(str(body.get("source", "")).strip() or "SIMULATOR")
            elif path == "/api/disconnect":
                c.disconnect()
            elif path == "/api/tare":
                c.tare()
            elif path == "/api/undo":
                c.undo()
            elif path == "/api/new_session":
                c.new_session()
            elif path == "/api/dismiss_unknown":
                c.dismiss_unknown()
            elif path == "/api/items":
                return self._add_item(body)
            elif path == "/api/teach":
                return self._teach(body)
            elif path == "/api/calibrate/read":
                raw, factor = c.calibrate_read(float(body.get("known_g", 0)))
                return self._json({"ok": True, "raw": raw, "factor": round(factor, 1)})
            elif path == "/api/calibrate/apply":
                c.calibrate_apply(float(body["factor"]))
            elif path == "/api/sim/place":
                c.sim_place(float(body.get("grams", 0)))
            elif path == "/api/sim/clear":
                c.sim_clear()
            elif path == "/api/settings":
                c.save_settings(body)
            else:
                return self._error("Unknown endpoint", 404)
        except (RuntimeError, ValueError, KeyError) as exc:
            return self._error(str(exc))
        return self._json({"ok": True})

    def do_PUT(self) -> None:                     # noqa: N802
        path = urlparse(self.path).path
        c = self.controller
        if path.startswith("/api/items/"):
            body = self._body()
            try:
                item_id = int(path.rsplit("/", 1)[1])
                name = str(body["name"]).strip()
                weight = float(body["weight_g"])
                tol = float(body.get("tolerance_g") or c.cfg.tolerance_for(weight))
                if not name or weight <= 0 or tol <= 0:
                    raise ValueError("Name, weight and tolerance are required.")
                c.update_item(item_id, name, weight, tol)
            except (ValueError, KeyError) as exc:
                return self._error(str(exc))
            except Exception as exc:              # noqa: BLE001 - e.g. UNIQUE name
                return self._error(str(exc))
            return self._json({"ok": True})
        return self._error("Unknown endpoint", 404)

    def do_DELETE(self) -> None:                  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/items/"):
            try:
                self.controller.delete_item(int(path.rsplit("/", 1)[1]))
            except ValueError as exc:
                return self._error(str(exc))
            return self._json({"ok": True})
        return self._error("Unknown endpoint", 404)

    # -------------------------------------------------------------- helpers
    def _validated(self, body: dict, need_weight: bool = True):
        c = self.controller
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValueError("Enter a name.")
        weight = body.get("weight_g")
        weight = float(weight) if weight not in (None, "") else None
        if need_weight and (weight is None or weight <= 0):
            raise ValueError("Weight must be a positive number of grams.")
        tol = body.get("tolerance_g")
        tol = float(tol) if tol not in (None, "") else None
        if tol is not None and tol <= 0:
            raise ValueError("Tolerance must be positive.")
        if tol is None and weight is not None:
            tol = c.cfg.tolerance_for(weight)
        return name, weight, tol

    def _add_item(self, body: dict) -> None:
        c = self.controller
        try:
            name, weight, tol = self._validated(body)
            clashes = c.check_overlaps(name, weight, tol)
            if clashes and not body.get("force"):
                return self._json({"ok": False, "clash": True,
                                   "clashes": [_item_dict(i) for i in clashes]})
            item = c.add_item(name, weight, tol)
        except ValueError as exc:
            return self._error(str(exc))
        except Exception as exc:                  # noqa: BLE001
            return self._error("Could not add item: %s" % exc)
        return self._json({"ok": True, "item": _item_dict(item)})

    def _teach(self, body: dict) -> None:
        c = self.controller
        try:
            name, weight, tol = self._validated(body, need_weight=False)
            probe = weight
            if probe is None:
                probe = c.engine.pending_unknown_g
                if probe is None and c.engine.is_stable:
                    probe = c.engine.baseline_g - c.engine.expected_g
            if probe is not None:
                if tol is None:
                    tol = c.cfg.tolerance_for(probe)
                clashes = c.check_overlaps(name, probe, tol)
                if clashes and not body.get("force"):
                    return self._json({"ok": False, "clash": True,
                                       "weight_g": round(probe, 1),
                                       "clashes": [_item_dict(i) for i in clashes]})
            item = c.teach(name, weight, tol)
        except (ValueError, RuntimeError) as exc:
            return self._error(str(exc))
        except Exception as exc:                  # noqa: BLE001
            return self._error("Could not teach item: %s" % exc)
        return self._json({"ok": True, "item": _item_dict(item)})


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    controller = ScaleController()
    controller.start()
    DashboardHandler.controller = controller

    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    server.daemon_threads = True
    url = "http://127.0.0.1:%d" % port
    print("Smart Scale dashboard:  %s   (Ctrl+C to stop)" % url)

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        controller.stop()
