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


class Collector:
    def __init__(self):
        self.msgs = []

    def __call__(self, text):
        self.msgs.append(text)


def with_alerts(eng, notify_rejections=False):
    from deriv_swing.notify import Notifier
    sent = Collector()
    n = Notifier({"enabled": False, "notify_rejections": notify_rejections}, sender=sent)
    n.enabled = True
    n.send = sent            # deliver synchronously for the test
    eng.notifier = n
    return sent


def test_breakeven_moves_stop_at_2r_once(tmp_path, monkeypatch):
    eng, broker = make_engine(tmp_path, monkeypatch, signal_for=SYMS[:1])
    sent = with_alerts(eng)
    eng.step()                                         # BUY @1000, SL 990 -> 1R = 10
    eng.feed.price = lambda s: (1019.0, 1019.1)        # +1.9R: not yet
    eng.step()
    assert broker.open_positions()[0].sl == 990.0
    eng.feed.price = lambda s: (1020.0, 1020.1)        # +2R
    eng.step()
    assert broker.open_positions()[0].sl == 1000.0
    eng.step()
    assert sum("Break-even" in m for m in sent.msgs) == 1
    eng.feed.price = lambda s: (999.0, 999.1)          # reverses: stopped at entry, 0R
    eng.step()
    assert broker.open_positions() == []
    assert broker.closed_trades()[0].profit == 0
    assert "Closed at break-even" in sent.msgs[-1] and "+0.0R" in sent.msgs[-1]


def test_breakeven_disabled(tmp_path, monkeypatch):
    eng, broker = make_engine(tmp_path, monkeypatch, signal_for=SYMS[:1])
    eng.cfg["risk"]["breakeven"]["enabled"] = False
    eng.step()
    eng.feed.price = lambda s: (1030.0, 1030.1)
    eng.step()
    assert broker.open_positions()[0].sl == 990.0


def test_breakeven_frees_daily_cap(tmp_path, monkeypatch):
    eng, broker = make_engine(tmp_path, monkeypatch)
    eng.step()
    assert len(broker.open_positions()) == 3
    eng.feed.price = lambda s: (1020.0, 1020.1)        # all three reach 2R -> no open risk left
    eng.step()
    eng.journal.db.execute("DELETE FROM signals WHERE status='rejected'")
    eng.last_bar.clear()
    eng.step()
    assert len(broker.open_positions()) == 4


def test_alerts_for_open_tp_and_rejection(tmp_path, monkeypatch):
    eng, broker = make_engine(tmp_path, monkeypatch)
    sent = with_alerts(eng, notify_rejections=True)
    eng.step()
    opened = [m for m in sent.msgs if m.startswith("🟢")]
    assert len(opened) == 3 and "SL 990.00" in opened[0] and "TP 1050.00" in opened[0]
    assert any("Skipped" in m and "cap" in m for m in sent.msgs)
    eng.feed.price = lambda s: (1051.0, 1051.1)        # TP for all
    eng.step()
    assert sum("Take profit hit" in m and "+5.0R" in m for m in sent.msgs) == 3


def test_daily_cap_alert_once(tmp_path, monkeypatch):
    eng, broker = make_engine(tmp_path, monkeypatch)
    sent = with_alerts(eng)
    eng.step()
    eng.feed.price = lambda s: (989.0, 989.1)          # three stops -> -6%
    eng.step()
    eng.step()
    assert sum("Daily loss cap hit" in m for m in sent.msgs) == 1


def test_notifier_disabled_without_credentials():
    from deriv_swing.notify import Notifier
    sent = Collector()
    Notifier({"enabled": True, "access_token": "", "phone_number_id": "1", "template": "t", "to": ["1"]},
             sender=sent).send("x")
    assert sent.msgs == []


def test_notifier_background_delivery():
    import time

    from deriv_swing.notify import Notifier
    sent = Collector()
    n = Notifier({"enabled": True, "access_token": "t", "phone_number_id": "1", "template": "t", "to": "1"},
                 sender=sent)
    n.send("hello")
    for _ in range(50):
        if sent.msgs:
            break
        time.sleep(0.02)
    assert sent.msgs == ["hello"]


def test_whatsapp_template_param_is_single_line():
    from deriv_swing.notify import to_param
    assert to_param("🟢 BUY V75\nEntry 1 · SL 2\n\n  stop moved.   This\tline.") == \
        "🟢 BUY V75 · Entry 1 · SL 2 · stop moved. This line"
    assert len(to_param("x" * 5000)) == 1000


def test_whatsapp_payload_to_every_recipient(monkeypatch):
    import deriv_swing.notify as nt
    posted = []
    monkeypatch.setattr(nt, "_post", lambda cfg, payload: posted.append(payload))
    n = nt.Notifier({"enabled": True, "access_token": "tok", "phone_number_id": "123", "template": "trade_alert",
                     "language": "en", "to": ["+27 82 000 0000", 447700900000]}, sender=lambda t: None)
    n._send_whatsapp("BUY V75")
    assert [p["to"] for p in posted] == ["27820000000", "447700900000"]
    assert posted[0]["template"] == {"name": "trade_alert", "language": {"code": "en"}, "components": [
        {"type": "body", "parameters": [{"type": "text", "text": "BUY V75"}]}]}
    assert posted[0]["messaging_product"] == "whatsapp" and posted[0]["type"] == "template"
