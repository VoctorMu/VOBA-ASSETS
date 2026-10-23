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
    for o, h, lo, c in [(101.2, 101.5, min(97.0, sweep_close - 0.5), sweep_close), (100.8, 101.6, 100.3, 101.4),
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
    df = _amd_frame(sweep_close=94.0)   # sweep candle closes below the range: a real breakdown, not a stop hunt
    assert not [a for a in find_amd(df, atr(df, 14), S, 12) if a.direction == 1]


def test_amd_needs_distribution_close():
    df = _amd_frame(break_close=102.0)  # never closes above the range
    assert not [a for a in find_amd(df, atr(df, 14), S, 12) if a.direction == 1]


def test_weekly_rule_not_against(monkeypatch):
    import deriv_swing.strategy as st
    c = cfg()
    df = candles(path([(0, 100), (150, 120)]))
    for d1, w1, rule, blocked in [(1, 0, "not_against", False), (1, -1, "not_against", True),
                                  (1, 0, "agree", True), (0, 1, "not_against", True)]:
        biases = iter([d1, w1])
        monkeypatch.setattr(st, "htf_bias", lambda *a, **k: next(biases))
        c["strategy"]["htf"]["weekly"] = rule
        an = st.analyse("X", df, df, df, c)
        assert ("standing aside" in an.status) == blocked, (d1, w1, rule, an.status)


def test_score_rewards_confluence():
    from deriv_swing.amd import AMD
    from deriv_swing.strategy import score_signal
    amd = AMD(1, 0, 10, 104.0, 100.0, 11, 98.0, 14)
    weak, _ = score_signal(False, [], [object()], amd, False, 104.1, 97.0, 2.0, 2.0, S)
    strong, parts = score_signal(True, [type("P", (), {"label": "W"})()], [object()], amd, True,
                                 105.0, 97.8, 2.0, 2.0, S)
    assert 0 <= weak < strong <= 100
    assert any("Weekly agrees" in p for p in parts) and any("same candle" in p for p in parts)


def test_setup_window_five_days():
    from deriv_swing.strategy import analyse
    # M pattern breaks, then ~20 quiet candles pass (older than 2 days, younger than 5)
    df = candles(list(path([(0, 100), (100, 100), (10, 120), (6, 110), (6, 120), (10, 100)])) + [100.0] * 20)
    c = cfg()
    c["strategy"]["setup_window"] = 12
    assert not analyse("X", df, df, df, c).patterns
    c["strategy"]["setup_window"] = 30
    assert [p.name for p in analyse("X", df, df, df, c).patterns] == ["double_top"]


def test_score_independent_of_amd_range_limit():
    import copy

    from deriv_swing.amd import AMD
    from deriv_swing.strategy import score_signal
    amd = AMD(1, 0, 10, 104.0, 100.0, 11, 98.0, 14)
    loose = copy.deepcopy(S)
    loose["amd"]["max_range_atr"] = 6.0
    args = (True, [], [object()], amd, False, 104.5, 97.0, 2.0, 2.0)
    assert score_signal(*args, S) == score_signal(*args, loose)


def test_looser_amd_range_finds_wider_coil():
    import copy
    # slow drift up to 104 and back: a coil about 4.6 ATR wide, then sweep below, hold, break above
    df = candles(list(path([(0, 100), (30, 100), (6, 104), (6, 100)])))
    for o, h, lo, c in [(100.0, 100.5, 98.5, 100.6), (100.6, 101.2, 100.3, 101.0), (101.0, 106.2, 100.8, 106.0)]:
        df.loc[len(df)] = [df["time"].iloc[-1] + 14400, o, h, lo, c]
    a = atr(df, 14)
    loose = copy.deepcopy(S)
    loose["amd"]["max_range_atr"] = 6.0
    assert find_amd(df, a, S, 12) == []
    found = find_amd(df, a, loose, 12)
    assert len(found) == 1 and found[0].direction == 1 and found[0].manip_extreme == 98.5


def test_amd_tries_shorter_range_when_longest_reaches_below_sweep():
    # price drifted up into the range, so the longest "tight" window reaches back below the sweep wick
    df = candles(list(path([(0, 95), (20, 95), (12, 100)])) + [101, 100.5, 101.5, 100.8, 101.2, 100.6, 101.4, 101.0])
    for o, h, lo, c in [(101.0, 101.3, 99.6, 100.9), (100.9, 101.5, 100.5, 101.2), (101.2, 104.0, 101.0, 103.8)]:
        df.loc[len(df)] = [df["time"].iloc[-1] + 14400, o, h, lo, c]
    found = find_amd(df, atr(df, 14), S, 12)
    assert found and found[0].direction == 1 and found[0].range_low > 99.6
