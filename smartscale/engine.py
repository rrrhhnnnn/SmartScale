"""
Item-recognition engine.

A single load cell reports ONE number: total weight. A total cannot be
uniquely decomposed into a set of items (that is subset-sum, and it is
ambiguous). So this engine never decomposes the total. Instead it watches the
CHANGE in weight each time something is placed or removed, matches that delta
against the item library, and keeps the basket as latched state.

Four layers:
  1. Stability gate  - nothing is recorded while the weight is moving.
  2. Dead-band       - changes under `min_event_g` are drift, not items.
  3. Latching        - the basket is state; it is never recomputed from the
                       current weight, so a reading drifting 200 -> 202 g can
                       never change the item shown.
  4. Tolerance match - |delta - item.weight| <= item.tolerance.
"""
from __future__ import annotations

import statistics
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

from .models import BasketEntry, ItemDef, ScaleEvent


class StabilityDetector:
    """Layer 1. Fires once, with a median value, on each unstable->stable edge."""

    def __init__(self, window: int = 6, band_g: float = 3.0):
        self.window = max(3, int(window))
        self.band_g = float(band_g)
        self._buf: deque = deque(maxlen=self.window)
        self.is_stable = False

    def push(self, grams: float) -> Optional[float]:
        """Return the settled weight on the rising edge of stability, else None."""
        self._buf.append(grams)
        if len(self._buf) < self.window:
            return None

        now_stable = (max(self._buf) - min(self._buf)) <= self.band_g
        fired = now_stable and not self.is_stable
        self.is_stable = now_stable
        # median, not mean: a single spike cannot drag the settled value
        return statistics.median(self._buf) if fired else None

    def reset(self) -> None:
        self._buf.clear()
        self.is_stable = False


