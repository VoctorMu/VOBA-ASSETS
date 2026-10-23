"""Swing (pivot) detection. Every pattern in the app is built from these points."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Swing:
    idx: int      # positional index into the candle frame
    price: float
    kind: str     # "H" swing high, "L" swing low


def find_swings(df: pd.DataFrame, window: int) -> list[Swing]:
    """Fractal pivots confirmed by `window` bars on each side, reduced to an alternating H/L sequence.

    A pivot at bar i is only known once bar i+window has closed, so this never looks ahead
    when called on candles up to "now".
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    raw: list[Swing] = []
    for i in range(window, len(df) - window):
        h = highs[i]
        if h >= highs[i - window:i].max() and h > highs[i + 1:i + window + 1].max():
            raw.append(Swing(i, float(h), "H"))
        lo = lows[i]
        if lo <= lows[i - window:i].min() and lo < lows[i + 1:i + window + 1].min():
            raw.append(Swing(i, float(lo), "L"))

    swings: list[Swing] = []
    for s in raw:
        if swings and swings[-1].kind == s.kind:
            prev = swings[-1]
            if (s.kind == "H" and s.price > prev.price) or (s.kind == "L" and s.price < prev.price):
                swings[-1] = s
        else:
            swings.append(s)
    return swings
