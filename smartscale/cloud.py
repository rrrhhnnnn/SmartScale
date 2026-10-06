"""Phase-2 cloud push.

Deliberately store-and-forward: events are already in SQLite before this runs,
so the app never blocks on the network and nothing is lost when it is offline.
Point `cloud_url` at any endpoint that accepts JSON (Node-RED, ThingsBoard,
Firebase, a Flask dashboard, an AWS API Gateway...) and flip `cloud_enabled`.

Uses urllib from the standard library so there is no extra dependency.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Callable, List, Optional


def record_from_row(row, device_id: str) -> dict:
    """The wire format. Fix this shape now and the dashboard never has to change."""
    return {
        "device_id": device_id,
        "session": row["session"],
        "ts": row["iso"],
        "event": row["kind"],
        "item": row["item_name"],
        "count": row["count"],
        "delta_g": round(row["delta_g"], 1),
        "total_g": round(row["total_g"], 1),
        "quantity": row["quantity"],
        "distinct_items": row["distinct_items"],
        "status": row["status"],
    }


class CloudSync:
    def __init__(self, cfg, db, notify: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.db = db
        self.notify = notify or (lambda msg: None)
        self._busy = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.cloud_enabled and self.cfg.cloud_url)

    def push_async(self) -> None:
        """Drain unsynced rows on a worker thread. Safe to call repeatedly."""
        if not self.enabled or self._busy.locked():
            return
        threading.Thread(target=self._push, daemon=True).start()

    def _push(self) -> None:
        with self._busy:
            rows = self.db.unsynced(limit=200)
            if not rows:
                return
            payload = {
                "device_id": self.cfg.device_id,
                "records": [record_from_row(r, self.cfg.device_id) for r in rows],
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.cfg.cloud_url, data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if 200 <= resp.status < 300:
                        self.db.mark_synced([r["id"] for r in rows])
                        self.notify("Cloud: pushed %d record(s)." % len(rows))
                    else:
                        self.notify("Cloud: HTTP %s, will retry." % resp.status)
            except (urllib.error.URLError, OSError) as exc:
                # Rows stay unsynced and are retried on the next push.
                self.notify("Cloud: offline (%s), queued %d record(s)."
                            % (exc, len(rows)))

    def preview(self) -> str:
        """Show the operator exactly what would be sent - handy for a demo."""
        rows = self.db.unsynced(limit=3)
        sample: List[dict] = [record_from_row(r, self.cfg.device_id) for r in rows]
        return json.dumps({"device_id": self.cfg.device_id,
                           "records": sample}, indent=2)
