from __future__ import annotations

import os
import queue
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import List, Optional

from . import APP_NAME, __version__
from .cloud import CloudSync
from .config import DB_PATH, Config
from .engine import RecognitionEngine
from .link import SerialLink, SimulatorLink, list_ports
from .models import ItemDef
from .storage import Database

SIMULATOR = "SIMULATOR (no hardware)"

BG = "#11161d"
PANEL = "#1a222d"
FG = "#e6edf3"
MUTED = "#8b98a8"
ACCENT = "#4ea1ff"
GREEN = "#2ea043"
AMBER = "#d29922"
RED = "#da3633"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("%s  v%s" % (APP_NAME, __version__))
        self.geometry("1180x760")
        self.minsize(1020, 680)
        self.configure(bg=BG)

        self.cfg = Config.load()
        self.db = Database(DB_PATH)
        self.db.seed_demo_items()
        self.engine = RecognitionEngine(self.cfg, self.db.items)
        self.cloud = CloudSync(self.cfg, self.db, notify=self._status)

        self.link = None
        self._protocol = None              # "v2" | "legacy" once the board talks
        self._paused = False               # true while a modal dialog is open
        self._raw_waiter = None            # calibration wizard callback
        self._items_cache: List[ItemDef] = self.db.items()

        self._init_style()
        self._build_ui()
        self._refresh_library()
        self._refresh_readout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(40, self._poll)

    # ------------------------------------------------------------------ style
    def _init_style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Panel.TLabel", background=PANEL, foreground=FG)
        st.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        st.configure("Big.TLabel", background=PANEL, foreground=FG,
                     font=("Segoe UI", 46, "bold"))
        st.configure("Metric.TLabel", background=PANEL, foreground=ACCENT,
                     font=("Segoe UI", 30, "bold"))
        st.configure("Head.TLabel", background=PANEL, foreground=MUTED,
                     font=("Segoe UI", 9))
        st.configure("TButton", padding=6)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", padding=(16, 8))
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                     foreground=FG, rowheight=24, borderwidth=0)
        st.configure("Treeview.Heading", background=BG, foreground=MUTED)
        st.map("Treeview", background=[("selected", ACCENT)])

    # --------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        self._build_toolbar()
        self._build_readout()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.nb = nb

        self.tab_session = ttk.Frame(nb, padding=10)
        self.tab_library = ttk.Frame(nb, padding=10)
        self.tab_sim = ttk.Frame(nb, padding=10)
        self.tab_settings = ttk.Frame(nb, padding=10)
        nb.add(self.tab_session, text="Session")
        nb.add(self.tab_library, text="Item Library")
        nb.add(self.tab_sim, text="Simulator")
        nb.add(self.tab_settings, text="Settings")

        self._build_session_tab()
        self._build_library_tab()
        self._build_simulator_tab()
        self._build_settings_tab()

        self.status = tk.StringVar(value="Ready. Pick a source and press Connect.")
        bar = tk.Label(self, textvariable=self.status, anchor="w", bg=PANEL,
                       fg=MUTED, padx=10, pady=5)
        bar.pack(fill="x", side="bottom")

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(12, 10))
        bar.pack(fill="x")

        ttk.Label(bar, text="Source").pack(side="left", padx=(0, 6))
        self.cbo_port = ttk.Combobox(bar, width=26, state="readonly")
        self.cbo_port.pack(side="left")
        self._refresh_ports()

        ttk.Button(bar, text="Refresh", command=self._refresh_ports)\
            .pack(side="left", padx=4)

        ttk.Label(bar, text="Baud").pack(side="left", padx=(8, 4))
        self.cbo_baud = ttk.Combobox(bar, width=8, state="readonly",
                                     values=("9600", "115200"))
        self.cbo_baud.set(str(self.cfg.baud))
        self.cbo_baud.pack(side="left")
        self.btn_connect = ttk.Button(bar, text="Connect",
                                      command=self._toggle_connect)
        self.btn_connect.pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)

        ttk.Button(bar, text="Tare", command=self._tare).pack(side="left", padx=3)
        ttk.Button(bar, text="Calibrate...", command=self._open_calibration)\
            .pack(side="left", padx=3)
        ttk.Button(bar, text="Undo", command=self._undo).pack(side="left", padx=3)
        ttk.Button(bar, text="New Session", command=self._new_session)\
            .pack(side="left", padx=3)

        self.lbl_lamp = tk.Label(bar, text="  OFFLINE  ", bg=MUTED, fg="#000",
                                 font=("Segoe UI", 9, "bold"), padx=8, pady=3)
        self.lbl_lamp.pack(side="right")

    def _build_readout(self) -> None:
        wrap = ttk.Frame(self, padding=(12, 0, 12, 10))
        wrap.pack(fill="x")

        card = ttk.Frame(wrap, style="Panel.TFrame", padding=16)
        card.pack(fill="x")
        for col in range(4):
            card.columnconfigure(col, weight=1)

        ttk.Label(card, text="LIVE WEIGHT", style="Head.TLabel")\
            .grid(row=0, column=0, sticky="w")
        self.var_weight = tk.StringVar(value="0.000 kg")
        ttk.Label(card, textvariable=self.var_weight, style="Big.TLabel")\
            .grid(row=1, column=0, sticky="w")
        self.var_grams = tk.StringVar(value="0.0 g")
        ttk.Label(card, textvariable=self.var_grams, style="Muted.TLabel")\
            .grid(row=2, column=0, sticky="w")

        ttk.Label(card, text="QUANTITY (total items)", style="Head.TLabel")\
            .grid(row=0, column=1, sticky="w")
        self.var_qty = tk.StringVar(value="0")
        ttk.Label(card, textvariable=self.var_qty, style="Metric.TLabel")\
            .grid(row=1, column=1, sticky="w")

        ttk.Label(card, text="DIFFERENT ITEMS", style="Head.TLabel")\
            .grid(row=0, column=2, sticky="w")
        self.var_distinct = tk.StringVar(value="0")
        ttk.Label(card, textvariable=self.var_distinct, style="Metric.TLabel")\
            .grid(row=1, column=2, sticky="w")

        ttk.Label(card, text="RECONCILIATION", style="Head.TLabel")\
            .grid(row=0, column=3, sticky="w")
        self.var_drift = tk.StringVar(value="--")
        ttk.Label(card, textvariable=self.var_drift, style="Panel.TLabel",
                  font=("Segoe UI", 12)).grid(row=1, column=3, sticky="w")
        self.var_expected = tk.StringVar(value="")
        ttk.Label(card, textvariable=self.var_expected, style="Muted.TLabel")\
            .grid(row=2, column=3, sticky="w")

    def _build_session_tab(self) -> None:
        tab = self.tab_session
        tab.columnconfigure(0, weight=2)
        tab.columnconfigure(1, weight=3)
        tab.rowconfigure(1, weight=1)

        ttk.Label(tab, text="On the platform").grid(row=0, column=0, sticky="w")
        self.tv_basket = ttk.Treeview(
            tab, columns=("item", "count", "unit", "total"),
            show="headings", height=9)
        for key, text, width in [("item", "Item", 180), ("count", "Count", 60),
                                 ("unit", "Unit (g)", 90), ("total", "Total (g)", 95)]:
            self.tv_basket.heading(key, text=text)
            self.tv_basket.column(key, width=width, anchor="w")
        self.tv_basket.grid(row=1, column=0, sticky="nsew", padx=(0, 10), pady=4)

        head = ttk.Frame(tab)
        head.grid(row=0, column=1, sticky="ew")
        ttk.Label(head, text="Measurement history").pack(side="left")
        ttk.Button(head, text="Download CSV", command=self._export_csv)\
            .pack(side="right")
        ttk.Button(head, text="Export basket", command=self._export_basket)\
            .pack(side="right", padx=6)

        self.tv_hist = ttk.Treeview(
            tab, columns=("time", "event", "item", "cnt", "delta", "total",
                          "qty", "items", "status"),
            show="headings", height=9)
        for key, text, width in [
            ("time", "Time", 74), ("event", "Event", 72), ("item", "Item", 130),
            ("cnt", "x", 32), ("delta", "Delta (g)", 78), ("total", "Total (g)", 80),
            ("qty", "Qty", 44), ("items", "Items", 48), ("status", "Status", 88),
        ]:
            self.tv_hist.heading(key, text=text)
            self.tv_hist.column(key, width=width, anchor="w")
        self.tv_hist.grid(row=1, column=1, sticky="nsew", pady=4)
        self.tv_hist.tag_configure("unknown", foreground=AMBER)
        self.tv_hist.tag_configure("ambiguous", foreground=AMBER)
        self.tv_hist.tag_configure("remove", foreground=MUTED)

    def _build_library_tab(self) -> None:
        tab = self.tab_library
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(bar, text="Items are recognised by weight. "
                           "Use 'Teach from scale' to add one by placing it.")\
            .pack(side="left")
        ttk.Button(bar, text="Teach from scale", command=self._teach_from_scale)\
            .pack(side="right")
        ttk.Button(bar, text="Delete", command=self._delete_item)\
            .pack(side="right", padx=6)
        ttk.Button(bar, text="Edit", command=self._edit_item).pack(side="right")
        ttk.Button(bar, text="Add", command=self._add_item).pack(side="right", padx=6)

        self.tv_lib = ttk.Treeview(
            tab, columns=("name", "weight", "tol", "window"),
            show="headings")
        for key, text, width in [("name", "Name", 220), ("weight", "Weight (g)", 110),
                                 ("tol", "Tolerance (g)", 120),
                                 ("window", "Accepted range (g)", 200)]:
            self.tv_lib.heading(key, text=text)
            self.tv_lib.column(key, width=width, anchor="w")
        self.tv_lib.grid(row=1, column=0, sticky="nsew")
        self.tv_lib.bind("<Double-1>", lambda _e: self._edit_item())
        self.tv_lib.tag_configure("clash", foreground=AMBER)

    def _build_simulator_tab(self) -> None:
        tab = self.tab_sim
        ttk.Label(tab, text="Virtual platform - works without any hardware. "
                            "Connect with source = SIMULATOR, then place items.")\
            .pack(anchor="w", pady=(0, 10))

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Library item").pack(side="left")
        self.cbo_sim_item = ttk.Combobox(row, width=34, state="readonly")
        self.cbo_sim_item.pack(side="left", padx=8)
        ttk.Button(row, text="Place", command=lambda: self._sim_item(+1))\
            .pack(side="left", padx=3)
        ttk.Button(row, text="Remove", command=lambda: self._sim_item(-1))\
            .pack(side="left", padx=3)

        row2 = ttk.Frame(tab)
        row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="Custom weight (g)").pack(side="left")
        self.var_sim_g = tk.StringVar(value="120")
        ttk.Entry(row2, textvariable=self.var_sim_g, width=12)\
            .pack(side="left", padx=8)
        ttk.Button(row2, text="Place", command=lambda: self._sim_custom(+1))\
            .pack(side="left", padx=3)
        ttk.Button(row2, text="Lift", command=lambda: self._sim_custom(-1))\
            .pack(side="left", padx=3)
        ttk.Button(row2, text="Clear platform", command=self._sim_clear)\
            .pack(side="left", padx=16)

        ttk.Label(tab, text="Tip: place items ONE AT A TIME, exactly as a real "
                            "operator would. Two identical items placed together "
                            "are still detected via multi-match.",
                  wraplength=900, foreground=MUTED).pack(anchor="w", pady=14)

    def _build_settings_tab(self) -> None:
        tab = self.tab_settings
        self._setting_vars = {}

        fields = [
            ("stability_window", "Stability window (samples)",
             "How many consecutive samples must agree before a reading counts."),
            ("stability_band_g", "Stability band (g)",
             "Max spread across that window. Bigger = settles sooner, less precise."),
            ("min_event_g", "Dead-band / min item weight (g)",
             "Changes smaller than this are treated as drift, never as an item."),
            ("zero_track_g", "Auto zero-tracking band (g)",
             "Silently re-zeroes when the platform is empty and near zero."),
            ("zero_display_g", "Display zero band (g)",
             "Readings between -X and +X are shown as 0.000. Display only."),
            ("empty_zero_g", "Re-zero when empty (g)",
             "With 0 items, residual up to this is zeroed so it reads 0.000."),
            ("default_tolerance_g", "Default tolerance (g)",
             "Minimum +/- window for a new item."),
            ("tolerance_pct", "Tolerance percent (%)",
             "Effective tolerance = max(default, weight x percent)."),
            ("max_multiple", "Max identical items at once",
             "Detect up to N of the same item placed together."),
            ("teach_samples", "Teach samples", "Readings averaged when teaching."),
            ("device_id", "Device ID", "Identifies this scale in the cloud feed."),
            ("cloud_url", "Cloud endpoint URL",
             "POST target for phase 2. Leave blank to stay offline."),
        ]
        for i, (key, label, hint) in enumerate(fields):
            ttk.Label(tab, text=label).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(getattr(self.cfg, key)))
            self._setting_vars[key] = var
            ttk.Entry(tab, textvariable=var, width=22)\
                .grid(row=i, column=1, sticky="w", padx=10)
            ttk.Label(tab, text=hint, foreground=MUTED)\
                .grid(row=i, column=2, sticky="w")

        row = len(fields)
        self.var_neg = tk.BooleanVar(value=self.cfg.allow_negative)
        ttk.Checkbutton(tab, text="Allow negative readings (below 0 g)",
                        variable=self.var_neg)\
            .grid(row=row, column=1, columnspan=2, sticky="w", padx=10, pady=(10, 2))
        ttk.Label(tab, text="Off = anything under 0 g is clamped to 0.000.",
                  foreground=MUTED).grid(row=row + 1, column=1, columnspan=2,
                                         sticky="w", padx=10)
        row += 2
        self.var_cloud = tk.BooleanVar(value=self.cfg.cloud_enabled)
        ttk.Checkbutton(tab, text="Enable cloud push", variable=self.var_cloud)\
            .grid(row=row, column=1, sticky="w", padx=10, pady=10)
        ttk.Button(tab, text="Save settings", command=self._save_settings)\
            .grid(row=row, column=0, sticky="w", pady=10)
        ttk.Button(tab, text="Preview cloud payload", command=self._preview_cloud)\
            .grid(row=row, column=2, sticky="w")

    # --------------------------------------------------------------- connection
    def _refresh_ports(self) -> None:
        ports = list_ports()
        values = ports + [SIMULATOR]
        self.cbo_port["values"] = values
        if self.cfg.port in values:
            self.cbo_port.set(self.cfg.port)
        elif ports:
            self.cbo_port.set(ports[0])
        else:
            self.cbo_port.set(SIMULATOR)

    def _toggle_connect(self) -> None:
        if self.link and self.link.is_open:
            self.link.stop()
            self.link = None
            self._protocol = None
            self.btn_connect.config(text="Connect")
            self._set_lamp("OFFLINE", MUTED)
            return

        source = self.cbo_port.get()
        if source == SIMULATOR:
            self.link = SimulatorLink()
        else:
            if not list_ports():
                messagebox.showerror(
                    "pyserial missing",
                    "No COM ports found.\n\nEither the Arduino is unplugged, or "
                    "pyserial is not installed:\n\n    pip install pyserial\n\n"
                    "You can still use SIMULATOR mode.")
                return
            self.cfg.port = source
            try:
                self.cfg.baud = int(self.cbo_baud.get())
            except ValueError:
                self.cfg.baud = 9600
            self.cfg.save()
            self.link = SerialLink(source, self.cfg.baud)

        self._protocol = None
        self.engine.reset_after_tare()
        self.link.start()
        self.btn_connect.config(text="Disconnect")
        self._set_lamp("CONNECTING", AMBER)

    def _set_lamp(self, text: str, colour: str) -> None:
        self.lbl_lamp.config(text="  %s  " % text, bg=colour,
                             fg="#000" if colour != MUTED else "#000")

    # --------------------------------------------------------------- main loop
    def _poll(self) -> None:
        if self.link:
            drained = 0
            while drained < 60:
                try:
                    kind, payload = self.link.inbox.get_nowait()
                except queue.Empty:
                    break
                drained += 1
                self._handle_message(kind, payload)
        self.after(40, self._poll)

    def _handle_message(self, kind: str, payload) -> None:
        if kind == "W":
            self._on_sample(float(payload))
        elif kind == "RAW":
            if self._raw_waiter:
                cb, self._raw_waiter = self._raw_waiter, None
                cb(float(payload))
        elif kind == "ACK":
            self._status("Device: %s" % payload)
        elif kind == "PROTOCOL":
            self._on_protocol(str(payload))
        elif kind == "ERROR":
            self._status(payload)
            self._set_lamp("ERROR", RED)
            messagebox.showerror("Connection", str(payload))
        else:
            self._status(str(payload))

    def _on_protocol(self, name: str) -> None:
        """The board has started talking; adapt to whichever firmware it runs."""
        self._protocol = name
        if name == "legacy":
            # The original sketch sends ~1 reading/s (get_units(10) at 10 SPS),
            # so a 6-sample window would mean 6 s to settle. Use a shorter one.
            self.engine.set_window(4)
            self._status("Original firmware detected (1 reading/s). "
                         "Tare works in software; live calibration needs firmware v2.")
        else:
            self.engine.apply_config()
            self._status("Firmware v2 detected. Tare and calibration run on the device.")

    def _on_sample(self, grams: float) -> None:
        if self._paused:                               # dialog open: display only
            events = []
            shown = grams - self.engine.tare_offset_g
        else:
            events = self.engine.on_sample(grams)
            shown = self.engine.last_g                 # tare-corrected, re-zeroed
        if not self.cfg.allow_negative and shown < 0:  # never under 0 g
            shown = 0.0
        if abs(shown) < self.cfg.zero_display_g:       # zero band: |x| < 1 g -> 0
            shown = 0.0
        self.var_weight.set("%.3f kg" % (shown / 1000.0))
        self.var_grams.set("%.1f g" % shown)

        if self._paused:
            return

        self._set_lamp("STABLE" if self.engine.is_stable else "MOVING",
                       GREEN if self.engine.is_stable else AMBER)
        for ev in events:
            self._on_event(ev)
        if events:
            self._refresh_basket()
        self._refresh_readout()

    def _on_event(self, ev) -> None:
        self.db.log_event(ev)
        tag = ""
        if ev.status == "UNKNOWN":
            tag = "unknown"
        elif ev.status == "AMBIGUOUS":
            tag = "ambiguous"
        elif ev.kind in ("REMOVE", "CLEAR"):
            tag = "remove"

        self.tv_hist.insert(
            "", "end", iid="ev%d" % id(ev), tags=(tag,),
            values=(ev.clock, ev.kind, ev.item_name or "-", ev.count,
                    "%+.1f" % ev.delta_g, "%.1f" % ev.total_g,
                    ev.quantity, ev.distinct, ev.status))
        self.tv_hist.yview_moveto(1.0)
        self._status(ev.describe())
        self.cloud.push_async()

        if ev.kind == "UNKNOWN_ADD":
            self._prompt_teach(self.engine.pending_unknown_g)

    # ----------------------------------------------------------------- refresh
    def _refresh_readout(self) -> None:
        self.var_qty.set(str(self.engine.quantity))
        self.var_distinct.set(str(self.engine.distinct))
        if self.engine.basket:
            self.var_drift.set("%+.1f g off" % self.engine.drift_g)
            self.var_expected.set("expected %.1f g" % self.engine.expected_g)
        else:
            self.var_drift.set("--")
            self.var_expected.set("platform empty")

    def _refresh_basket(self) -> None:
        self.tv_basket.delete(*self.tv_basket.get_children())
        for e in self.engine.entries():
            self.tv_basket.insert("", "end", values=(
                e.item.name, e.count, "%.1f" % e.item.weight_g,
                "%.1f" % e.total_g))

    def _refresh_library(self) -> None:
        self._items_cache = self.db.items()
        self.tv_lib.delete(*self.tv_lib.get_children())
        for item in self._items_cache:
            clash = [o.name for o in self._items_cache
                     if o.id != item.id and o.overlaps(item)]
            self.tv_lib.insert(
                "", "end", iid=str(item.id), tags=("clash",) if clash else (),
                values=(item.name, "%.1f" % item.weight_g,
                        "%.1f" % item.tolerance_g,
                        "%.1f - %.1f%s" % (item.low, item.high,
                                           "   clashes with " + ", ".join(clash)
                                           if clash else "")))
        self.cbo_sim_item["values"] = ["%s  (%.1f g)" % (i.name, i.weight_g)
                                       for i in self._items_cache]
        if self._items_cache and not self.cbo_sim_item.get():
            self.cbo_sim_item.current(0)

    # --------------------------------------------------------------- commands
    def _tare(self) -> None:
        if not (self.link and self.link.is_open):
            self._status("Not connected.")
            return
        if self._protocol == "v2":
            self.link.send("T")                 # firmware zeroes itself...
            self.engine.reset_after_tare()      # ...so our state must zero too
            self._status("Tared on the device. Baseline and basket reset.")
        else:
            # Original sketch has no tare command: zero it in software instead.
            self.engine.software_tare()
            self._status("Tared in software. Baseline and basket reset.")
        self.var_weight.set("0.000 kg")
        self.var_grams.set("0.0 g")
        self._refresh_basket()
        self._refresh_readout()

    def _undo(self) -> None:
        events = self.engine.undo()                    # a bulk removal is one step
        if not events:
            self._status("Nothing to undo.")
            return
        for ev in events:
            self.db.delete_event(ev.ts, ev.kind)
            iid = "ev%d" % id(ev)
            if self.tv_hist.exists(iid):
                self.tv_hist.delete(iid)
        self._refresh_basket()
        self._refresh_readout()
        self._status("Undone: %s" % "; ".join(ev.describe() for ev in events))

    def _new_session(self) -> None:
        if not messagebox.askyesno("New session",
                                   "Clear the basket and history view?"):
            return
        self.engine.clear_session()
        self.db.session = time.strftime("%Y%m%d_%H%M%S")
        self.tv_hist.delete(*self.tv_hist.get_children())
        self._refresh_basket()
        self._refresh_readout()
        self._status("New session started.")

    def _export_csv(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile="weigh_session_%s.csv" % self.db.session)
        if not path:
            return
        n = self.db.export_csv(path, session_only=True)
        self._status("Exported %d event(s) to %s" % (n, os.path.basename(path)))
        messagebox.showinfo("Export complete", "%d rows written to:\n%s" % (n, path))

    def _export_basket(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile="basket_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))
        if not path:
            return
        self.db.export_basket_csv(path, self.engine.entries(),
                                  self.engine.quantity, self.engine.distinct)
        self._status("Basket exported to %s" % os.path.basename(path))

    def _save_settings(self) -> None:
        try:
            for key, var in self._setting_vars.items():
                current = getattr(self.cfg, key)
                value = var.get().strip()
                setattr(self.cfg, key,
                        type(current)(value) if not isinstance(current, str)
                        else value)
            self.cfg.cloud_enabled = bool(self.var_cloud.get())
            self.cfg.allow_negative = bool(self.var_neg.get())
        except ValueError as exc:
            messagebox.showerror("Invalid value", str(exc))
            return
        self.cfg.save()
        self.engine.apply_config()
        self._status("Settings saved.")

    def _preview_cloud(self) -> None:
        messagebox.showinfo("Cloud payload preview", self.cloud.preview())

    def _status(self, msg: str) -> None:
        try:
            self.status.set(msg)
        except tk.TclError:
            pass

    # ------------------------------------------------------------- item library
    def _selected_item(self) -> Optional[ItemDef]:
        sel = self.tv_lib.selection()
        if not sel:
            return None
        return next((i for i in self._items_cache if str(i.id) == sel[0]), None)

    def _add_item(self, weight: Optional[float] = None) -> Optional[ItemDef]:
        dlg = ItemDialog(self, self.cfg, weight_g=weight)
        if not dlg.result:
            return None
        name, weight_g, tol = dlg.result
        candidate = ItemDef(-1, name, weight_g, tol)
        clashes = self.db.find_overlaps(candidate)
        if clashes and not messagebox.askyesno(
                "Overlapping weights",
                "%s (%.1f +/- %.1f g) overlaps:\n\n  %s\n\n"
                "These items can never be told apart by weight alone.\n"
                "Add it anyway?" % (name, weight_g, tol,
                                    "\n  ".join("%s  %.1f - %.1f g"
                                                % (c.name, c.low, c.high)
                                                for c in clashes))):
            return None
        try:
            item = self.db.add_item(name, weight_g, tol)
        except Exception as exc:                       # noqa: BLE001
            messagebox.showerror("Could not add item", str(exc))
            return None
        self._refresh_library()
        self._status("Added '%s' at %.1f g." % (name, weight_g))
        return item

    def _edit_item(self) -> None:
        item = self._selected_item()
        if not item:
            self._status("Select an item first.")
            return
        dlg = ItemDialog(self, self.cfg, item=item)
        if not dlg.result:
            return
        name, weight_g, tol = dlg.result
        self.db.update_item(item.id, name, weight_g, tol)
        self._refresh_library()
        self._status("Updated '%s'." % name)

    def _delete_item(self) -> None:
        item = self._selected_item()
        if not item:
            self._status("Select an item first.")
            return
        if not messagebox.askyesno("Delete item",
                                   "Remove '%s' from the library?" % item.name):
            return
        self.db.delete_item(item.id)
        self._refresh_library()
        self._status("Deleted '%s'." % item.name)

    def _teach_from_scale(self) -> None:
        """Place an item on an empty platform, then name it."""
        if self.engine.pending_unknown_g is not None:
            self._prompt_teach(self.engine.pending_unknown_g)
            return
        if not (self.link and self.link.is_open):
            self._status("Connect first, then place the item and press Teach.")
            return
        if not self.engine.is_stable:
            messagebox.showwarning("Not stable",
                                   "Wait for the reading to settle (green STABLE "
                                   "lamp), then press Teach again.")
            return
        weight = self.engine.baseline_g - self.engine.expected_g
        if weight < self.cfg.min_event_g:
            messagebox.showwarning("Nothing to teach",
                                   "Place the item on the platform first.")
            return
        self._add_item(weight=round(weight, 1))

    def _prompt_teach(self, weight: Optional[float]) -> None:
        if weight is None:
            return
        self._paused = True
        try:
            if messagebox.askyesno(
                    "Unknown item",
                    "An unrecognised item of %.1f g was placed.\n\n"
                    "Add it to the library now?" % weight):
                item = self._add_item(weight=round(weight, 1))
                if item:
                    ev = self.engine.resolve_pending(item)
                    if ev:
                        self._on_event(ev)
                        self._refresh_basket()
                        self._refresh_readout()
            else:
                self.engine.pending_unknown_g = None
        finally:
            self._paused = False

    # ------------------------------------------------------------- calibration
    def _open_calibration(self) -> None:
        if not (self.link and self.link.is_open):
            messagebox.showinfo("Calibration", "Connect to the scale first.")
            return
        if self._protocol != "v2":
            messagebox.showinfo(
                "Calibration",
                "Live calibration needs firmware v2 on the Arduino\n"
                "(arduino\\weigh_scale\\weigh_scale.ino).\n\n"
                "With the original sketch, calibrate by editing\n"
                "calibration_factor in the .ino and re-uploading:\n\n"
                "   new = old x (shown weight / actual weight)")
            return
        CalibrationDialog(self)

    def request_raw(self, callback) -> None:
        self._raw_waiter = callback
        self.link.send("R")

    # --------------------------------------------------------------- simulator
    def _sim(self) -> Optional[SimulatorLink]:
        if isinstance(self.link, SimulatorLink) and self.link.is_open:
            return self.link
        messagebox.showinfo("Simulator",
                            "Connect with source = %s first." % SIMULATOR)
        return None

    def _sim_item(self, sign: int) -> None:
        sim = self._sim()
        if not sim:
            return
        idx = self.cbo_sim_item.current()
        if idx < 0 or idx >= len(self._items_cache):
            return
        item = self._items_cache[idx]
        # a real object is never exactly its nominal weight
        import random
        jitter = random.uniform(-item.tolerance_g * 0.4, item.tolerance_g * 0.4)
        sim.place(sign * (item.weight_g + jitter))

    def _sim_custom(self, sign: int) -> None:
        sim = self._sim()
        if not sim:
            return
        try:
            grams = float(self.var_sim_g.get())
        except ValueError:
            messagebox.showerror("Simulator", "Enter a number of grams.")
            return
        sim.place(sign * grams)

    def _sim_clear(self) -> None:
        sim = self._sim()
        if sim:
            sim.clear_platform()

    # ------------------------------------------------------------------ close
    def _on_close(self) -> None:
        try:
            if self.link:
                self.link.stop()
            self.cfg.save()
            self.db.close()
        finally:
            self.destroy()


class ItemDialog(tk.Toplevel):
    """Add / edit one library item."""

    def __init__(self, parent: App, cfg: Config,
                 item: Optional[ItemDef] = None,
                 weight_g: Optional[float] = None):
        super().__init__(parent)
        self.title("Edit item" if item else "Add item")
        self.configure(bg=BG, padx=16, pady=14)
        self.resizable(False, False)
        self.result = None
        self.cfg = cfg

        start_weight = item.weight_g if item else (weight_g or 0.0)
        self.var_name = tk.StringVar(value=item.name if item else "")
        self.var_weight = tk.StringVar(value="%.1f" % start_weight)
        self.var_tol = tk.StringVar(
            value="%.1f" % (item.tolerance_g if item
                            else cfg.tolerance_for(start_weight)))

        ttk.Label(self, text="Name").grid(row=0, column=0, sticky="w", pady=4)
        entry = ttk.Entry(self, textvariable=self.var_name, width=26)
        entry.grid(row=0, column=1, pady=4)

        ttk.Label(self, text="Weight (g)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self.var_weight, width=26)\
            .grid(row=1, column=1, pady=4)

        ttk.Label(self, text="Tolerance (+/- g)")\
            .grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self.var_tol, width=26)\
            .grid(row=2, column=1, pady=4)

        ttk.Label(self, text="Tip: tolerance must be wide enough to absorb\n"
                             "sensor noise, but narrow enough not to collide\n"
                             "with another item.", foreground=MUTED)\
            .grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 10))

        btns = ttk.Frame(self)
        btns.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=self._save)\
            .pack(side="right", padx=8)

        entry.focus_set()
        self.bind("<Return>", lambda _e: self._save())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.transient(parent)
        self.grab_set()
        parent.wait_window(self)

    def _save(self) -> None:
        name = self.var_name.get().strip()
        if not name:
            messagebox.showerror("Invalid", "Enter a name.", parent=self)
            return
        try:
            weight = float(self.var_weight.get())
            tol = float(self.var_tol.get())
        except ValueError:
            messagebox.showerror("Invalid", "Weight and tolerance must be "
                                            "numbers.", parent=self)
            return
        if weight <= 0 or tol <= 0:
            messagebox.showerror("Invalid", "Weight and tolerance must be "
                                            "positive.", parent=self)
            return
        self.result = (name, weight, tol)
        self.destroy()


