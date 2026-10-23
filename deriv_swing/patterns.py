"""M (double top), W (double bottom) and head & shoulders detection on swing points."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .swings import Swing

BEARISH, BULLISH = -1, 1


@dataclass
class Pattern:
    name: str                               # double_top | double_bottom | head_shoulders | inv_head_shoulders
    direction: int                          # -1 bearish, +1 bullish
    points: list[tuple[int, float]]         # the swing points that make the shape
    neckline: tuple[tuple[int, float], tuple[int, float]]
    break_idx: int                          # first candle that closed through the neckline
    stop: float                             # structural invalidation price (before buffer)

    @property
    def label(self) -> str:
        return {
            "double_top": "M (double top)",
            "double_bottom": "W (double bottom)",
            "head_shoulders": "Head & shoulders",
            "inv_head_shoulders": "Inverse head & shoulders",
        }[self.name]


def _line(p1: tuple[int, float], p2: tuple[int, float], i: int) -> float:
    (x1, y1), (x2, y2) = p1, p2
    return y1 if x2 == x1 else y1 + (y2 - y1) * (i - x1) / (x2 - x1)


def _find_break(close, extreme_series, start, direction, neckline, invalid_level, max_bars):
    """First close through the neckline after `start`, unless price invalidates the pattern first."""
    end = min(len(close), start + 1 + max_bars)
    for j in range(start + 1, end):
        if direction == BEARISH:
            if extreme_series[j] > invalid_level:
                return None
            if close[j] < _line(*neckline, j):
                return j
        else:
            if extreme_series[j] < invalid_level:
                return None
            if close[j] > _line(*neckline, j):
                return j
    return None


def find_patterns(df: pd.DataFrame, swings: list[Swing], atr: pd.Series, cfg: dict) -> list[Pattern]:
    pcfg = cfg["pattern"]
    max_break = pcfg.get("max_bars_to_break", 24)
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    atr_v = atr.to_numpy()
    out: list[Pattern] = []

    # M / W: three alternating swings with matching outer extremes.
    for a, b, c in zip(swings, swings[1:], swings[2:]):
        a_atr = atr_v[c.idx]
        if np.isnan(a_atr) or a_atr <= 0:
            continue
        if abs(a.price - c.price) > pcfg["peak_tolerance_atr"] * a_atr:
            continue
        if a.kind == "H":
            if min(a.price, c.price) - b.price < pcfg["min_height_atr"] * a_atr:
                continue
            top = max(a.price, c.price)
            neck = ((b.idx, b.price), (c.idx, b.price))
            j = _find_break(close, high, c.idx, BEARISH, neck, top, max_break)
            if j is not None:
                out.append(Pattern("double_top", BEARISH, [(a.idx, a.price), (b.idx, b.price), (c.idx, c.price)],
                                   ((b.idx, b.price), (j, b.price)), j, top))
        else:
            if b.price - max(a.price, c.price) < pcfg["min_height_atr"] * a_atr:
                continue
            bottom = min(a.price, c.price)
            neck = ((b.idx, b.price), (c.idx, b.price))
            j = _find_break(close, low, c.idx, BULLISH, neck, bottom, max_break)
            if j is not None:
                out.append(Pattern("double_bottom", BULLISH, [(a.idx, a.price), (b.idx, b.price), (c.idx, c.price)],
                                   ((b.idx, b.price), (j, b.price)), j, bottom))

    # Head & shoulders: five alternating swings, head beyond both shoulders, neckline through the two troughs.
    for ls, t1, head, t2, rs in zip(swings, swings[1:], swings[2:], swings[3:], swings[4:]):
        a_atr = atr_v[rs.idx]
        if np.isnan(a_atr) or a_atr <= 0:
            continue
        if abs(ls.price - rs.price) > pcfg["shoulder_tolerance_atr"] * a_atr:
            continue
        neck = ((t1.idx, t1.price), (t2.idx, t2.price))
        pts = [(s.idx, s.price) for s in (ls, t1, head, t2, rs)]
        use_head = pcfg.get("hs_stop", "right_shoulder") == "head"
        if head.kind == "H":
            if head.price <= max(ls.price, rs.price):
                continue
            if head.price - _line(*neck, head.idx) < pcfg["min_height_atr"] * a_atr:
                continue
            if rs.price <= _line(*neck, rs.idx):
                continue
            j = _find_break(close, high, rs.idx, BEARISH, neck, head.price, max_break)
            if j is not None:
                stop = head.price if use_head else rs.price
                out.append(Pattern("head_shoulders", BEARISH, pts, (neck[0], (j, _line(*neck, j))), j, stop))
        else:
            if head.price >= min(ls.price, rs.price):
                continue
            if _line(*neck, head.idx) - head.price < pcfg["min_height_atr"] * a_atr:
                continue
            if rs.price >= _line(*neck, rs.idx):
                continue
            j = _find_break(close, low, rs.idx, BULLISH, neck, head.price, max_break)
            if j is not None:
                stop = head.price if use_head else rs.price
                out.append(Pattern("inv_head_shoulders", BULLISH, pts, (neck[0], (j, _line(*neck, j))), j, stop))

    return sorted(out, key=lambda p: p.break_idx)
