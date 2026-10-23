# VOBA-ASSETS · Deriv Swing

A fully automatic **4H swing-trading engine** for Deriv's **Volatility 75** and other Volatility indices on
**Deriv MT5**, with a web dashboard you can open from your phone or PC.

Think of it as an airline crew:

- The **Python engine** is the pilot. Every time a 4H candle closes, it checks each index against the flight plan
  (the strategy) and acts when every item on the checklist passes.
- **MetaTrader 5** is the plane's engines. Orders go through the MT5 terminal on the same Windows VPS.
- The **dashboard** is the cockpit window. You can see what the pilot sees, and you have the big red
  *Close all* lever.

![dashboard: watchlist, 4H chart with patterns and RSI, setup checklist](docs/dashboard.png)

---

## The strategy

A trade is only taken when **all** of these are true on the latest *closed* 4H candle:

| # | Check | Detail |
|---|-------|--------|
| 1 | **Weekly trend** | Price above the 50 EMA without a lower-high + lower-low structure (bullish), or the mirror image (bearish). |
| 2 | **Daily trend** | Same test on the Daily chart. Daily and Weekly must **agree**; that is the only direction allowed. |
| 3 | **4H setup** in that direction | **M** (double top) / **W** (double bottom), **head & shoulders** / inverse H&S with a close through the neckline, **or** regular **RSI(14) divergence**. Must be recent (within `setup_window` candles). |
| 4 | **AMD complete** | **A**ccumulation: a tight 4H range. **M**anipulation: a wick sweeps one side of the range and the candle closes back inside (a stop hunt). **D**istribution: a 4H candle *closes* beyond the opposite side. |
| 5 | **4H close confirmation** | The candle that just closed must be the AMD break or the pattern's neckline break. The engine never chases a setup that confirmed earlier. |

AMD works like a spring: price coils (accumulation), gets pushed the wrong way to grab stops (manipulation),
then releases (distribution). Buying the release *after* the fake-out means the stop goes beyond the fake-out's
extreme, the point that would prove the idea wrong.

**Stop loss:** beyond the AMD sweep extreme. If a pattern was part of the setup, the stop also clears the
pattern's invalidation point (the right shoulder or the M/W peak). A small ATR buffer is added.
**Take profit:** 5 × the stop distance (1:5).

## Risk management

| Rule | Setting |
|---|---|
| Risk per trade | **2%** of equity. Lot size is calculated from the stop distance and the symbol's tick value, always **rounded down**. |
| Reward : risk | **1 : 5** |
| Daily loss cap | **−6%**. A trade is refused if *today's loss + the risk on all open trades + this trade* could exceed 6%. In practice that means at most three full-risk trades at a time and no revenge trading after a bad day. |
| Too-big minimum lot | If the broker's minimum lot would risk more than 2%, the trade is skipped, never forced. |
| Duplicates | Each AMD setup trades once (it is logged in `state/.../journal.sqlite`). |
| Kill switch | **Close all** in the dashboard closes every bot position and pauses the engine. |

What 2% at 1:5 means in numbers:
- You break even at a **16.7% win rate** (1 winner pays for 5 losers).
- **10 losses in a row** cost about **18%** of the account, and losing streaks of that length *will* happen with a low-win-rate system.
- Each winner adds about **+10%**.

The bot only manages positions that carry its own `magic` number, so manual trades on the same account are left alone.

---

## Install on a Windows VPS

1. Rent a Windows VPS (any provider; 2 GB RAM is enough). Install **Deriv MT5** and **Python 3.11+** (tick
   "Add to PATH").
2. Log into your **Deriv MT5 demo** account in the terminal. In *Tools → Options → Expert Advisors*, enable
   **Algo Trading**.
3. Get the code and install:
   ```bat
   git clone https://github.com/voctormu/voba-assets.git
   cd voba-assets
   pip install -r requirements.txt
   copy config.example.yaml config.yaml
   ```
4. Edit `config.yaml`: fill in `mt5.login`, `mt5.password` and `mt5.server`, and set `data_source: mt5`.
5. Run it:
   ```bat
   python run.py
   ```
   Then open <http://127.0.0.1:8075> in the VPS browser.

**Viewing the dashboard from your phone:** set `dashboard.host: 0.0.0.0` **and** a long random
`dashboard.token` (the app refuses to start without one), and open port 8075 in the VPS firewall. The page asks
for the token once.

## Go live in stages

| Stage | `data_source` | `execution` | MT5 login | What you're checking |
|---|---|---|---|---|
| 0. Try the app anywhere | `synthetic` | `paper` | – | The dashboard works. Time runs 1,440× faster, so a 4H candle closes every 10 s. |
| 1. Backtest | – | – | any | `python -m deriv_swing.backtest --mt5 --bars 6000` replays the exact live rules over real Deriv history. |
| 2. Paper on real prices | `mt5` | `paper` | any | Signals on real candles; fills are simulated locally. |
| 3. Demo account | `mt5` | `mt5` | **Deriv-Demo** | Real orders, lot sizing and SL/TP on a demo account. |
| 4. Live | `mt5` | `mt5` | real | Only after stages 1–3 look right to you. |

**Be honest with yourself at stage 1.** Deriv's Volatility indices are generated by a random number generator
with constant volatility, so chart patterns have no guaranteed edge on them. The backtest is how you find out
whether this rule set holds up on real Deriv data before you risk money. Every threshold is in `config.yaml`
(`strategy:` section) so you can test variations.

## Project layout

```
run.py                      start engine + dashboard
config.example.yaml         every setting, documented
deriv_swing/
  swings.py                 swing highs/lows (the raw material for every pattern)
  patterns.py               M / W / head & shoulders / inverse H&S
  divergence.py             regular RSI divergence
  amd.py                    accumulation -> manipulation -> distribution
  structure.py              Daily / Weekly trend
  strategy.py               the checklist above -> Signal
  risk.py                   lot sizing, daily loss cap
  engine.py                 the loop: new 4H close -> analyse -> risk checks -> order
  broker.py                 PaperBroker and MT5Broker
  data.py                   MT5 candles, or a synthetic feed for testing
  journal.py                SQLite log of every signal and why it was taken or refused
  backtest.py               bar-by-bar replay of the live rules
  server.py, static/        FastAPI + dashboard (TradingView Lightweight Charts, bundled)
tests/                      pytest: detectors on hand-built charts, sizing, cap, engine, API
```

Run the tests with `pip install -r requirements-dev.txt && python -m pytest`.

## Limitations

- Paper mode checks SL/TP at each poll (every 30 s), not on every tick, so paper fills are approximate.
- The backtest assumes the stop is hit first when one candle touches both SL and TP. It doesn't model spread
  or the daily cap, so treat its results as optimistic.
- The bot does not trail stops or take partial profits; each trade runs to its SL or its 5R target.

Charts use [TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/) (Apache 2.0).
Trading synthetic indices carries a high risk of loss. This software is a tool, not financial advice.
