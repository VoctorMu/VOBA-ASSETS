"""Order execution: PaperBroker (simulated) and MT5Broker (real orders via the MetaTrader5 package)."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .risk import SymbolSpec


@dataclass
class Position:
    ticket: int
    symbol: str
    direction: int
    volume: float
    entry: float
    sl: float
    tp: float
    open_time: int
    profit: float = 0.0
    comment: str = ""


@dataclass
class ClosedTrade:
    ticket: int
    symbol: str
    direction: int
    volume: float
    entry: float
    exit: float
    sl: float
    tp: float
    open_time: int
    close_time: int
    profit: float
    reason: str


@dataclass
class OrderResult:
    ok: bool
    ticket: int = 0
    price: float = 0.0
    message: str = ""


@dataclass
class Account:
    balance: float
    equity: float
    currency: str = "USD"
    name: str = ""


class PaperBroker:
    """Fills at the current bid/ask, closes at SL/TP when the polled price crosses them."""

    SPEC = SymbolSpec(tick_size=0.01, tick_value=0.01, volume_min=0.001, volume_step=0.001, volume_max=100)

    def __init__(self, feed, starting_balance: float, state_path: Path):
        self.feed = feed
        self.state_path = state_path
        self.balance = starting_balance
        self.positions: list[Position] = []
        self.history: list[ClosedTrade] = []
        self.next_ticket = 1
        if state_path.exists():
            s = json.loads(state_path.read_text())
            self.balance = s["balance"]
            self.positions = [Position(**p) for p in s["positions"]]
            self.history = [ClosedTrade(**h) for h in s["history"]]
            self.next_ticket = s["next_ticket"]

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "balance": self.balance, "next_ticket": self.next_ticket,
            "positions": [asdict(p) for p in self.positions],
            "history": [asdict(h) for h in self.history[-500:]],
        }))

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return self.SPEC

    def _pnl(self, p: Position, price: float) -> float:
        return (price - p.entry) * p.direction / self.SPEC.tick_size * self.SPEC.tick_value * p.volume

    def sync(self):
        """Mark to market and close anything whose SL/TP has been crossed."""
        changed = False
        for p in list(self.positions):
            bid, ask = self.feed.price(p.symbol)
            exit_side = bid if p.direction > 0 else ask
            hit = None
            if (p.direction > 0 and exit_side <= p.sl) or (p.direction < 0 and exit_side >= p.sl):
                hit, px = "sl", p.sl
            elif (p.direction > 0 and exit_side >= p.tp) or (p.direction < 0 and exit_side <= p.tp):
                hit, px = "tp", p.tp
            if hit:
                self._close(p, px, hit)
                changed = True
            else:
                p.profit = self._pnl(p, exit_side)
        if changed:
            self._save()

    def _close(self, p: Position, price: float, reason: str):
        pnl = self._pnl(p, price)
        self.balance += pnl
        self.positions.remove(p)
        self.history.append(ClosedTrade(p.ticket, p.symbol, p.direction, p.volume, p.entry, price, p.sl, p.tp,
                                        p.open_time, self.feed.now(), pnl, reason))

    def account(self) -> Account:
        return Account(self.balance, self.balance + sum(p.profit for p in self.positions), "USD", "Paper")

    def open(self, symbol, direction, volume, sl, tp, comment) -> OrderResult:
        bid, ask = self.feed.price(symbol)
        price = ask if direction > 0 else bid
        p = Position(self.next_ticket, symbol, direction, volume, price, sl, tp, self.feed.now(), 0.0, comment)
        self.next_ticket += 1
        self.positions.append(p)
        self._save()
        return OrderResult(True, p.ticket, price, "paper fill")

    def modify_sl(self, p: Position, sl: float) -> OrderResult:
        for mine in self.positions:
            if mine.ticket == p.ticket:
                mine.sl = sl
                self._save()
                return OrderResult(True, p.ticket, sl, "paper modify")
        return OrderResult(False, message="position not found")

    def open_positions(self) -> list[Position]:
        return list(self.positions)

    def closed_trades(self, limit: int = 100) -> list[ClosedTrade]:
        return self.history[-limit:][::-1]

    def close_all(self) -> int:
        n = 0
        for p in list(self.positions):
            bid, ask = self.feed.price(p.symbol)
            self._close(p, bid if p.direction > 0 else ask, "manual")
            n += 1
        self._save()
        return n


class MT5Broker:
    """Live/demo execution on the logged-in Deriv MT5 account. Only touches positions with our magic number."""

    def __init__(self, mt5, magic: int, deviation: int):
        self.mt5 = mt5
        self.magic = magic
        self.deviation = deviation

    def sync(self):
        pass

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"symbol {symbol} not found in MT5")
        return SymbolSpec(info.trade_tick_size, info.trade_tick_value, info.volume_min, info.volume_step,
                          info.volume_max, info.trade_stops_level * info.point)

    def account(self) -> Account:
        a = self.mt5.account_info()
        if a is None:
            raise RuntimeError("MT5 account_info() failed - is the terminal logged in?")
        return Account(a.balance, a.equity, a.currency, f"{a.login} @ {a.server}")

    def _filling(self, symbol: str) -> int:
        mode = self.mt5.symbol_info(symbol).filling_mode
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def open(self, symbol, direction, volume, sl, tp, comment) -> OrderResult:
        mt5 = self.mt5
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL,
            "price": tick.ask if direction > 0 else tick.bid,
            "sl": round(sl, info.digits),
            "tp": round(tp, info.digits),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(symbol),
        }
        res = mt5.order_send(request)
        if res is None:
            return OrderResult(False, message=f"order_send failed: {mt5.last_error()}")
        if res.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(False, message=f"retcode {res.retcode}: {res.comment}")
        return OrderResult(True, res.order, res.price, res.comment)

    def modify_sl(self, p: Position, sl: float) -> OrderResult:
        mt5 = self.mt5
        digits = mt5.symbol_info(p.symbol).digits
        res = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": p.ticket,
                              "sl": round(sl, digits), "tp": round(p.tp, digits), "magic": self.magic})
        if res is None:
            return OrderResult(False, message=f"order_send failed: {mt5.last_error()}")
        if res.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(False, message=f"retcode {res.retcode}: {res.comment}")
        return OrderResult(True, p.ticket, sl, res.comment)

    def open_positions(self) -> list[Position]:
        out = []
        for p in self.mt5.positions_get() or []:
            if p.magic != self.magic:
                continue
            out.append(Position(p.ticket, p.symbol, 1 if p.type == self.mt5.POSITION_TYPE_BUY else -1,
                                p.volume, p.price_open, p.sl, p.tp, int(p.time), p.profit, p.comment))
        return out

    def closed_trades(self, limit: int = 100) -> list[ClosedTrade]:
        mt5 = self.mt5
        now = int(time.time())
        deals = mt5.history_deals_get(now - 90 * 86400, now + 86400) or []
        opens = {d.position_id: d for d in deals if d.magic == self.magic and d.entry == mt5.DEAL_ENTRY_IN}
        out = []
        for d in deals:
            if d.entry != mt5.DEAL_ENTRY_OUT or d.position_id not in opens:
                continue
            o = opens[d.position_id]
            direction = 1 if o.type == mt5.DEAL_TYPE_BUY else -1
            reason = {mt5.DEAL_REASON_SL: "sl", mt5.DEAL_REASON_TP: "tp"}.get(d.reason, "manual")
            out.append(ClosedTrade(d.position_id, d.symbol, direction, d.volume, o.price, d.price, 0.0, 0.0,
                                   int(o.time), int(d.time), d.profit + d.commission + d.swap, reason))
        return sorted(out, key=lambda t: t.close_time, reverse=True)[:limit]

    def close_all(self) -> int:
        mt5 = self.mt5
        n = 0
        for p in self.open_positions():
            tick = mt5.symbol_info_tick(p.symbol)
            res = mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume, "position": p.ticket,
                "type": mt5.ORDER_TYPE_SELL if p.direction > 0 else mt5.ORDER_TYPE_BUY,
                "price": tick.bid if p.direction > 0 else tick.ask, "deviation": self.deviation,
                "magic": self.magic, "comment": "kill switch", "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(p.symbol),
            })
            if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
                n += 1
        return n
