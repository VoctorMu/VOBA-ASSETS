"""Higher-timeframe bias (Daily / Weekly)."""
from __future__ import annotations

import pandas as pd

from .indicators import ema
from .swings import find_swings


def htf_bias(df: pd.DataFrame, ema_period: int, swing_window: int) -> int:
    """+1 bullish, -1 bearish, 0 unclear.

    Bullish needs price above the EMA and no bearish structure (lower high + lower low);
    bearish is the mirror image.
    """
    if len(df) < max(ema_period // 2, 2 * swing_window + 3):
        return 0
    close = df["close"]
    above = close.iloc[-1] > ema(close, ema_period).iloc[-1]
    swings = find_swings(df, swing_window)
    highs = [s.price for s in swings if s.kind == "H"][-2:]
    lows = [s.price for s in swings if s.kind == "L"][-2:]
    structure = 0
    if len(highs) == 2 and len(lows) == 2:
        if highs[1] > highs[0] and lows[1] > lows[0]:
            structure = 1
        elif highs[1] < highs[0] and lows[1] < lows[0]:
            structure = -1
    if above and structure >= 0:
        return 1
    if not above and structure <= 0:
        return -1
    return 0
