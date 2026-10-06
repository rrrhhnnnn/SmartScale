"""GUI + simulator smoke test: builds the real window, drives the simulator,
and checks that items are recognised through the whole pipeline."""
import time
import os
import sys
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

from smartscale.app import App
from smartscale.link import SimulatorLink


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.01)


def main():
    app = App()
    app.update()
    print("PASS  window built (%d library items)" % len(app._items_cache))

    app.link = SimulatorLink()
    app.engine.reset_after_tare()
    app.link.start()
    app.btn_connect.config(text="Disconnect")
    pump(app, 2.0)
    print("PASS  simulator connected, live weight = %s" % app.var_weight.get())

    phone = next(i for i in app._items_cache if i.name == "Phone")      # 227 g
    eraser = next(i for i in app._items_cache if i.name == "Eraser")    # 18 g

    app.link.place(phone.weight_g)
    pump(app, 4.0)
    app.link.place(phone.weight_g)
    pump(app, 4.0)
    app.link.place(eraser.weight_g)
    pump(app, 4.0)

    qty, dist = app.engine.quantity, app.engine.distinct
    names = {e.item.name: e.count for e in app.engine.entries()}
    print("      basket = %s | Quantity %d | Items %d" % (names, qty, dist))
    assert qty == 3 and dist == 2, "expected Qty 3 / Items 2, got %d / %d" % (qty, dist)
    print("PASS  2 phones + 1 eraser -> Quantity 3, Different items 2")

    rows = len(app.tv_hist.get_children())
    assert rows == 3, "history should have 3 rows, has %d" % rows
    print("PASS  history table shows %d events" % rows)

    out = os.path.join(os.path.dirname(__file__), "_smoke_export.csv")
    n = app.db.export_csv(out, session_only=True)
    assert n == 3 and os.path.exists(out)
    with open(out, encoding="utf-8") as fh:
        lines = fh.read().strip().splitlines()
    print("PASS  CSV export wrote %d rows; header = %s" % (n, lines[0][:60]))
    print("      sample row = %s" % lines[-1])
    os.remove(out)

    app.link.clear_platform()
    pump(app, 6.0)
    assert app.engine.quantity == 0, app.engine.quantity
    print("PASS  clearing the platform empties the basket (Quantity 0)")

    app._on_close()
    print("PASS  clean shutdown")


if __name__ == "__main__":
    main()
