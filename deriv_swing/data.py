"""Candle feeds. All frames have columns time (bar open, unix seconds), open, high, low, close
and contain CLOSED candles only."""
from __future__ import annotations

import time as _time
import zlib

import numpy as np
import pandas as pd

TF_SECONDS = {"H4": 4 * 3600, "D1": 86400, "W1": 7 * 86400}


class Feed:
    def candles(self, symbol: str, tf: str, count: int) -> pd.DataFrame: ...
    def price(self, symbol: str) -> tuple[float, float]: ...   # (bid, ask)
    def now(self) -> int: ...


class MT5Feed(Feed):
    def __init__(self, mt5):
        self.mt5 = mt5
        self.tfs = {"H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1}

    def candles(self, symbol, tf, count):
        self.mt5.symbol_select(symbol, True)
        rates = self.mt5.copy_rates_from_pos(symbol, self.tfs[tf], 1, count)  # pos 0 is the forming bar
        if rates is None or len(rates) == 0:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close"])
        df = pd.DataFrame(rates)[["time", "open", "high", "low", "close"]]
        df["time"] = df["time"].astype("int64")
        return df

    def price(self, symbol):
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"no tick for {symbol}")
        return float(tick.bid), float(tick.ask)

    def now(self):
        return int(_time.time())


def _resample(path_t: np.ndarray, path_p: np.ndarray, tf_s: int, anchor: int = 0) -> pd.DataFrame:
    key = (path_t - anchor) // tf_s
    s = pd.DataFrame({"k": key, "p": path_p})
    g = s.groupby("k")["p"]
    df = pd.DataFrame({"open": g.first(), "high": g.max(), "low": g.min(), "close": g.last()})
    df["time"] = df.index.to_numpy() * tf_s + anchor
    return df.reset_index(drop=True)[["time", "open", "high", "low", "close"]]


class SyntheticFeed(Feed):
    """Deterministic regime-switching random walk with an accelerated clock, for testing anywhere.

    speed=1440 means one real second is 24 simulated minutes (a 4H candle every 10 seconds).
    """

    STEP = 900  # 15-minute price path
    WEEK_ANCHOR = 4 * 86400  # unix epoch was a Thursday; weekly bars open on Monday

    def __init__(self, symbols: list[str], speed: float = 1440, history_days: int = 1500,
                 future_days: int = 400, seed: int = 7):
        self.speed = speed
        self.real_start = _time.time()
        day0 = (int(_time.time()) // 86400) * 86400
        self.sim_start = day0
        t = np.arange(day0 - history_days * 86400, day0 + future_days * 86400, self.STEP, dtype=np.int64)
        self.paths: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.bars: dict[str, dict[str, pd.DataFrame]] = {}
        for sym in symbols:
            rng = np.random.default_rng(seed + zlib.crc32(sym.encode()))
            vol = 0.0025 * (1 + rng.random())
            regime = np.repeat(rng.normal(0, vol * 0.03, len(t) // 1500 + 1), 1500)[:len(t)]
            rets = regime + rng.standard_t(5, len(t)) * vol / 1.3
            price = 1000 * (1 + rng.random() * 50) * np.exp(np.cumsum(rets))
            self.paths[sym] = (t, price)
            self.bars[sym] = {
                "H4": _resample(t, price, TF_SECONDS["H4"]),
                "D1": _resample(t, price, TF_SECONDS["D1"]),
                "W1": _resample(t, price, TF_SECONDS["W1"], self.WEEK_ANCHOR),
            }

    def now(self):
        return int(self.sim_start + (_time.time() - self.real_start) * self.speed)

    def candles(self, symbol, tf, count):
        df = self.bars[symbol][tf]
        closed = df[df["time"] + TF_SECONDS[tf] <= self.now()]
        return closed.tail(count).reset_index(drop=True)

    def price(self, symbol):
        t, p = self.paths[symbol]
        i = max(0, min(len(t) - 1, int(np.searchsorted(t, self.now(), side="right")) - 1))
        mid = float(p[i])
        spread = mid * 0.00005
        return mid - spread, mid + spread
