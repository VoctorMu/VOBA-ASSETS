import pytest

from deriv_swing.risk import DailyGuard, SymbolSpec, money_at_risk, position_size

V75 = SymbolSpec(tick_size=0.01, tick_value=0.01, volume_min=0.001, volume_step=0.001, volume_max=1.0)


def test_size_risks_two_percent():
    # $10,000 at 2% = $200. Stop 5,000 points away -> $5,000 per lot -> 0.04 lots.
    vol, why = position_size(10_000, 2.0, 400_000.0, 395_000.0, V75)
    assert why == "" and vol == pytest.approx(0.04)
    assert money_at_risk(V75, 400_000.0, 395_000.0, vol) == pytest.approx(200)


def test_size_rounds_down_never_over_risks():
    vol, _ = position_size(10_000, 2.0, 400_000.0, 393_000.0, V75)
    assert money_at_risk(V75, 400_000.0, 393_000.0, vol) <= 200


def test_min_lot_too_big_is_rejected():
    spec = SymbolSpec(0.01, 0.01, volume_min=0.5, volume_step=0.01, volume_max=100)
    vol, why = position_size(100, 2.0, 10_000.0, 9_000.0, spec)
    assert vol == 0 and "minimum lot" in why


def test_stop_inside_broker_minimum_is_rejected():
    spec = SymbolSpec(0.01, 0.01, 0.001, 0.001, 1.0, min_stop_distance=50)
    assert position_size(10_000, 2.0, 1000.0, 990.0, spec)[0] == 0


def test_daily_guard_allows_three_two_percent_losses_not_four():
    g = DailyGuard(6.0)
    g.roll("2026-10-23", 10_000)
    assert g.allows(10_000, open_risk=400, new_risk=200)[0]
    assert not g.allows(10_000, open_risk=600, new_risk=200)[0]
    assert not g.allows(9_400, open_risk=0, new_risk=1)[0]          # cap already hit
    assert g.allows(9_800, open_risk=200, new_risk=200)[0]          # 2% lost + 4% at risk = 6%


def test_daily_guard_resets_next_day():
    g = DailyGuard(6.0)
    g.roll("2026-10-23", 10_000)
    assert not g.allows(9_300, 0, 100)[0]
    g.roll("2026-10-24", 9_300)
    assert g.allows(9_300, 0, 100)[0]
