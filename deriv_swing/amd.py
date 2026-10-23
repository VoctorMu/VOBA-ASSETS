"""AMD (Accumulation -> Manipulation -> Distribution), a.k.a. "power of three".

Like a spring: price coils in a tight range (accumulation), is shoved the wrong way to grab the
stops beyond one edge (manipulation, a wick that closes back inside), then releases through the
opposite edge (distribution, a candle *closing* beyond the range).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class AMD:
    direction: int          # +1 sweep of lows then break up, -1 sweep of highs then break down
    range_start: int
    range_end: int
    range_high: float
    range_low: float
    manip_idx: int
    manip_extreme: float    # the sweep's wick extreme (stop goes beyond it)
    dist_idx: int           # candle that closed beyond the opposite side


def _amd_ending_at(d, o, h, lo, c, atr_v, cfg) -> AMD | None:
    acfg = cfg["amd"]
    for m in range(d - 1, max(d - 1 - acfg["max_bars_to_break"], acfg["min_range_bars"]) - 1, -1):
        a = atr_v[m - 1]
        if np.isnan(a) or a <= 0:
            continue
        # Tight ranges that end right before the sweep candle, longest first. The longest one
        # can reach back below (or above) the sweep, so shorter ones get a turn too.
        for length in range(acfg["max_range_bars"], acfg["min_range_bars"] - 1, -1):
            start = m - length
            if start < 0:
                continue
            rh, rl = h[start:m].max(), lo[start:m].min()
            if rh - rl > acfg["max_range_atr"] * a:
                continue
            between = c[m + 1:d]
            held = bool(np.all((between >= rl) & (between <= rh)))
            if not held:
                continue
            if lo[m] < rl and rl <= c[m] <= rh and c[d] > rh:
                return AMD(1, start, m - 1, float(rh), float(rl), m, float(lo[m:d + 1].min()), d)
            if h[m] > rh and rl <= c[m] <= rh and c[d] < rl:
                return AMD(-1, start, m - 1, float(rh), float(rl), m, float(h[m:d + 1].max()), d)
    return None


def find_amd(df: pd.DataFrame, atr: pd.Series, cfg: dict, recent: int) -> list[AMD]:
    """AMD sequences whose distribution candle is among the last `recent` closed candles, newest first."""
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    atr_v = atr.to_numpy()
    last = len(df) - 1
    out: list[AMD] = []
    for d in range(last, max(last - recent, 0) - 1, -1):
        found = _amd_ending_at(d, o, h, lo, c, atr_v, cfg)
        if found and all(found.manip_idx != x.manip_idx for x in out):
            out.append(found)
    return out