class RecognitionEngine:
    def __init__(self, cfg, items_provider: Callable[[], List[ItemDef]]):
        self.cfg = cfg
        self._items = items_provider
        self.stab = StabilityDetector(cfg.stability_window, cfg.stability_band_g)

        self.baseline_g: float = 0.0          # last settled weight
        self.last_g: float = 0.0              # most recent sample (tare-corrected)
        self.tare_offset_g: float = 0.0       # software zero, for firmware with no T command
        self.basket: Dict[int, BasketEntry] = {}
        self.history: List[ScaleEvent] = []
        self.pending_unknown_g: Optional[float] = None
        self._undo: List[Tuple[Dict[int, int], List[ScaleEvent]]] = []

    # ------------------------------------------------------------------ state
    @property
    def quantity(self) -> int:
        """Total number of things on the platform (2 pencils + 1 book = 3)."""
        return sum(e.count for e in self.basket.values())

    @property
    def distinct(self) -> int:
        """How many different item types are present."""
        return len(self.basket)

    @property
    def expected_g(self) -> float:
        return sum(e.total_g for e in self.basket.values())

    @property
    def drift_g(self) -> float:
        """Measured minus expected - accumulated error, useful as a health check."""
        return self.baseline_g - self.expected_g

    def entries(self) -> List[BasketEntry]:
        return sorted(self.basket.values(), key=lambda e: e.item.name.lower())

    @property
    def is_stable(self) -> bool:
        return self.stab.is_stable

    # ------------------------------------------------------------------- feed
    def on_sample(self, grams: float) -> List[ScaleEvent]:
        """Feed one live reading. Returns any events it produced (usually none)."""
        grams = grams - self.tare_offset_g
        if not getattr(self.cfg, "allow_negative", False) and grams < 0:
            grams = 0.0                                 # never below zero
        self.last_g = grams

        settled = self.stab.push(grams)
        if settled is None:
            return []                                   # layer 1: still moving

        delta = settled - self.baseline_g
        if abs(delta) < self.cfg.min_event_g:           # layer 2: drift
            self.baseline_g = settled                   # absorb it silently
            self._rezero_if_empty()                     # empty platform reads 0.000
            return []

        self.baseline_g = settled
        if delta > 0:
            return self._on_add(delta, settled)
        return self._on_remove(-delta, settled)

    # -------------------------------------------------------------- add / rem
    def _on_add(self, delta: float, total: float) -> List[ScaleEvent]:
        hits = sorted((i for i in self._items() if i.matches(delta)),
                      key=lambda i: abs(delta - i.weight_g))
        if hits:
            status = "AMBIGUOUS" if len(hits) > 1 else "OK"
            return [self._commit_add(hits[0], 1, delta, total, status)]

        multi = self._match_multiple(delta)
        if multi:
            item, k = multi
            return [self._commit_add(item, k, delta, total, "MULTI x%d" % k)]

        self.pending_unknown_g = delta                  # offer to teach it
        return [self._log("UNKNOWN_ADD", None, delta, total, "UNKNOWN")]

    def _match_multiple(self, delta: float) -> Optional[Tuple[ItemDef, int]]:
        """Two pencils dropped together still resolve to 2 x Pencil."""
        best = None
        for item in self._items():
            if item.weight_g <= 0:
                continue
            k = round(delta / item.weight_g)
            if 2 <= k <= self.cfg.max_multiple and \
                    abs(delta - k * item.weight_g) <= k * item.tolerance_g:
                err = abs(delta - k * item.weight_g)
                if best is None or err < best[0]:
                    best = (err, item, k)
        return (best[1], best[2]) if best else None

    def _on_remove(self, delta: float, total: float) -> List[ScaleEvent]:
        # Everything lifted off at once -> clear the basket rather than
        # reporting one giant unknown removal.
        clear_band = max(self.cfg.zero_track_g * 2, self.cfg.min_event_g)
        if self.basket and abs(total) <= clear_band:
            snapshot = self._snapshot()
            removed = self.quantity
            self.basket.clear()
            ev = self._log("CLEAR", None, -delta, total, "OK", count=removed)
            self._undo.append((snapshot, [ev]))
            self._rezero_if_empty()
            return [ev]

        # Bulk removal: find the combination of things ACTUALLY IN THE BASKET
        # whose weight matches what just came off. "2 of the 3 plates", or
        # "1 plate + 1 gear", are both found here.
        picks, ambiguous = self._find_removal(delta)
        if not picks:
            return [self._log("UNKNOWN_REMOVE", None, -delta, total, "UNKNOWN")]

        snapshot = self._snapshot()
        events: List[ScaleEvent] = []
        n_entries = len(picks)
        for item_id, count in picks.items():
            entry = self.basket[item_id]
            entry.count -= count
            if entry.count <= 0:
                del self.basket[entry.item.id]
            if ambiguous:
                status = "AMBIGUOUS"
            elif n_entries > 1:
                status = "BULK"
            elif count > 1:
                status = "MULTI x%d" % count
            else:
                status = "OK"
            share = entry.item.weight_g * count      # this entry's part of the delta
            events.append(self._log("REMOVE", entry.item, -share, total,
                                    status, count=count))
        # one undo step reverts the whole bulk removal
        self._undo.append((snapshot, events))
        self._rezero_if_empty()
        return events

    MAX_COMBOS = 20000

    def _find_removal(self, delta: float) -> Tuple[Dict[int, int], bool]:
        """Which sub-multiset of the basket weighs `delta`?

        Returns ({item_id: count}, ambiguous). Tolerance accumulates per piece
        (three plates can each be a little off). Best = smallest error, then
        fewest pieces. Ambiguous = another different combination also fits.
        """
        entries = [e for e in self.basket.values() if e.count > 0]
        if not entries:
            return {}, False

        combos = 1
        for e in entries:
            combos *= (e.count + 1)

        candidates: List[Tuple[float, int, Dict[int, int]]] = []
        if combos <= self.MAX_COMBOS:
            import itertools
            for choice in itertools.product(*[range(e.count + 1) for e in entries]):
                n = sum(choice)
                if n == 0:
                    continue
                weight = sum(r * e.item.weight_g for r, e in zip(choice, entries))
                tol = sum(r * e.item.tolerance_g for r, e in zip(choice, entries))
                err = abs(delta - weight)
                if err <= tol:
                    candidates.append((err, n, {e.item.id: r for r, e
                                                in zip(choice, entries) if r}))
        else:
            # Huge basket: fall back to "k of one kind" only.
            for e in entries:
                for k in range(1, e.count + 1):
                    err = abs(delta - k * e.item.weight_g)
                    if err <= k * e.item.tolerance_g:
                        candidates.append((err, k, {e.item.id: k}))

        if not candidates:
            return {}, False
        candidates.sort(key=lambda c: (c[0], c[1]))
        best = candidates[0][2]
        ambiguous = any(c[2] != best for c in candidates[1:])
        return best, ambiguous

    def _rezero_if_empty(self) -> bool:
        """Nothing recognised on the platform -> whatever residual the sensor
        still reads (drift, a few grams) becomes the new zero, so the display
        shows 0.000 kg whenever Quantity and Items are 0."""
        if self.basket or self.pending_unknown_g is not None:
            return False
        band = getattr(self.cfg, "empty_zero_g", 10.0)
        if self.baseline_g == 0.0 or abs(self.baseline_g) > band:
            return False
        self.tare_offset_g += self.baseline_g
        self.baseline_g = 0.0
        self.last_g = 0.0
        return True

    def _commit_add(self, item: ItemDef, count: int, delta: float,
                    total: float, status: str) -> ScaleEvent:
        snapshot = self._snapshot()
        entry = self.basket.get(item.id)
        if entry is None:
            entry = BasketEntry(item=item, count=0)
            self.basket[item.id] = entry
        entry.count += count
        ev = self._log("ADD", item, delta, total, status, count=count)
        self._undo.append((snapshot, [ev]))
        return ev

    # ------------------------------------------------------------- operations
    def resolve_pending(self, item: ItemDef) -> Optional[ScaleEvent]:
        """Called after the user teaches a name for the last unknown weight."""
        if self.pending_unknown_g is None:
            return None
        delta = self.pending_unknown_g
        self.pending_unknown_g = None
        return self._commit_add(item, 1, delta, self.baseline_g, "TAUGHT")

    def undo(self) -> List[ScaleEvent]:
        """Revert the last basket change (a bulk removal is ONE step). The
        baseline is deliberately kept: the physical weight did not change,
        only our interpretation of it. Returns the events that were undone."""
        if not self._undo:
            return []
        snapshot, events = self._undo.pop()
        by_id = {i.id: i for i in self._items()}
        self.basket = {
            iid: BasketEntry(item=by_id[iid], count=cnt)
            for iid, cnt in snapshot.items() if iid in by_id and cnt > 0
        }
        for ev in events:
            if ev in self.history:
                self.history.remove(ev)
        return events

    def reset_after_tare(self, hardware: bool = True) -> None:
        """The scale just zeroed, so our baseline and basket must zero too.

        hardware=True means the firmware itself re-zeroed (T command), so any
        software offset is now wrong and is dropped.
        """
        if hardware:
            self.tare_offset_g = 0.0
        self.baseline_g = 0.0
        self.last_g = 0.0                     # the scale reads zero right now
        self.basket.clear()
        self.pending_unknown_g = None
        self._undo.clear()
        self.stab.reset()

    def software_tare(self) -> None:
        """Zero the scale in software. Works with ANY firmware, including the
        original sketch that has no tare command: whatever is being read right
        now becomes the new zero."""
        self.tare_offset_g += self.last_g
        self.reset_after_tare(hardware=False)

    def clear_session(self) -> None:
        self.reset_after_tare()
        self.history.clear()

    def apply_config(self) -> None:
        """Re-read tuning values after the user edits them in Settings."""
        self.set_window(int(self.cfg.stability_window))
        self.stab.band_g = float(self.cfg.stability_band_g)

    def set_window(self, samples: int) -> None:
        """Change the stability window without losing buffered samples."""
        self.stab.window = max(3, int(samples))
        self.stab._buf = deque(self.stab._buf, maxlen=self.stab.window)

    # ----------------------------------------------------------------- helpers
    def _snapshot(self) -> Dict[int, int]:
        return {iid: e.count for iid, e in self.basket.items()}

    def _log(self, kind: str, item: Optional[ItemDef], delta: float,
             total: float, status: str, count: int = 1) -> ScaleEvent:
        ev = ScaleEvent(
            kind=kind,
            item_name=item.name if item else None,
            item_id=item.id if item else None,
            delta_g=round(delta, 1),
            total_g=round(total, 1),
            count=count,
            quantity=self.quantity,      # computed AFTER the basket mutation
            distinct=self.distinct,
            status=status,
        )
        self.history.append(ev)
        return ev
