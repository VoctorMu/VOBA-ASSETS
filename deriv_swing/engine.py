"""The trading loop: on every new closed 4H candle, analyse each symbol and act on signals."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from .journal import Journal
from .risk import DailyGuard, money_at_risk, position_size
from .strategy import Analysis, analyse

log = logging.getLogger("deriv_swing")


class Engine:
    def __init__(self, cfg: dict, feed, broker, state_dir: Path):
        self.cfg = cfg
        self.feed = feed
        self.broker = broker
        self.state_path = state_dir / "engine.json"
        self.journal = Journal(state_dir / "journal.sqlite")
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.paused: bool = state.get("paused", False)
        self.guard = DailyGuard(cfg["risk"]["daily_loss_cap_pct"], state.get("guard"))
        self.last_bar: dict[str, int] = {}
        self.analyses: dict[str, Analysis] = {}
        self.errors: dict[str, str] = {}
        self.last_step = 0
        self.lock = threading.RLock()

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"paused": self.paused, "guard": self.guard.state()}))

    def set_paused(self, paused: bool):
        with self.lock:
            self.paused = paused
            self._save()

    def step(self):
        with self.lock:
            self.broker.sync()
            now = self.feed.now()
            self.guard.roll(datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"),
                            self.broker.account().equity)
            self._save()
            ecfg = self.cfg["engine"]
            for sym in self.cfg["symbols"]:
                try:
                    h4 = self.feed.candles(sym, "H4", ecfg["h4_bars"])
                    if h4.empty or self.last_bar.get(sym) == int(h4["time"].iloc[-1]):
                        continue
                    d1 = self.feed.candles(sym, "D1", ecfg["d1_bars"])
                    w1 = self.feed.candles(sym, "W1", ecfg["w1_bars"])
                    an = analyse(sym, h4, d1, w1, self.cfg)
                    an.candles = h4
                    self.analyses[sym] = an
                    self.last_bar[sym] = an.bar_time
                    self.errors.pop(sym, None)
                    if an.signal:
                        self._handle(an)
                except Exception as exc:  # one bad symbol must not stop the others
                    log.exception("error on %s", sym)
                    self.errors[sym] = str(exc)
            self.last_step = now

    def _handle(self, an: Analysis):
        sig = an.signal
        if self.journal.seen(sig.setup_id):
            return
        rcfg = self.cfg["risk"]
        row = dict(setup_id=sig.setup_id, created=self.feed.now(), bar_time=sig.bar_time, symbol=sig.symbol,
                   direction=sig.direction, stop=sig.stop, reasons=sig.reasons)

        def reject(why: str):
            log.info("%s rejected: %s", sig.setup_id, why)
            self.journal.record(**row, status="rejected", detail=why)

        if self.paused:
            return reject("engine paused")
        if rcfg.get("one_position_per_symbol") and any(
                p.symbol == sig.symbol for p in self.broker.open_positions()):
            return reject("already holding this symbol")

        bid, ask = self.feed.price(sig.symbol)
        entry = ask if sig.direction > 0 else bid
        if (entry - sig.stop) * sig.direction <= 0:
            return reject("price moved beyond the stop before entry")
        distance = abs(entry - sig.stop)
        target = entry + sig.direction * rcfg["reward_risk"] * distance
        row.update(entry=entry, target=target)

        spec = self.broker.symbol_spec(sig.symbol)
        equity = self.broker.account().equity
        volume, why = position_size(equity, rcfg["risk_per_trade_pct"], entry, sig.stop, spec)
        if volume <= 0:
            return reject(why)
        risk_money = money_at_risk(spec, entry, sig.stop, volume)
        row.update(volume=volume, risk_money=risk_money)

        open_risk = 0.0
        for p in self.broker.open_positions():
            loss_if_stopped = (p.entry - p.sl) * p.direction
            if p.sl and loss_if_stopped > 0:
                open_risk += money_at_risk(self.broker.symbol_spec(p.symbol), p.entry, p.sl, p.volume)
        ok, why = self.guard.allows(equity, open_risk, risk_money)
        if not ok:
            return reject(why)

        res = self.broker.open(sig.symbol, sig.direction, volume, sig.stop, target, "deriv-swing")
        if not res.ok:
            return reject(f"order failed: {res.message}")
        log.info("OPENED %s %s %.3f @ %s SL %s TP %s", "BUY" if sig.direction > 0 else "SELL",
                 sig.symbol, volume, res.price, sig.stop, target)
        row.update(entry=res.price or entry, ticket=res.ticket)
        self.journal.record(**row, status="opened", detail="")

    def close_all(self) -> int:
        with self.lock:
            return self.broker.close_all()

    def run_forever(self, stop: threading.Event):
        while not stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("engine step failed")
            stop.wait(self.cfg["engine"]["poll_seconds"])
