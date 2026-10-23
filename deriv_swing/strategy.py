"""Combines the building blocks into a trade decision for the latest closed 4H candle.

Rules (all must hold):
  1. The Daily bias sets the direction. The Weekly must not point against it (htf.weekly:
     "not_against", the default) or must agree with it (htf.weekly: "agree").
  2. A recent 4H setup in that direction: M/W or (inverse) head & shoulders neckline break,
     OR regular RSI(14) divergence.
  3. A recent completed AMD (range -> sweep -> close beyond the far side) in that direction.
  4. The latest 4H close is the confirmation: it is the AMD distribution close or a pattern's
     neckline-break close. Nothing fires on a stale setup.
Stop: beyond the AMD sweep extreme (and beyond the pattern's invalidation if a pattern is used),
plus a small ATR buffer. Target: reward_risk x the stop distance.

Every signal also gets a 0-100 quality score (see score_signal). The engine trades only signals
above selection.min_score, at most selection.max_trades_per_week.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .amd import AMD, find_amd
from .divergence import Divergence, find_divergences
from .indicators import atr, rsi
from .patterns import Pattern, find_patterns
from .structure import htf_bias
from .swings import Swing, find_swings

MIN_H4_BARS = 120


@dataclass
class Signal:
    symbol: str
    direction: int
    bar_time: int
    close: float
    stop: float
    setup_id: str
    reasons: list[str]
    score: int = 0
    score_parts: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    symbol: str
    bar_time: int
    bias_d1: int
    bias_w1: int
    swings: list[Swing] = field(default_factory=list)
    patterns: list[Pattern] = field(default_factory=list)
    divergences: list[Divergence] = field(default_factory=list)
    amds: list[AMD] = field(default_factory=list)
    signal: Signal | None = None
    status: str = ""
    rsi: pd.Series | None = None
    candles: pd.DataFrame | None = None
    atr: float = 0.0


def analyse(symbol: str, h4: pd.DataFrame, d1: pd.DataFrame, w1: pd.DataFrame, cfg: dict) -> Analysis:
    scfg = cfg["strategy"]
    h4 = h4.reset_index(drop=True)
    last = len(h4) - 1
    an = Analysis(symbol, int(h4["time"].iloc[-1]) if len(h4) else 0,
                  htf_bias(d1, scfg["htf"]["ema_period"], scfg["htf"]["swing_window"]),
                  htf_bias(w1, scfg["htf"]["ema_period"], scfg["htf"]["swing_window"]))
    if len(h4) < MIN_H4_BARS:
        an.status = f"Not enough 4H history ({len(h4)} bars)"
        return an

    window = scfg["setup_window"]
    atr_s = atr(h4, scfg["atr_period"])
    rsi_s = rsi(h4["close"], scfg["rsi_period"])
    an.rsi, an.atr = rsi_s, float(atr_s.iloc[-1])
    an.swings = find_swings(h4, scfg["swing_window"])
    an.patterns = [p for p in find_patterns(h4, an.swings, atr_s, scfg) if p.break_idx >= last - window]
    an.divergences = [d for d in find_divergences(an.swings, rsi_s, scfg["swing_window"], scfg)
                      if d.confirm_idx >= last - window]
    an.amds = find_amd(h4, atr_s, scfg, window)

    direction = an.bias_d1
    if direction == 0:
        an.status = "Daily trend unclear - standing aside"
        return an
    weekly_rule = scfg["htf"].get("weekly", "not_against")
    if an.bias_w1 == -direction or (weekly_rule == "agree" and an.bias_w1 != direction):
        an.status = "Weekly trend against the Daily - standing aside"
        return an
    side = "bullish" if direction > 0 else "bearish"

    pats = [p for p in an.patterns if p.direction == direction]
    divs = [d for d in an.divergences if d.direction == direction]
    amds = [a for a in an.amds if a.direction == direction]
    if not pats and not divs:
        an.status = f"HTF {side}; waiting for a {side} M/W, H&S or RSI divergence"
        return an
    if not amds:
        an.status = f"HTF {side} + setup found; waiting for AMD to complete"
        return an

    amd = amds[0]
    pattern_trigger = any(p.break_idx == last for p in pats)
    if amd.dist_idx != last and not pattern_trigger:
        an.status = "Setup and AMD already confirmed on an earlier candle - not chasing"
        return an

    close = float(h4["close"].iloc[-1])
    buffer = scfg["stop_buffer_atr"] * an.atr
    stops = [amd.manip_extreme] + [p.stop for p in pats]
    stop = (min(stops) - buffer) if direction > 0 else (max(stops) + buffer)
    if (direction > 0 and stop >= close) or (direction < 0 and stop <= close):
        an.status = "Price already beyond the invalidation level"
        return an

    htf_txt = f"Daily + Weekly {side}" if an.bias_w1 == direction else f"Daily {side}, Weekly neutral"
    reasons = [htf_txt] + [p.label for p in pats] + [d.label for d in divs]
    reasons.append("AMD complete (range, sweep, close beyond)")
    setup_time = int(h4["time"].iloc[amd.manip_idx])
    score, parts = score_signal(an.bias_w1 == direction, pats, divs, amd, pattern_trigger and amd.dist_idx == last,
                                close, stop, an.atr, float(atr_s.iloc[amd.range_end]), scfg)
    an.signal = Signal(symbol, direction, an.bar_time, close, float(stop),
                       f"{symbol}|{direction}|{setup_time}", reasons, score, parts)
    an.status = f"SIGNAL: {'BUY' if direction > 0 else 'SELL'} (score {score})"
    return an


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_signal(weekly_agrees: bool, pats: list[Pattern], divs: list[Divergence], amd: AMD,
                 double_trigger: bool, close: float, stop: float, atr_now: float, atr_range: float,
                 scfg: dict) -> tuple[int, list[str]]:
    """0-100 quality score. Weights are a starting point: check them against your backtest's
    "score vs result" table and adjust."""
    parts: list[tuple[int, str]] = []
    parts.append((20, "Weekly agrees") if weekly_agrees else (5, "Weekly neutral"))

    setup = 0
    if pats:
        setup += 20 + (5 if len(pats) > 1 else 0)
    if divs:
        setup += 15
    labels = [p.label for p in pats] + (["RSI divergence"] if divs else [])
    parts.append((min(setup, 40), " + ".join(labels)))

    max_h = scfg["amd"]["max_range_atr"]
    height = (amd.range_high - amd.range_low) / atr_range if atr_range > 0 else max_h
    parts.append((round(15 * _clamp01((max_h - height) / max_h)), f"AMD range {height:.1f} ATR tight"))

    beyond = (close - amd.range_high) if amd.direction > 0 else (amd.range_low - close)
    if atr_now > 0 and beyond >= 0.3 * atr_now:
        parts.append((5, "decisive AMD break"))
    if double_trigger:
        parts.append((10, "pattern + AMD confirmed on the same candle"))

    stop_atr = abs(close - stop) / atr_now if atr_now > 0 else 99
    parts.append((round(10 * _clamp01((8 - stop_atr) / 5)), f"stop {stop_atr:.1f} ATR"))

    total = int(sum(p for p, _ in parts))
    return total, [f"+{p} {txt}" for p, txt in parts if p > 0]
