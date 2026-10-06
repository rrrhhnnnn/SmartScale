"""Engine tests - run with:  python tests/test_engine.py

No pytest required. These exercise the exact scenarios from the project brief.
"""
import os
import random
import sys

import tempfile

# Isolate from the operator's real database and settings - ALWAYS.
_tmp = tempfile.mkdtemp(prefix="smartscale_test_")
os.environ["SMARTSCALE_DB"] = os.path.join(_tmp, "test.db")
os.environ["SMARTSCALE_CONFIG"] = os.path.join(_tmp, "test_config.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smartscale.config import Config
from smartscale.engine import RecognitionEngine, StabilityDetector
from smartscale.models import ItemDef

random.seed(7)

PENCIL = ItemDef(1, "Pencil", 60.0, 3.0)
ERASER = ItemDef(2, "Eraser", 18.0, 2.0)
BOOK = ItemDef(3, "Book", 240.0, 5.0)
LIBRARY = [PENCIL, ERASER, BOOK]


def make_engine(items=None):
    cfg = Config()
    items = items if items is not None else LIBRARY
    return RecognitionEngine(cfg, lambda: list(items))


def settle(engine, total_g, noise=0.4, plateau=12):
    """Simulate a hand placing something: an unstable ramp, then a plateau."""
    events = []
    for i in range(4):                      # ramp - must NOT produce events
        step = engine.on_sample(total_g * (i + 1) / 4.0 + random.uniform(-8, 8))
        events.extend(step)
    for _ in range(plateau):                # plateau - fires once
        events.extend(engine.on_sample(total_g + random.uniform(-noise, noise)))
    return events


# --------------------------------------------------------------------- tests
def test_stability_detector_fires_once():
    det = StabilityDetector(window=6, band_g=3.0)
    fires = [det.push(100.0 + random.uniform(-0.5, 0.5)) for _ in range(20)]
    assert sum(1 for f in fires if f is not None) == 1, "should fire exactly once"
    print("PASS  stability detector fires exactly once per settle")


def test_stability_blocks_moving_weight():
    det = StabilityDetector(window=6, band_g=3.0)
    for _ in range(20):
        assert det.push(random.uniform(0, 300)) is None
    assert not det.is_stable
    print("PASS  a moving weight never settles")


def test_brief_scenario_two_pencils_eraser_book():
    """The exact example from the brief: expect Quantity 4, Items 3."""
    eng = make_engine()
    settle(eng, 60.0)                       # pencil 1
    settle(eng, 120.0)                      # pencil 2
    settle(eng, 138.0)                      # eraser
    settle(eng, 378.0)                      # book

    names = {e.item.name: e.count for e in eng.entries()}
    assert eng.quantity == 4, "quantity was %d" % eng.quantity
    assert eng.distinct == 3, "distinct was %d" % eng.distinct
    assert names == {"Pencil": 2, "Eraser": 1, "Book": 1}, names
    print("PASS  2 pencils + 1 eraser + 1 book -> Quantity 4, Items 3, %s" % names)


def test_drift_never_changes_the_item():
    """200 g creeping to 202 g must keep showing the same item."""
    item = ItemDef(9, "Widget", 200.0, 2.0)
    eng = make_engine([item])
    settle(eng, 200.0)
    assert eng.quantity == 1 and eng.entries()[0].item.name == "Widget"

    before = eng._snapshot()
    for drift in (200.5, 201.0, 201.4, 201.8, 202.0, 202.2, 201.6, 202.0):
        for _ in range(8):
            evs = eng.on_sample(drift + random.uniform(-0.3, 0.3))
            assert not evs, "drift produced a spurious event: %s" % evs
    assert eng._snapshot() == before
    assert eng.quantity == 1 and eng.distinct == 1
    print("PASS  200 g -> 202 g drift produces no events and no item change")


def test_removal():
    eng = make_engine()
    settle(eng, 240.0)                      # book on
    settle(eng, 258.0)                      # eraser on
    assert eng.quantity == 2
    settle(eng, 240.0)                      # eraser off
    names = {e.item.name: e.count for e in eng.entries()}
    assert names == {"Book": 1}, names
    assert eng.quantity == 1 and eng.distinct == 1
    print("PASS  removing an item updates the basket correctly")


def test_clear_platform():
    eng = make_engine()
    settle(eng, 240.0)
    settle(eng, 300.0)
    settle(eng, 0.0)                        # everything lifted off
    assert eng.quantity == 0 and eng.distinct == 0
    assert eng.history[-1].kind == "CLEAR", eng.history[-1].kind
    print("PASS  lifting everything off clears the basket in one CLEAR event")


def test_multiple_identical_items_at_once():
    eng = make_engine()
    evs = settle(eng, 120.0)                # two pencils placed together
    assert eng.quantity == 2, eng.quantity
    assert eng.entries()[0].item.name == "Pencil"
    assert "MULTI" in evs[-1].status, evs[-1].status
    print("PASS  two identical items placed together -> MULTI x2")


def test_unknown_item_is_flagged_for_teaching():
    eng = make_engine()
    settle(eng, 512.0)                      # nothing in the library matches
    assert eng.history[-1].kind == "UNKNOWN_ADD"
    assert eng.pending_unknown_g is not None
    taught = ItemDef(4, "Laptop", round(eng.pending_unknown_g, 1), 6.0)
    eng.resolve_pending(taught)
    assert eng.quantity == 1 and eng.entries()[0].item.name == "Laptop"
    print("PASS  unknown weight is flagged, then resolved by teaching")


def test_ambiguity_is_reported():
    a = ItemDef(1, "Bolt A", 50.0, 3.0)
    b = ItemDef(2, "Bolt B", 52.0, 3.0)     # overlapping windows
    assert a.overlaps(b)
    eng = make_engine([a, b])
    evs = settle(eng, 51.0)
    assert evs[-1].status == "AMBIGUOUS", evs[-1].status
    print("PASS  overlapping items are accepted but flagged AMBIGUOUS")


def test_undo():
    eng = make_engine()
    settle(eng, 240.0)
    settle(eng, 300.0)
    assert eng.quantity == 2
    eng.undo()
    assert eng.quantity == 1, eng.quantity
    assert eng.entries()[0].item.name == "Book"
    print("PASS  undo reverts the last basket change")


def test_tare_resets_everything():
    eng = make_engine()
    settle(eng, 240.0)
    eng.reset_after_tare()
    assert eng.quantity == 0 and eng.baseline_g == 0.0
    print("PASS  tare resets baseline and basket together")


# ---------------------------------------------------------- firmware compat
def test_original_sketch_output_is_parsed():
    """The user's own sketch prints 'Measurement N: Weight: X kg [hh:mm:ss]'."""
    from smartscale.link import BaseLink
    link = BaseLink()
    link._emit("Measurement 1: Weight: 0.227 kg  [00:00:03]")
    link._emit("")                                   # the blank separator line
    link._emit("Measurement 2: Weight: 0.000 kg  [00:00:13]")
    link._emit("W,7,512.4,98765")                    # v2 still works too
    msgs = []
    while not link.inbox.empty():
        msgs.append(link.inbox.get_nowait())
    assert msgs[0] == ("PROTOCOL", "legacy"), msgs
    assert msgs[1] == ("W", 227.0), msgs
    assert msgs[2] == ("W", 0.0), msgs
    assert ("PROTOCOL", "v2") in msgs and ("W", 512.4) in msgs
    print("PASS  original sketch output parsed (0.227 kg -> 227.0 g), v2 too")


def test_software_tare_for_firmware_without_T():
    eng = make_engine()
    settle(eng, 240.0)                      # something already on the platform
    assert eng.quantity == 1
    eng.software_tare()                     # operator presses Tare
    assert eng.quantity == 0 and abs(eng.last_g) < 1e-6
    for _ in range(8):
        eng.on_sample(240.0)                # sensor still reads 240...
    assert abs(eng.last_g) < 1.0            # ...but the app shows ~0 (noise-level)
    settle(eng, 258.0)                      # +18 g -> eraser
    assert eng.entries()[0].item.name == "Eraser" and eng.quantity == 1
    print("PASS  software tare zeroes the reading and recognition continues")


def test_display_zero_band():
    """-1 g < reading < +1 g must be shown as 0.000 kg; the engine keeps the truth."""
    from smartscale.config import Config
    cfg = Config()
    assert cfg.zero_display_g == 1.0

    def shown(g):
        return 0.0 if abs(g) < cfg.zero_display_g else g

    assert shown(0.4) == 0.0 and shown(-0.9) == 0.0 and shown(0.99) == 0.0
    assert shown(1.0) == 1.0 and shown(-1.0) == -1.0 and shown(2.3) == 2.3
    assert "%.3f kg" % (shown(-0.7) / 1000.0) == "0.000 kg"      # no "-0.000"
    print("PASS  display zero band: |reading| < 1 g shows as 0.000 kg")


def test_never_below_zero():
    """With allow_negative=False (default) the engine never reads under 0 g."""
    eng = make_engine()
    assert eng.cfg.allow_negative is False
    for g in (-0.4, -3.0, -120.0):
        eng.on_sample(g)
        assert eng.last_g == 0.0, (g, eng.last_g)
    # software tare on a noisy zero, then readings that dip below the tare point
    settle(eng, 5.0)                        # under min_event_g -> no item, just baseline
    eng.software_tare()
    for g in (4.6, 4.2, 3.9, 4.8):          # sensor sits slightly under the tare point
        eng.on_sample(g)
        assert eng.last_g == 0.0
    # and recognition still works on top of it
    settle(eng, 245.0)                      # +240 -> Book
    assert eng.entries()[0].item.name == "Book"
    print("PASS  readings never go below 0 g; recognition unaffected")


def test_allow_negative_can_be_turned_back_on():
    eng = make_engine()
    eng.cfg.allow_negative = True
    eng.on_sample(-3.0)
    assert eng.last_g == -3.0
    print("PASS  allow_negative=True restores signed readings")


# ------------------------------------------------------------ bulk removal
PLATE = ItemDef(10, "plate", 150.0, 3.0)
GEAR = ItemDef(11, "Gear", 300.0, 6.0)


def test_bulk_remove_two_of_three_plates():
    """The reported bug: 3 plates on together, 2 lifted off together."""
    eng = make_engine([PLATE, GEAR])
    settle(eng, 450.0)                      # 3 plates at once -> MULTI x3
    assert eng.quantity == 3 and eng.distinct == 1
    evs = settle(eng, 150.0)                # 2 come off together (-300 g)
    assert eng.quantity == 1, "quantity should drop to 1, got %d" % eng.quantity
    assert eng.distinct == 1
    assert eng.entries()[0].item.name == "plate" and eng.entries()[0].count == 1
    assert evs[-1].kind == "REMOVE" and evs[-1].count == 2, evs[-1]
    print("PASS  3 plates on, 2 off together -> Quantity 1, Items 1 (REMOVE plate x2)")


def test_bulk_remove_mixed_items():
    """1 plate + 1 gear lifted together resolve to both entries."""
    eng = make_engine([PLATE, GEAR])
    settle(eng, 150.0)                      # plate
    settle(eng, 300.0)                      # plate
    settle(eng, 600.0)                      # gear
    assert eng.quantity == 3 and eng.distinct == 2
    evs = settle(eng, 150.0)                # -450 g = plate + gear
    names = {e.item.name: e.count for e in eng.entries()}
    assert names == {"plate": 1}, names
    assert eng.quantity == 1 and eng.distinct == 1
    kinds = sorted((e.item_name, e.count, e.status) for e in evs)
    assert kinds == [("Gear", 1, "BULK"), ("plate", 1, "BULK")], kinds
    print("PASS  plate + gear lifted together -> both removed (BULK)")


def test_bulk_remove_ambiguity_is_flagged():
    """-300 g could be 2 plates (150 x2) or 1 gear (300): both fit, so flag it."""
    eng = make_engine([PLATE, GEAR])
    settle(eng, 150.0)                      # plate
    settle(eng, 300.0)                      # plate
    settle(eng, 600.0)                      # gear   -> basket: plate x2, gear x1
    evs = settle(eng, 300.0)                # -300 g: 2 plates? or the gear?
    assert all(e.status == "AMBIGUOUS" for e in evs), [e.status for e in evs]
    assert eng.quantity == 2                # something was removed (fewest pieces wins)
    print("PASS  ambiguous bulk removal is executed but flagged AMBIGUOUS")


def test_undo_reverts_whole_bulk_removal():
    eng = make_engine([PLATE, GEAR])
    settle(eng, 150.0); settle(eng, 300.0); settle(eng, 600.0)
    settle(eng, 150.0)                      # plate + gear off
    assert eng.quantity == 1
    undone = eng.undo()
    assert len(undone) == 2 and eng.quantity == 3 and eng.distinct == 2
    print("PASS  one Undo restores both pieces of a bulk removal")


def test_weight_reads_zero_when_basket_empty():
    """Quantity 0 and Items 0 -> display 0.000 kg even with a few g of residual."""
    eng = make_engine([PLATE, GEAR])
    settle(eng, 450.0)                      # 3 plates
    settle(eng, 6.0)                        # all off, but 6 g of drift remains
    assert eng.quantity == 0 and eng.distinct == 0
    band = eng.cfg.zero_display_g            # what the display rounds to 0.000
    assert eng.baseline_g == 0.0 and abs(eng.last_g) < band, (eng.last_g, eng.baseline_g)
    for _ in range(6):
        eng.on_sample(6.0)                  # sensor still says 6 g...
    assert abs(eng.last_g) < band           # ...display says 0.000
    settle(eng, 156.0)                      # next plate placed on the same residual
    assert eng.entries()[0].item.name == "plate" and eng.quantity == 1
    print("PASS  empty basket re-zeroes residual drift; next item still recognised")


def test_large_residual_is_not_hidden():
    """40 g left on the platform with 0 items is something unrecognised - show it."""
    eng = make_engine([PLATE, GEAR])
    settle(eng, 150.0)
    settle(eng, 40.0)                       # -110 g: not a plate -> UNKNOWN_REMOVE
    assert eng.history[-1].kind == "UNKNOWN_REMOVE"
    assert eng.quantity == 1                # basket untouched, so no re-zero
    print("PASS  a large residual is left visible, not silently zeroed")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print("FAIL  %s: %s" % (fn.__name__, exc))
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
