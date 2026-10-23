"""Bar-by-bar backtest of the exact live strategy.

Fill model: enter at the trigger candle's close; a later candle touching the stop loses 1R,
touching the target wins reward_risk R; if one candle touches both, the loss is assumed.
Break-even: once a candle reaches trigger_r, the stop moves to entry (+lock_r) from the NEXT candle,
so a trade that then reverses scores lock_r (0R by default).
Results are reported in R so they are independent of account size; the equity curve compounds
risk_per_trade_pct per trade. Spread, slippage and the daily cap are not modelled.

Two steps: first every candidate setup on every symbol is found and played out on its own; then the
candidates are replayed in time order through the live selection rules (score bar, trades per week,
one open trade per symbol). Because the first step keeps every candidate, the report can show what
each score bar would have produced: that table is how you calibrate selection.min_score.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from .config import load_config
from .data import TF_SECONDS, SyntheticFeed
from .strategy import analyse


def candidates(symbol: str, h4: pd.DataFrame, d1: pd.DataFrame, w1: pd.DataFrame, cfg: dict,
               start: int = 300, lookback: int = 600) -> list[dict]:
    """Every signal the strategy produces on this symbol, each played out to SL / break-even / TP."""
    rr = cfg["risk"]["reward_risk"]
    be = cfg["risk"].get("breakeven", {})
    out, seen = [], set()
    hi, lo = h4["high"].to_numpy(), h4["low"].to_numpy()
    t = h4["time"].to_numpy()
    d1_close = d1["time"].to_numpy() + TF_SECONDS["D1"]
    w1_close = w1["time"].to_numpy() + TF_SECONDS["W1"]
    for i in range(start, len(h4)):
        now = int(t[i]) + TF_SECONDS["H4"]
        an = analyse(symbol, h4.iloc[max(0, i + 1 - lookback):i + 1],
                     d1.iloc[:int(np.searchsorted(d1_close, now, side="right"))].tail(400),
                     w1.iloc[:int(np.searchsorted(w1_close, now, side="right"))].tail(200), cfg)
        sig = an.signal
        if sig is None or sig.setup_id in seen:
            continue
        seen.add(sig.setup_id)
        entry, stop, d = sig.close, sig.stop, sig.direction
        risk = abs(entry - stop)
        target = entry + d * rr * risk
        result, exit_i, stop_r = None, None, -1.0
        for j in range(i + 1, len(h4)):
            hit_sl = lo[j] <= stop if d > 0 else hi[j] >= stop
            hit_tp = hi[j] >= target if d > 0 else lo[j] <= target
            if hit_sl or hit_tp:
                result, exit_i = (stop_r if hit_sl else rr), j
                break
            best = hi[j] if d > 0 else lo[j]
            if be.get("enabled") and stop_r < 0 and (best - entry) * d >= be["trigger_r"] * risk:
                stop_r = be.get("lock_r", 0.0)
                stop = entry + d * stop_r * risk
        if result is None:      # still open at the end of the data
            continue
        out.append({"symbol": symbol, "time": now, "exit_time": int(t[exit_i]) + TF_SECONDS["H4"],
                    "direction": d, "score": sig.score, "R": result, "bars": exit_i - i,
                    "reasons": "; ".join(sig.reasons[1:])})
    return out


def select(cands: list[dict], min_score: int, per_week: int | None) -> list[dict]:
    """Replay candidates in time order through the live selection rules."""
    from .engine import week_start

    taken, busy, weekly = [], {}, {}
    for c in sorted(cands, key=lambda c: c["time"]):
        if c["score"] < min_score or busy.get(c["symbol"], 0) > c["time"]:
            continue
        wk = week_start(c["time"])
        if per_week and weekly.get(wk, 0) >= per_week:
            continue
        weekly[wk] = weekly.get(wk, 0) + 1
        busy[c["symbol"]] = c["exit_time"]
        taken.append(c)
    return taken


def calibration_table(cands: list[dict], weeks: float, per_week: int | None) -> str:
    rows = []
    for bar in (0, 40, 50, 60, 70, 80):
        taken = select(cands, bar, per_week)
        r = np.array([c["R"] for c in taken]) if taken else np.array([0.0])
        rows.append(f"  {bar:>3}+   {len(taken) / weeks:6.2f}   {len(taken):5d}   "
                    f"{(r > 0).mean() * 100 if taken else 0:5.1f}%   {r.mean() if taken else 0:+6.2f}   "
                    f"{r.sum() if taken else 0:+7.1f}")
    head = f"  bar    /week  trades    win%   avg R   total R   (max {per_week or 'no limit'} per week)"
    buckets = []
    for lo_, hi_ in ((0, 40), (40, 50), (50, 60), (60, 70), (70, 80), (80, 101)):
        b = np.array([c["R"] for c in cands if lo_ <= c["score"] < hi_])
        if len(b):
            buckets.append(f"  {lo_:>3}-{min(hi_, 100) - 1:<3}  {len(b):5d}   {(b > 0).mean() * 100:5.1f}%   {b.mean():+6.2f}")
    return "\n".join(["Score bar -> trades taken (with the weekly limit and one trade per symbol):", head, *rows,
                      "", "Every candidate by score (does a higher score really mean better trades?):",
                      "  score   count    win%   avg R", *buckets])


def summarise(trades: list[dict], risk_pct: float) -> str:
    if not trades:
        return "No trades."
    df = pd.DataFrame(trades).sort_values("time")
    r = df["R"].to_numpy()
    flat = int((np.abs(r) < 1e-9).sum())
    equity = np.concatenate([[1.0], np.cumprod(1 + r * risk_pct / 100)])
    dd = (equity / np.maximum.accumulate(equity) - 1).min() * 100
    losses = -r[r < 0].sum()
    streak = max((len(s) for s in "".join("L" if x < 0 else "W" for x in r).split("W")), default=0)
    return "\n".join([
        f"Trades: {len(r)}   Win rate: {(r > 0).mean() * 100:.1f}%   Break-even exits: {flat}   "
        f"Total: {r.sum():+.1f}R",
        f"Profit factor: {(r[r > 0].sum() / losses) if losses else float('inf'):.2f}   "
        f"Longest losing streak: {streak}",
        f"Compounded at {risk_pct}%/trade: {(equity[-1] - 1) * 100:+.1f}%   Max drawdown: {dd:.1f}%",
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--mt5", action="store_true", help="use MT5 history instead of synthetic data")
    ap.add_argument("--bars", type=int, default=3000, help="4H bars of history per symbol")
    ap.add_argument("--no-breakeven", action="store_true", help="let every trade run to SL or TP")
    ap.add_argument("--min-score", type=int, help="override selection.min_score")
    ap.add_argument("--per-week", type=int, help="override selection.max_trades_per_week (0 = no limit)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.no_breakeven:
        cfg["risk"]["breakeven"]["enabled"] = False
    sel = cfg["selection"]
    min_score = sel["min_score"] if args.min_score is None else args.min_score
    per_week = sel["max_trades_per_week"] if args.per_week is None else args.per_week
    symbols = args.symbols or cfg["symbols"]

    if args.mt5:
        import MetaTrader5 as mt5

        from .data import MT5Feed
        if not mt5.initialize():
            raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
        feed = MT5Feed(mt5)
        frames = {s: (feed.candles(s, "H4", args.bars), feed.candles(s, "D1", args.bars // 6 + 400),
                      feed.candles(s, "W1", args.bars // 42 + 200)) for s in symbols}
    else:
        feed = SyntheticFeed(symbols, speed=0)
        frames = {s: tuple(feed.bars[s][tf].pipe(lambda f: f[f["time"] < feed.now()]) for tf in ("H4", "D1", "W1"))
                  for s in symbols}
        frames = {s: (h.tail(args.bars).reset_index(drop=True), d, w) for s, (h, d, w) in frames.items()}

    cands = []
    for s in symbols:
        found = candidates(s, *frames[s], cfg)
        print(f"{s}: {len(found)} candidate setups", flush=True)
        cands += found
    weeks = max(1.0, max(len(frames[s][0]) - 300 for s in symbols) / 42)   # 4H bars, 24/7 market
    taken = select(cands, min_score, per_week)
    risk_pct = cfg["risk"]["risk_per_trade_pct"]
    print(f"\n== {weeks:.0f} weeks · {len(cands)} candidates ({len(cands) / weeks:.2f}/week before selection)")
    print(calibration_table(cands, weeks, per_week))
    print(f"\n== Your settings: score >= {min_score}, max {per_week or 'unlimited'}/week -> "
          f"{len(taken) / weeks:.2f} trades/week")
    print(summarise(taken, risk_pct))
    if taken:
        df = pd.DataFrame(taken).sort_values("time")
        df["time"] = pd.to_datetime(df["time"], unit="s")
        print("\n" + df.drop(columns=["exit_time"]).tail(15).to_string(index=False))


if __name__ == "__main__":
    main()