class CalibrationDialog(tk.Toplevel):
    """Compute and apply the HX711 calibration factor without re-flashing."""

    def __init__(self, parent: App):
        super().__init__(parent)
        self.app = parent
        self.title("Calibration wizard")
        self.configure(bg=BG, padx=18, pady=16)
        self.resizable(False, False)
        self.factor = None

        ttk.Label(self, text="Calibrate the scale",
                  font=("Segoe UI", 13, "bold")).grid(row=0, column=0,
                                                      columnspan=2, sticky="w")
        ttk.Label(self, text="1. Empty the platform, then press Tare.\n"
                             "2. Place a known weight and enter it below.\n"
                             "3. Press Read, then Apply.",
                  foreground=MUTED, justify="left")\
            .grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 14))

        ttk.Button(self, text="1. Tare (empty)", command=self._tare)\
            .grid(row=2, column=0, sticky="w", pady=4)

        ttk.Label(self, text="2. Known weight (g)")\
            .grid(row=3, column=0, sticky="w", pady=4)
        self.var_known = tk.StringVar(value="227")
        ttk.Entry(self, textvariable=self.var_known, width=14)\
            .grid(row=3, column=1, sticky="w", pady=4)

        ttk.Button(self, text="3. Read raw value", command=self._read)\
            .grid(row=4, column=0, sticky="w", pady=4)
        self.var_result = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.var_result, foreground=ACCENT)\
            .grid(row=4, column=1, sticky="w")

        self.btn_apply = ttk.Button(self, text="4. Apply to device",
                                    command=self._apply, state="disabled")
        self.btn_apply.grid(row=5, column=0, sticky="w", pady=(12, 0))
        ttk.Button(self, text="Close", command=self.destroy)\
            .grid(row=5, column=1, sticky="e", pady=(12, 0))

        self.transient(parent)
        self.grab_set()

    def _tare(self) -> None:
        self.app._tare()
        self.var_result.set("Tared.")

    def _read(self) -> None:
        try:
            known_g = float(self.var_known.get())
            if known_g <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid", "Enter the known weight in grams.",
                                 parent=self)
            return
        self.var_result.set("Reading...")
        self.app.request_raw(lambda raw: self._got_raw(raw, known_g))

    def _got_raw(self, raw: float, known_g: float) -> None:
        # HX711: units = (raw - offset) / factor, and we want kilograms,
        # so factor = raw_at_known_weight / known_kg.
        self.factor = raw / (known_g / 1000.0)
        self.var_result.set("raw %.0f  ->  factor %.1f" % (raw, self.factor))
        self.btn_apply.config(state="normal")

    def _apply(self) -> None:
        if not self.factor:
            return
        self.app.link.send("C:%.1f" % self.factor)
        self.app.engine.reset_after_tare()
        self.app._status("Calibration factor %.1f sent to the device."
                         % self.factor)
        self.destroy()


def main() -> None:
    App().mainloop()
