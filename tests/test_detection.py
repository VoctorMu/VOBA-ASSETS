from helpers import candles, cfg, path

from deriv_swing.amd import find_amd
from deriv_swing.divergence import find_divergences
from deriv_swing.indicators import atr, rsi
from deriv_swing.patterns import find_patterns
from deriv_swing.swings import find_swings

S = cfg()["strategy"]


def detect(closes):
    df = candles(closes)
    a = atr(df, 14)
    sw = find_swings(df, S["swing_window"])
    return df, sw, find_patterns(df, sw, a, S), find_divergences(sw, rsi(df["close"], 14), S["swing_window"], S)


def test_swings_alternate():
    _, sw, _, _ = detect(path([(0, 100), (20, 100), (10, 120), (6, 110), (6, 120), (10, 100)]))
    kinds = [s.kind for s in sw]
    assert all(a != b for a, b in zip(kinds, kinds[1:]))


def test_m_double_top():
    closes = path([(0, 100), (30, 100), (10, 120), (6, 110), (6, 120), (10, 100)])
    df, _, pats, _ = detect(closes)
    m = [p for p in pats if p.name == "double_top"]
    assert len(m) == 1 and m[0].direction == -1
    assert df["close"].iloc[m[0].break_idx] < 110          # closed through the neckline
    assert df["close"].iloc[m[0].break_idx - 1] >= 109.9   # first close through it
    assert m[0].stop >= 120


def test_w_double_bottom_is_mirror():
    closes = 300 - path([(0, 100), (30, 100), (10, 120), (6, 110), (6, 120), (10, 100)])
    _, _, pats, _ = detect(closes)
    assert [p.name for p in pats] == ["double_bottom"]
    assert pats[0].direction == 1 and pats[0].stop <= 180


def test_unequal_peaks_are_not_an_m():
    _, _, pats, _ = detect(path([(0, 100), (30, 100), (10, 120), (6, 110), (6, 135), (10, 100)]))
    assert not [p for p in pats if p.name == "double_top"]


def test_head_and_shoulders():
    closes = path([(0, 100), (30, 100), (8, 115), (5, 108), (7, 125), (7, 108), (5, 115), (8, 98)])
    _, _, pats, _ = detect(closes)
    hs = [p for p in pats if p.name == "head_shoulders"]
    assert len(hs) == 1 and hs[0].direction == -1
    assert round(hs[0].stop, 1) == 115.3   # right shoulder high


def test_inverse_head_and_shoulders():
    closes = 300 - path([(0, 100), (30, 100), (8, 115), (5, 108), (7, 125), (7, 108), (5, 115), (8, 98)])
    _, _, pats, _ = detect(closes)
    assert [p.name for p in pats if "head" in p.name] == ["inv_head_shoulders"]


def test_bullish_rsi_divergence():
    _, _, _, divs = detect(path([(0, 150), (20, 150), (10, 100), (8, 120), (25, 97), (10, 115)]))
    assert len(divs) == 1
    d = divs[0]
    assert d.direction == 1 and d.p2[1] < d.p1[1] and d.p2[2] > d.p1[2]


def test_no_divergence_when_momentum_confirms():
    # first low is a choppy grind (moderate RSI); second is a sharp, deeper drop: RSI makes a lower low too
    chop = [(2, 146), (1, 148), (2, 142), (1, 144), (2, 138), (1, 140), (2, 134), (1, 136), (2, 130), (1, 132), (2, 120)]
    _, _, _, divs = detect(path([(0, 150), (20, 150)] + chop + [(8, 135), (5, 90), (10, 110)]))
    assert not [d for d in divs if d.direction == 1]


def _amd_frame(break_close=105.0, sweep_close=100.8):
    df = candles(list(path([(0, 90), (30, 100)])) + [101, 102, 101.5, 100.5, 101, 102, 101, 100.6, 101.8, 101.2])
    for o, h, lo, c in [(101.2, 101.5, 97.0, sweep_close), (100.8, 101.6, 100.3, 101.4),
                        (101.4, max(105.5, break_close), 101.0, break_close)]:
        df.loc[len(df)] = [df["time"].iloc[-1] + 14400, o, h, lo, c]
    return df


def test_amd_range_sweep_break():
    df = _amd_frame()
    amds = find_amd(df, atr(df, 14), S, 12)
    assert len(amds) == 1
    a = amds[0]
    assert a.direction == 1 and a.manip_extreme == 97.0 and a.dist_idx == len(df) - 1
    assert a.range_low > 97.0 and a.range_high < 105.0


def test_amd_needs_close_back_inside():
    df = _amd_frame(sweep_close=97.5)   # sweep candle closes below the range: a real breakdown, not a stop hunt
    assert not [a for a in find_amd(df, atr(df, 14), S, 12) if a.direction == 1]


def test_amd_needs_distribution_close():
    df = _amd_frame(break_close=102.0)  # never closes above the range
    assert not [a for a in find_amd(df, atr(df, 14), S, 12) if a.direction == 1]
