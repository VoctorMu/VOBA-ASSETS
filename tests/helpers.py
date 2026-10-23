import numpy as np
import pandas as pd

from deriv_swing.config import load_config


def cfg():
    return load_config()


def path(anchors: list[tuple[int, float]], noise: float = 0.0, seed: int = 1) -> np.ndarray:
    """Piecewise-linear closes through (bars_from_previous_anchor, price) anchors."""
    out = [anchors[0][1]]
    for n, p in anchors[1:]:
        out += list(np.linspace(out[-1], p, n + 1)[1:])
    arr = np.array(out, dtype=float)
    if noise:
        arr += np.random.default_rng(seed).normal(0, noise, len(arr))
    return arr


def candles(closes, wick: float = 0.3, start: int = 1_700_000_000, tf: int = 14400) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({
        "time": start + np.arange(len(closes)) * tf,
        "open": opens,
        "high": np.maximum(opens, closes) + wick,
        "low": np.minimum(opens, closes) - wick,
        "close": closes,
    })
