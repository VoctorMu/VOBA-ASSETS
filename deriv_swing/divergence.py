"""Regular RSI divergence between consecutive swing highs / lows."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .swings import Swing


@dataclass
class Divergence:
    direction: int                    # +1 bullish (lower low, higher RSI), -1 bearish
    p1: tuple[int, float, float]      # (idx, price, rsi)
    p2: tuple[int, float, float]
    confirm_idx: int                  # bar on which the second swing became known

    @property
    def label(self) -> str:
        return "Bullish RSI divergence" if self.direction > 0 else "Bearish RSI divergence"


def find_divergences(swings: list[Swing], rsi: pd.Series, swing_window: int, cfg: dict) -> list[Divergence]:
    dcfg = cfg["divergence"]
    r = rsi.to_numpy()
    out: list[Divergence] = []
    for kind in ("H", "L"):
        pts = [s for s in swings if s.kind == kind]
        for a, b in zip(pts, pts[1:]):
            gap = b.idx - a.idx
            if not dcfg["min_bars"] <= gap <= dcfg["max_bars"]:
                continue
            ra, rb = r[a.idx], r[b.idx]
            if np.isnan(ra) or np.isnan(rb):
                continue
            if kind == "L" and b.price < a.price and rb > ra:
                direction = 1
            elif kind == "H" and b.price > a.price and rb < ra:
                direction = -1
            else:
                continue
            out.append(Divergence(direction, (a.idx, a.price, float(ra)), (b.idx, b.price, float(rb)),
                                  b.idx + swing_window))
    return sorted(out, key=lambda d: d.confirm_idx)
