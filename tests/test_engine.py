from fastapi.testclient import TestClient
from helpers import candles, cfg, path

import deriv_swing.engine as engine_mod
from deriv_swing.broker import PaperBroker
from deriv_swing.engine import Engine
from deriv_swing.server import create_app
from deriv_swing.strategy import Analysis, Signal

SYMS = ["Volatility 75 Index", "Volatility 25 Index", "Volatility 50 Index", "Volatility 100 Index"]


class FakeFeed:
    def __init__(self):
        self.t = 1_800_000_000
        self.df = candles(path([(0, 1000), (200, 1100)]))

    def candles(self, symbol, tf, count):
        return self.df

    def price(self, symbol):
        return 999.9, 1000.0

    def now(self):
        return self.t


def make_engine(tmp_path, monkeypatch, signal_for=SYMS):
    c = cfg()
    c["symbols"] = SYMS
    feed = FakeFeed()
    broker = PaperBroker(feed, 10_000, tmp_path / "paper.json")

    def fake_analyse(symbol, h4, d1, w1, cfg):
        an = Analysis(symbol, int(h4["time"].iloc[-1]), 1, 1, status="test")
        if symbol in signal_for:
            an.signal = Signal(symbol, 1, an.bar_time, 1000.0, 990.0, f"{symbol}|1|1", ["test"])
        return an

    monkeypatch.setattr(engine_mod, "analyse", fake_analyse)
    return Engine(c, feed, broker, tmp_path), broker


def test_engine_opens_sized_trades_until_daily_cap(tmp_path, monkeypatch):
    eng, broker = make_engine(tmp_path, monkeypatch)
    eng.step()
    pos = broker.open_positions()
    assert len(pos) == 3                       # 3 x 2% = 6% cap; the 4th is refused
    p = pos[0]
    assert p.sl == 990.0 and p.tp == 1050.0    # 1:5 -> 10 risk, 50 reward
    assert abs((p.entry - p.sl) / 0.01 * 0.01 * p.volume - 200) < 1
    rows = {r["symbol"]: r for r in eng.journal.recent()}
    assert rows[SYMS[3]]["status"] == "rejected" and "cap" in rows[SYMS[3]]["detail"]


def test_engine_does_not_repeat_a_setup(tmp_path, monkeypatch):
    eng, broker = make_engine(tmp_path, monkeypatch, signal_for=SYMS[:1])
    eng.step()
    eng.last_bar.clear()
    eng.step()
    assert len(broker.open_positions()) == 1


def test_paused_engine_does_not_trade(tmp_path, monkeypatch):
    eng, broker = make_engine(tmp_path, monkeypatch)
    eng.set_paused(True)
    eng.step()
    assert broker.open_positions() == []


def test_paper_stop_loss_closes_trade(tmp_path, monkeypatch):
    eng, broker = make_engine(tmp_path, monkeypatch, signal_for=SYMS[:1])
    eng.step()
    eng.feed.price = lambda s: (989.0, 989.1)
    broker.sync()
    assert broker.open_positions() == []
    closed = broker.closed_trades()[0]
    assert closed.reason == "sl" and round(closed.profit) == -200
    assert round(broker.account().balance) == 9_800


def test_api(tmp_path, monkeypatch):
    eng, _ = make_engine(tmp_path, monkeypatch, signal_for=SYMS[:1])
    eng.step()
    c = cfg()
    c["symbols"] = SYMS
    c["dashboard"]["token"] = "secret"
    client = TestClient(create_app(eng, c, run_engine=False))
    assert client.get("/api/status").status_code == 401
    h = {"Authorization": "Bearer secret"}
    st = client.get("/api/status", headers=h).json()
    assert st["account"]["name"] == "Paper" and len(st["symbols"]) == 4
    an = client.get(f"/api/analysis/{SYMS[0]}", headers=h).json()
    assert an["signal"]["direction"] == 1 and len(an["candles"]) > 100
    assert len(client.get("/api/trades", headers=h).json()["open"]) == 1
    assert client.post("/api/control", json={"action": "close_all"}, headers=h).json()["closed"] == 1
    assert client.get("/api/status", headers=h).json()["paused"] is True
    assert client.get("/").status_code == 200
