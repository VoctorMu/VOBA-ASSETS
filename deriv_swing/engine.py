"""The trading loop: on every new closed 4H candle, analyse each symbol and act on signals."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from .journal import Journal
from .notify import Notifier
from .risk import DailyGuard, money_at_risk, position_size
from .strategy import Analysis, analyse

log = logging.getLogger("deriv_swing")


class Engine:
    def __init__(self, cfg: dict, feed, broker, state_dir: Path, notifier: Notifier | None = None):
        self.cfg = cfg
        self.notifier = notifier or Notifier(None)
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
        self.known_open: set[int] | None = None   # tickets seen open on the previous step
        self.cap_alert_day: str | None = state.get("cap_alert_day")
        self.lock = threading.RLock()

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"paused": self.paused, "guard": self.guard.state(),
                                               "cap_alert_day": self.cap_alert_day}))

    def set_paused(self, paused: bool):
        with self.lock:
            self.paused = paused
            self._save()

    def step(self):
        with self.lock:
            self.broker.sync()
            self._report_closed()
            self._manage_breakeven()
            now = self.feed.now()
            equity = self.broker.account().equity
            self.guard.roll(datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"), equity)
            if self.guard.loss_pct(equity) >= self.guard.cap_pct and self.cap_alert_day != self.guard.day:
                self.cap_alert_day = self.guard.day
                self.notifier.send(f"🛑 Daily loss cap hit (-{self.guard.loss_pct(equity):.2f}%). "
                                   f"No new trades until tomorrow (UTC).")
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
                    if self.errors.get(sym) != str(exc):
                        self.notifier.send(f"⚠️ Error on {sym}: {exc}")
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
            if self.notifier.notify_rejections:
                self.notifier.send(f"⏭️ Skipped {_side(sig.direction)} {sig.symbol}\n{why}")

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
        if self.known_open is not None:
            self.known_open.add(res.ticket)
        be = rcfg.get("breakeven", {})
        be_txt = f"\nStop moves to break-even at {be['trigger_r']:g}R" if be.get("enabled") else ""
        self.notifier.send(
            f"{'🟢' if sig.direction > 0 else '🔴'} {_side(sig.direction)} {sig.symbol}\n"
            f"Entry {row['entry']:.2f} · SL {sig.stop:.2f} · TP {target:.2f}\n"
            f"{volume:g} lots · risk {risk_money:.2f} ({rcfg['risk_per_trade_pct']:g}%) · 1:{rcfg['reward_risk']:g}\n"
            f"{' · '.join(sig.reasons)}{be_txt}")

    def _manage_breakeven(self):
        """Move the stop to entry (+lock_r) once a trade has run trigger_r times its initial risk."""
        be = self.cfg["risk"].get("breakeven", {})
        if not be.get("enabled"):
            return
        for p in self.broker.open_positions():
            risk = (p.entry - p.sl) * p.direction   # > 0 only while the stop is still the original one
            if not p.sl or risk <= 0:
                continue
            bid, ask = self.feed.price(p.symbol)
            price = bid if p.direction > 0 else ask
            if (price - p.entry) * p.direction < be["trigger_r"] * risk:
                continue
            new_sl = p.entry + p.direction * be.get("lock_r", 0.0) * risk
            if abs(price - new_sl) < self.broker.symbol_spec(p.symbol).min_stop_distance:
                continue
            res = self.broker.modify_sl(p, new_sl)
            if res.ok:
                log.info("BREAK-EVEN #%s %s stop -> %s", p.ticket, p.symbol, new_sl)
                self.notifier.send(f"🛡️ Break-even #{p.ticket} {p.symbol}\n"
                                   f"Price reached {be['trigger_r']:g}R; stop moved to {new_sl:.2f}. "
                                   f"This trade can no longer lose.")
            else:
                log.warning("break-even failed on #%s: %s", p.ticket, res.message)

    def _report_closed(self):
        current = {p.ticket for p in self.broker.open_positions()}
        if self.known_open is None:       # first step: don't alert on trades closed before startup
            self.known_open = current
            return
        gone = self.known_open - current
        self.known_open = current
        if not gone:
            return
        closed = {t.ticket: t for t in self.broker.closed_trades(200)}
        for ticket in gone:
            t = closed.get(ticket)
            if t is None:
                continue
            row = self.journal.by_ticket(ticket)
            r = t.profit / row["risk_money"] if row and row.get("risk_money") else None
            r_txt = f" ({r:+.1f}R)" if r is not None else ""
            if r is not None and abs(r) < 0.05:
                icon, what = "⚪", "Closed at break-even"
            elif t.reason == "tp":
                icon, what = "🏆", "Take profit hit"
            elif t.reason == "sl":
                icon, what = ("💚", "Stop hit in profit") if t.profit > 0 else ("❌", "Stop loss hit")
            else:
                icon, what = "✋", "Closed manually"
            self.notifier.send(f"{icon} {what}: {_side(t.direction)} {t.symbol}\n"
                               f"P/L {t.profit:+.2f}{r_txt} · exit {t.exit:.2f}")

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


def _side(direction: int) -> str:
    return "BUY" if direction > 0 else "SELL"
