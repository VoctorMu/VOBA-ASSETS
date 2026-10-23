"""Position sizing and the daily loss cap."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SymbolSpec:
    tick_size: float
    tick_value: float      # account currency per tick per 1.0 lot
    volume_min: float
    volume_step: float
    volume_max: float
    min_stop_distance: float = 0.0


def money_at_risk(spec: SymbolSpec, entry: float, stop: float, volume: float) -> float:
    return abs(entry - stop) / spec.tick_size * spec.tick_value * volume


def position_size(equity: float, risk_pct: float, entry: float, stop: float,
                  spec: SymbolSpec) -> tuple[float, str]:
    """Largest volume whose stop-out loses no more than risk_pct of equity. Returns (volume, reason)."""
    distance = abs(entry - stop)
    if distance <= 0:
        return 0.0, "stop distance is zero"
    if distance < spec.min_stop_distance:
        return 0.0, "stop is closer than the broker allows"
    budget = equity * risk_pct / 100
    per_lot = distance / spec.tick_size * spec.tick_value
    raw = budget / per_lot
    steps = math.floor(raw / spec.volume_step + 1e-9)
    volume = round(steps * spec.volume_step, 8)
    volume = min(volume, spec.volume_max)
    if volume < spec.volume_min:
        return 0.0, (f"minimum lot {spec.volume_min} would risk "
                     f"{spec.volume_min * per_lot / equity * 100:.2f}% (> {risk_pct}%)")
    return volume, ""


class DailyGuard:
    """Blocks new trades if a stop-out of everything open plus the new trade would breach the cap."""

    def __init__(self, cap_pct: float, state: dict | None = None):
        self.cap_pct = cap_pct
        state = state or {}
        self.day: str | None = state.get("day")
        self.start_equity: float | None = state.get("start_equity")

    def roll(self, day: str, equity: float) -> None:
        if day != self.day or self.start_equity is None:
            self.day, self.start_equity = day, equity

    def loss_pct(self, equity: float) -> float:
        if not self.start_equity:
            return 0.0
        return max(0.0, (self.start_equity - equity) / self.start_equity * 100)

    def allows(self, equity: float, open_risk: float, new_risk: float) -> tuple[bool, str]:
        if self.start_equity is None:
            return False, "daily guard not initialised"
        cap = self.start_equity * self.cap_pct / 100
        lost = max(0.0, self.start_equity - equity)
        if lost >= cap:
            return False, f"daily loss cap hit (-{self.loss_pct(equity):.2f}%)"
        if lost + open_risk + new_risk > cap + 1e-9:
            worst = (lost + open_risk + new_risk) / self.start_equity * 100
            return False, f"worst case today would be -{worst:.2f}% (cap -{self.cap_pct}%)"
        return True, ""

    def state(self) -> dict:
        return {"day": self.day, "start_equity": self.start_equity}
