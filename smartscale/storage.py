"""SQLite persistence for the item library and the event log, plus CSV export.

Every event is written to the local database first. The cloud push (phase 2)
then drains the unsynced rows in the background, so the app keeps working
perfectly with no network.
"""
from __future__ import annotations

import csv
import os
import sqlite3
import time
from typing import List, Optional

from .models import ItemDef, ScaleEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    weight_g    REAL    NOT NULL,
    tolerance_g REAL    NOT NULL DEFAULT 2.0
);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session        TEXT    NOT NULL,
    ts             REAL    NOT NULL,
    iso            TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    item_name      TEXT,
    item_id        INTEGER,
    count          INTEGER NOT NULL DEFAULT 1,
    delta_g        REAL    NOT NULL,
    total_g        REAL    NOT NULL,
    quantity       INTEGER NOT NULL,
    distinct_items INTEGER NOT NULL,
    status         TEXT    NOT NULL,
    synced         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session);
CREATE INDEX IF NOT EXISTS idx_events_synced  ON events(synced);
"""

CSV_HEADER = [
    "Timestamp", "Session", "Event", "Item", "Count",
    "Delta (g)", "Total (g)", "Quantity", "Distinct Items", "Status",
]


class Database:
    def __init__(self, path: str):
        self.path = path
        self.session = time.strftime("%Y%m%d_%H%M%S")
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------- item library
    def items(self) -> List[ItemDef]:
        rows = self.conn.execute(
            "SELECT id, name, weight_g, tolerance_g FROM items ORDER BY weight_g"
        ).fetchall()
        return [ItemDef(r["id"], r["name"], r["weight_g"], r["tolerance_g"])
                for r in rows]

    def add_item(self, name: str, weight_g: float, tolerance_g: float) -> ItemDef:
        cur = self.conn.execute(
            "INSERT INTO items (name, weight_g, tolerance_g) VALUES (?, ?, ?)",
            (name.strip(), float(weight_g), float(tolerance_g)),
        )
        self.conn.commit()
        return ItemDef(cur.lastrowid, name.strip(), float(weight_g), float(tolerance_g))

    def update_item(self, item_id: int, name: str, weight_g: float,
                    tolerance_g: float) -> None:
        self.conn.execute(
            "UPDATE items SET name = ?, weight_g = ?, tolerance_g = ? WHERE id = ?",
            (name.strip(), float(weight_g), float(tolerance_g), item_id),
        )
        self.conn.commit()

    def delete_item(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self.conn.commit()

    def find_overlaps(self, candidate: ItemDef,
                      exclude_id: Optional[int] = None) -> List[ItemDef]:
        """Items whose tolerance window collides with the candidate's.

        A collision means those two items can never be told apart by weight -
        worth catching when the library is edited, not at runtime.
        """
        return [i for i in self.items()
                if i.id != exclude_id and i.overlaps(candidate)]

    # ----------------------------------------------------------------- events
    def log_event(self, ev: ScaleEvent) -> int:
        cur = self.conn.execute(
            """INSERT INTO events (session, ts, iso, kind, item_name, item_id,
                                   count, delta_g, total_g, quantity,
                                   distinct_items, status, synced)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (self.session, ev.ts, ev.iso, ev.kind, ev.item_name, ev.item_id,
             ev.count, ev.delta_g, ev.total_g, ev.quantity, ev.distinct,
             ev.status),
        )
        self.conn.commit()
        return cur.lastrowid

    def delete_event(self, ts: float, kind: str) -> None:
        """Used by Undo, so the log matches what the operator actually meant."""
        self.conn.execute(
            "DELETE FROM events WHERE rowid = ("
            "  SELECT rowid FROM events WHERE ts = ? AND kind = ?"
            "  ORDER BY rowid DESC LIMIT 1)",
            (ts, kind),
        )
        self.conn.commit()

    def event_rows(self, session_only: bool = True) -> List[sqlite3.Row]:
        if session_only:
            return self.conn.execute(
                "SELECT * FROM events WHERE session = ? ORDER BY id",
                (self.session,),
            ).fetchall()
        return self.conn.execute("SELECT * FROM events ORDER BY id").fetchall()

    def unsynced(self, limit: int = 100) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE synced = 0 ORDER BY id LIMIT ?", (limit,)
        ).fetchall()

    def mark_synced(self, ids: List[int]) -> None:
        if not ids:
            return
        self.conn.executemany(
            "UPDATE events SET synced = 1 WHERE id = ?", [(i,) for i in ids]
        )
        self.conn.commit()

    # ------------------------------------------------------------ CSV export
    def export_csv(self, path: str, session_only: bool = True) -> int:
        rows = self.event_rows(session_only)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_HEADER)
            for r in rows:
                writer.writerow([
                    r["iso"], r["session"], r["kind"],
                    r["item_name"] or "", r["count"],
                    "%.1f" % r["delta_g"], "%.1f" % r["total_g"],
                    r["quantity"], r["distinct_items"], r["status"],
                ])
        return len(rows)

    def export_basket_csv(self, path: str, entries, quantity: int,
                          distinct: int) -> int:
        """Snapshot of what is on the platform right now."""
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Item", "Count", "Unit (g)", "Total (g)"])
            for e in entries:
                writer.writerow([e.item.name, e.count,
                                 "%.1f" % e.item.weight_g, "%.1f" % e.total_g])
            writer.writerow([])
            writer.writerow(["TOTAL QUANTITY", quantity])
            writer.writerow(["DISTINCT ITEMS", distinct])
        return len(entries)

    # ------------------------------------------------------------------ seed
    def seed_demo_items(self) -> None:
        """First run only (empty database): populate the item library.

        Prefers `seed_library.json` shipped beside this module / inside the
        packaged exe, so the real library travels with the build. Falls back
        to a few generic samples if that file is missing.
        """
        if self.items():
            return
        for name, weight, tol in self._seed_items():
            try:
                self.add_item(name, weight, tol)
            except sqlite3.IntegrityError:
                pass

    @staticmethod
    def _seed_items():
        """(name, weight_g, tolerance_g) tuples for a fresh database."""
        import json
        import sys

        # Tests point this at their own fixture so they never depend on the
        # operator's real library.
        path = os.environ.get("SMARTSCALE_SEED")
        if not path:
            if getattr(sys, "frozen", False):
                base = os.path.join(sys._MEIPASS, "smartscale")   # PyInstaller bundle
            else:
                base = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(base, "seed_library.json")

        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            items = [(d["name"], float(d["weight_g"]),
                      float(d.get("tolerance_g") or max(2.0, d["weight_g"] * 0.02)))
                     for d in data]
            if items:
                return items
        except (OSError, ValueError, KeyError):
            pass

        # fallback samples, used only if seed_library.json is unavailable
        return [(n, w, max(2.0, w * 0.02)) for n, w in
                (("Eraser", 18.0), ("Marker Pen", 12.0), ("Phone", 227.0),
                 ("Notebook", 240.0), ("Stapler", 155.0))]
