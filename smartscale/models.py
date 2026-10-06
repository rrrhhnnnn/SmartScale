"""Plain data objects shared by the engine, storage and UI."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ItemDef:
    """One entry in the item library."""
    id: int
    name: str
    weight_g: float
    tolerance_g: float = 2.0

    @property
    def low(self) -> float:
        return self.weight_g - self.tolerance_g

    @property
    def high(self) -> float:
        return self.weight_g + self.tolerance_g

    def matches(self, grams: float) -> bool:
        return self.low <= grams <= self.high

    def overlaps(self, other: "ItemDef") -> bool:
        """True if two items have colliding weight windows (unresolvable pair)."""
        return self.low <= other.high and other.low <= self.high


@dataclass
class BasketEntry:
    """A recognised item currently sitting on the platform."""
    item: ItemDef
    count: int = 0

    @property
    def total_g(self) -> float:
        return self.item.weight_g * self.count


@dataclass
class ScaleEvent:
    """One recognised add/remove, as written to history, CSV and the cloud."""
    kind: str                       # ADD | REMOVE | CLEAR | UNKNOWN_ADD | UNKNOWN_REMOVE | TARE
    delta_g: float
    total_g: float
    quantity: int
    distinct: int
    status: str = "OK"              # OK | AMBIGUOUS | MULTI xN | UNKNOWN | TAUGHT
    item_name: Optional[str] = None
    item_id: Optional[int] = None
    count: int = 1
    ts: float = field(default_factory=time.time)

    @property
    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.ts))

    @property
    def iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts))

    def describe(self) -> str:
        name = self.item_name or f"Unknown ({abs(self.delta_g):.1f} g)"
        if self.item_name and self.count > 1:
            name = f"{name} x{self.count}"
        sign = "+" if self.delta_g >= 0 else "-"
        return f"{self.kind} {name} {sign}{abs(self.delta_g):.1f} g"
