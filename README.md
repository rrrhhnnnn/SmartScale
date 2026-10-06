# Smart Weighing Scale — PC Control Software

Item-recognition weighing system for the IoT project.
**Load cell → HX711 → Arduino Uno → I2C LCD → USB → this app.**

Counts how many things are on the platform, how many *different* things they
are, and names each one — from weight alone.

```
place pencil          → +60 g    Pencil ×1                   Qty 1 | Items 1
place 2nd pencil      → +60 g    Pencil ×2                   Qty 2 | Items 1
place eraser          → +18 g    Pencil ×2, Eraser ×1        Qty 3 | Items 2
place book            → +240 g   Pencil ×2, Eraser ×1, Book  Qty 4 | Items 3
remove 2 pencils      → −120 g   Eraser ×1, Book ×1          Qty 2 | Items 2   (bulk)
remove the rest       → −258 g   (empty)                     Qty 0 | Items 0 → 0.000 kg
```

---

## Quick start

**Option A — no Python needed (the packaged build):**

Open `dist/` and double-click **`Start SmartScale (Web).bat`** (or run
`SmartScale.exe --web`). The dashboard opens in your browser. The item library
is already loaded — it is baked into the `.exe`. See
[Standalone .exe](#standalone-exe) below.

**Option B — run from source:**

```bash
cd "C:\D drive\Claude sessions\SmartScale"
```

```bash
python run.py
```

A desktop window opens. Pick source **`SIMULATOR (no hardware)`** →
**Connect** → **Simulator** tab → place items on the virtual platform. The
entire app is usable and demonstrable this way, with no hardware.

A browser-based dashboard is also available: `python run.py --web` (opens
`http://127.0.0.1:8765`). Same features, nicer presentation.

**2. To use the real scale:**

```bash
pip install pyserial
```

The software works with **your existing Arduino sketch as-is** (9600 baud,
`Measurement N: Weight: X kg` output) — it auto-detects the format. Plug in
the USB, choose the COM port, **Baud = 9600**, **Connect**. Close the Arduino
IDE's Serial Monitor first: only one program can hold a COM port.

Optionally flash `arduino/weigh_scale/weigh_scale.ino` (firmware v2, already
set to your pins DT 7 / SCK 2 and factor 215748). It adds two things the
original sketch cannot do: **Tare on the device** and **live calibration from
the PC** with no re-flashing. With the original sketch, Tare still works — it
is done in software — and calibration is done by editing the `.ino`.

| | Original sketch | Firmware v2 |
|---|---|---|
| Readings / second | ~1 (`get_units(10)`) | ~3 |
| Time to settle after placing | ~4–5 s | ~2 s |
| Tare button | software | on the device |
| Calibrate button | edit `.ino` and re-upload | live, from the PC |
| Item recognition, history, CSV | ✅ | ✅ |

---

## Why it works this way (the important bit)

A single load cell reports **one number: total weight**. A total cannot be
uniquely decomposed into a set of items — that is the subset-sum problem, and
it is ambiguous. If a pencil is 5 g and an eraser is 10 g, two pencils weigh
exactly the same as one eraser.

**So this software never decomposes the total.** It watches the *change* in
weight each time something is placed or removed, matches that delta against the
item library, and keeps the basket as latched state.

**Placing:** one item at a time, or several identical items together (the delta
is matched as `N × one item` — `MULTI ×2`). **Removing:** the engine searches
the things *currently in the basket* for the combination whose weight matches
what came off — so lifting 2 of 3 plates together, or a plate + a gear
together, is resolved correctly (a bulk removal). Searching only the basket
(never the whole library) keeps this exact and safe. That is how real inventory
scales work, and it is a defensible design decision to state in your report.

### The four layers

| Layer | What it does | Why |
|---|---|---|
| 1. Stability gate | Only acts once N consecutive samples agree within a band; uses the **median** | Nothing is recorded while a hand is on the platform |
| 2. Dead-band | Ignores changes under `min_event_g` | Thermal drift never creates phantom items |
| 3. **Latching** | The basket is *state*, never recomputed from current weight | **200 g drifting to 202 g cannot change the item shown** |
| 4. Tolerance match | `\|delta − item.weight\| ≤ item.tolerance` | Absorbs sensor noise and real-world variation |

Layer 3 is the direct answer to the "minor weight changes must not make the
item fluctuate" requirement. Because nothing ever re-derives the basket from
the live reading, a drifting number simply cannot move an item.

---

## Features

**Recognition**
- Names items by weight, with per-item tolerance windows
- **Quantity** (total things) and **Different items** (distinct types), live
- Multi-match: two identical items placed together → `MULTI ×2`
- **Bulk removal**: lift several items off together and the counts update
  correctly — 2 of 3 plates → `REMOVE plate ×2`; a plate + a gear → `BULK`;
  genuinely ambiguous removals are executed (fewest pieces) and flagged
  `AMBIGUOUS` for you to check
- Unknown weight → prompts to **teach** it into the library on the spot
- Overlapping items accepted but flagged `AMBIGUOUS`
- Lifting everything off at once → single `CLEAR` event

**Zero handling**
- Readings never go below 0 g (clamped; toggle off with **Allow negative**)
- `|reading| < 1 g` is shown as **0.000 kg** (display zero band)
- When **Quantity and Items are both 0**, any residual drift up to 10 g is
  re-zeroed automatically, so an empty platform reads exactly **0.000 kg**

**Item library** — add / edit / delete, **Teach from scale** (place it, name
it), and a warning when two items have colliding weight windows. On a fresh
install the library is pre-loaded (from `smartscale/seed_library.json`, also
baked into the `.exe`).

**Operations** — live reading, stability lamp, **Tare from the PC**, **Undo**
(reverts a whole bulk removal in one click), New Session, and a reconciliation
readout (measured vs expected).

**Calibration wizard** — tare, place a known weight, read, apply. The factor is
sent to the Arduino over serial with `C:<factor>`. **No more re-flashing to
tune calibration.**

**Data** — every event to SQLite, full **CSV download**, plus a basket snapshot
export.

**Cloud-ready** — store-and-forward push in `smartscale/cloud.py`. Set the
endpoint URL in Settings and tick Enable. Offline events queue and retry.

---

## Project layout

```
SmartScale/
├── run.py                            launcher (default = desktop window; --web = browser)
├── requirements.txt                  pyserial (hardware only); PyInstaller to build the exe
├── arduino/weigh_scale/
│   └── weigh_scale.ino               firmware v2 — optional (DT 7, SCK 2, 9600 baud)
├── smartscale/
│   ├── config.py                     tunable thresholds → config.json
│   ├── models.py                     ItemDef, BasketEntry, ScaleEvent
│   ├── engine.py                     ★ recognition engine (+ bulk removal, zero handling)
│   ├── storage.py                    SQLite library + event log + CSV + seeding
│   ├── seed_library.json             items a fresh database starts with (baked into the exe)
│   ├── link.py                       SerialLink (auto-detects firmware) + SimulatorLink
│   ├── cloud.py                      phase-2 push (store-and-forward)
│   ├── controller.py                 headless app core, pub/sub to any UI
│   ├── web.py                        HTTP + Server-Sent Events API (stdlib)
│   ├── static/                       the dashboard: index.html, app.css, app.js
│   └── app.py                        Tkinter desktop UI
├── tests/
│   ├── test_engine.py                22 engine tests
│   ├── test_web.py                   15 HTTP API tests through the real server
│   └── test_smoke.py                 Tkinter GUI + simulator pipeline
└── dist/                             built by PyInstaller (see Standalone .exe)
    ├── SmartScale.exe                one-file app, library baked in
    ├── Start SmartScale (Web).bat    double-click → SmartScale.exe --web
    └── smartscale.db                 created next to the exe; persists edits
```

### Dashboard

- **Live weight** with a 60-second trace, coloured green while stable and
  amber while moving, so you can *see* the stability gate working
- **Quantity** and **Different items** as large live counters
- **On the platform** — each recognised item with its count badge, plus a
  reconciliation line (expected vs measured, drift in grams)
- **History** table with one-click **Download CSV**
- **Item library** with add / edit / delete, overlap warnings inline
- **Simulator** tab, **Settings** tab (applies without restart)
- **Teach** dialog pops up automatically for any unrecognised weight
- **Calibration wizard** and **Tare / Undo / New session** in the toolbar
- Reload-safe: all state lives in the backend, the page just renders it

### HTTP API (what the page calls — also usable from any other client)

| Method | Path | Body |
|---|---|---|
| GET | `/events` | Server-Sent Events: `sample`, `state`, `event`, `unknown`, `status`, `error` |
| GET | `/api/state` | full snapshot |
| GET | `/api/export.csv` | CSV download |
| POST | `/api/connect` | `{"source": "COM3" \| "SIMULATOR"}` |
| POST | `/api/tare` `/api/undo` `/api/new_session` `/api/disconnect` | — |
| POST/PUT/DELETE | `/api/items[/id]` | `{"name","weight_g","tolerance_g"}` |
| POST | `/api/teach` | `{"name"}` — names the pending unknown weight |
| POST | `/api/calibrate/read` → `/api/calibrate/apply` | `{"known_g"}` → `{"factor"}` |
| POST | `/api/sim/place` `/api/sim/clear` | `{"grams"}` |
| POST | `/api/settings` | any config keys |

---

## Serial protocol (newline terminated)

The app **auto-detects** which firmware is on the board, so either works:

- **Original sketch** (default, **9600 baud**) — prints
  `Measurement N: Weight: X kg  [hh:mm:ss]`. The app parses the weight out of
  those lines. Tare is done in software; calibration by editing the `.ino`.
- **Firmware v2** (`arduino/weigh_scale/weigh_scale.ino`, **9600 baud**) — the
  compact machine protocol below, which also accepts PC commands.

| Direction | Message | Meaning |
|---|---|---|
| Arduino → PC | `W,<seq>,<grams>,<millis>` | live sample |
| Arduino → PC | `ACK,TARE` / `ACK,CAL,<f>` | command confirmed |
| Arduino → PC | `RAW,<value>` | raw count, for calibration |
| PC → Arduino | `T` | tare |
| PC → Arduino | `C:<factor>` | set calibration factor live |
| PC → Arduino | `R` | report raw count |

The firmware reports the true reading (it does not hide negatives); the **PC
side** clamps to 0 g and applies the zero bands, so the display and recognition
stay clean. Set the baud in Settings if your board differs.

---

## Tuning (Settings tab)

| Setting | Default | What it does |
|---|---|---|
| `stability_window` | 6 | samples that must agree before a reading counts |
| `stability_band_g` | 3.0 | max spread across that window (g) |
| `min_event_g` | 3.0 | changes smaller than this are drift, not items (g) |
| `zero_track_g` | 2.0 | auto re-zero band while the platform is empty (g) |
| `zero_display_g` | 1.0 | `|reading|` below this is shown as 0.000 kg |
| `empty_zero_g` | 10.0 | with 0 items, residual up to this is re-zeroed |
| `allow_negative` | off | on = show values below 0 g instead of clamping |
| `default_tolerance_g` | 2.0 | minimum ± window for a new item (g) |
| `tolerance_pct` | 2.0 | effective tolerance = max(default, weight × %) |
| `max_multiple` | 3 | detect up to N identical items placed together |
| `baud` | 9600 | serial speed; match your firmware |

Lower `stability_window` / `stability_band_g` for a snappier response, raise
them for reliability. Changes apply without restarting.

---

## Hardware note — read before the demo

Your 10 kg load cell has a noise floor of roughly **1–2 g**. Items lighter than
about **10 g are not reliably distinguishable** — a 5 g pencil sits barely
above the noise, and its ±2 g window would collide with everything.

- **For a reliable demo:** use items of **50 g or more** (phone, stapler,
  notebook, mouse, bottle).
- **For small items:** fit a **1 kg load cell** — same HX711, same wiring, same
  code, far better resolution.

This is a sensor limit, not a software one. Documenting it in your report is
worth more than hiding it.

---

## Running the tests

```bash
python tests/test_engine.py
```

```bash
python tests/test_web.py
```

```bash
python tests/test_smoke.py
```

`test_engine.py` (22 tests) covers the brief's exact scenario (2 pencils +
1 eraser + 1 book → Quantity 4, Items 3), the 200 → 202 g no-fluctuation
guarantee, single + multi + **bulk removal** (2 of 3 plates, mixed, ambiguous),
clearing, unknown/teach, undo of a bulk step, never-below-zero, the display
zero band, re-zero when empty, and firmware auto-detection. `test_web.py`
(15 tests) starts the real HTTP server and drives the full pipeline exactly as
the browser does — connect, recognise, teach, overlap refusal, undo, CSV
download, calibration, settings, tare, delete. `test_smoke.py` does the same
through the Tkinter window. All three run against a throwaway temporary
database, so they never touch your real library.

