"""Runs the MT5 adapter against a stand-in for the Windows-only MetaTrader5 package."""
from types import SimpleNamespace as NS

import numpy as np

from deriv_swing.broker import MT5Broker
from deriv_swing.data import MT5Feed


class FakeMT5:
    TIMEFRAME_H4, TIMEFRAME_D1, TIMEFRAME_W1 = 16388, 16408, 32769
    TRADE_ACTION_DEAL, ORDER_TYPE_BUY, ORDER_TYPE_SELL = 1, 0, 1
    ORDER_TIME_GTC, ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 0, 1, 2
    TRADE_RETCODE_DONE, POSITION_TYPE_BUY = 10009, 0
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_TYPE_BUY, DEAL_REASON_SL, DEAL_REASON_TP = 0, 1, 0, 4, 5

    def __init__(self):
        self.sent = []

    def symbol_select(self, s, on):
        return True

    def copy_rates_from_pos(self, symbol, tf, pos, count):
        dt = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                       ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])
        return np.array([(1_700_000_000 + i * 14400, 1, 2, 0.5, 1.5, 0, 0, 0) for i in range(count)], dtype=dt)

    def symbol_info_tick(self, s):
        return NS(bid=400_000.0, ask=400_010.0)

    def symbol_info(self, s):
        return NS(trade_tick_size=0.01, trade_tick_value=0.01, volume_min=0.001, volume_step=0.001,
                  volume_max=1.0, trade_stops_level=100, point=0.01, digits=2, filling_mode=1)

    def account_info(self):
        return NS(balance=1000.0, equity=990.0, currency="USD", login=1, server="Deriv-Demo")

    def order_send(self, req):
        self.sent.append(req)
        return NS(retcode=self.TRADE_RETCODE_DONE, order=42, price=req["price"], comment="done")

    def positions_get(self):
        return [NS(ticket=42, symbol="V75", type=0, volume=0.01, price_open=400_010.0, sl=395_000.0, tp=425_000.0,
                   time=1, profit=-0.1, comment="deriv-swing", magic=750075),
                NS(ticket=7, symbol="V75", type=1, volume=1, price_open=1, sl=0, tp=0, time=1, profit=0,
                   comment="manual", magic=0)]

    def history_deals_get(self, a, b):
        assert isinstance(a, int) and isinstance(b, int)
        return [NS(position_id=42, magic=750075, entry=0, type=0, price=400_010.0, time=1, symbol="V75", volume=0.01,
                   reason=0, profit=0, commission=0, swap=0),
                NS(position_id=42, magic=750075, entry=1, type=1, price=395_000.0, time=2, symbol="V75", volume=0.01,
                   reason=4, profit=-50.1, commission=0, swap=0)]

    def last_error(self):
        return (0, "ok")


def test_feed_drops_forming_bar_and_shapes_frame():
    df = MT5Feed(FakeMT5()).candles("V75", "H4", 5)
    assert list(df.columns) == ["time", "open", "high", "low", "close"] and len(df) == 5


def test_broker_roundtrip():
    mt5 = FakeMT5()
    b = MT5Broker(mt5, 750075, 200)
    spec = b.symbol_spec("V75")
    assert spec.min_stop_distance == 1.0
    assert b.account().equity == 990.0
    res = b.open("V75", 1, 0.01, 395_000.123, 425_000.0, "deriv-swing")
    assert res.ok and mt5.sent[0]["price"] == 400_010.0 and mt5.sent[0]["sl"] == 395_000.12
    assert [p.ticket for p in b.open_positions()] == [42]           # manual trade ignored
    closed = b.closed_trades()
    assert closed[0].reason == "sl" and closed[0].direction == 1
    assert b.close_all() == 1 and mt5.sent[-1]["position"] == 42