---

## Standalone .exe

A one-file Windows build lives in `dist/` — it runs with **no Python install**:

- **`SmartScale.exe`** — the whole app in one file. `SmartScale.exe --web` starts
  the browser dashboard; double-clicking opens the desktop window.
- **`Start SmartScale (Web).bat`** — double-click to launch in web mode.
- The **item library is baked into the exe** (from `seed_library.json`). A fresh
  run recreates it even on a machine with an empty folder; edits afterwards save
  to `smartscale.db` beside the exe and persist. Delete that `.db` and the
  original library comes back on next launch.

Rebuild after any code change:

```bash
cd "C:\D drive\Claude sessions\SmartScale" && python -m PyInstaller --noconfirm --onefile --name SmartScale --add-data "smartscale/static;smartscale/static" --add-data "smartscale/seed_library.json;smartscale" --hidden-import serial --hidden-import serial.tools.list_ports --collect-submodules serial run.py
```

To refresh the baked-in library from your current database first:

```bash
cd "C:\D drive\Claude sessions\SmartScale" && python -c "import sqlite3,json; c=sqlite3.connect('smartscale.db'); c.row_factory=sqlite3.Row; json.dump([dict(name=r['name'],weight_g=r['weight_g'],tolerance_g=r['tolerance_g']) for r in c.execute('select * from items')], open('smartscale/seed_library.json','w'), indent=2)"
```

> An `.exe` is a convenience, not a lock — Python executables can be unpacked and
> decompiled. For provable authorship, use a timestamped git history.

---

## Phase 2 — cloud dashboard

The wire format is already fixed, so the dashboard never has to change:

```json
{ "device_id": "scale-01", "ts": "2026-09-21T14:02:31",
  "event": "ADD", "item": "Pencil", "count": 1, "delta_g": 60.1,
  "total_g": 482.4, "quantity": 4, "distinct_items": 3, "status": "OK" }
```

Point `cloud_url` at any JSON endpoint (Node-RED, ThingsBoard, Firebase, a
Flask dashboard, AWS API Gateway). Events land in SQLite first and are pushed
in the background, so the app never blocks on the network and nothing is lost
when offline. Use **Preview cloud payload** in Settings to show the exact JSON
during a demo.
