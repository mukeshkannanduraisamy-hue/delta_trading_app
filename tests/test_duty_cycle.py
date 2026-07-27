import pytest

from research import search


def test_short_dte_on_coarse_timeframe_is_unusable():
    """1h bars with 8h DTE gives 4 bars per contract against a 14-bar ATR
    warmup — no trade can ever open."""
    d = search.duty_cycle("1h", 8.0)
    assert d["bars_per_segment"] == 4
    assert d["tradeable_bars"] == 0
    assert d["usable"] is False
    assert "UNUSABLE" in search.duty_warning("1h", 8.0)


def test_the_combination_that_produced_the_false_result():
    """15m/8h: 2 of 16 bars tradeable. This reported a direction/theta ratio
    of 1.75 when the honest figure (5m, 71% duty) was 1.00."""
    d = search.duty_cycle("15m", 8.0)
    assert d["bars_per_segment"] == 16
    assert d["tradeable_bars"] == 2
    assert d["duty"] == pytest.approx(0.125)
    assert d["usable"] is False
    assert "UNRELIABLE" in search.duty_warning("15m", 8.0)


def test_5m_at_8h_is_usable():
    d = search.duty_cycle("5m", 8.0)
    assert d["tradeable_bars"] == 34
    assert d["duty"] == pytest.approx(34 / 48)
    assert d["usable"] is True
    assert search.duty_warning("5m", 8.0) is None


def test_long_dte_is_usable_on_every_intraday_timeframe():
    for tf in ("5m", "15m", "1h"):
        assert search.duty_cycle(tf, 240.0)["usable"], f"{tf} at 240h should be fine"


def test_4h_bars_need_long_dte():
    assert not search.duty_cycle("4h", 48.0)["usable"]
    assert search.duty_cycle("4h", 240.0)["usable"]


def test_duty_rises_with_dte_on_a_fixed_timeframe():
    prev = -1.0
    for dte in (8.0, 24.0, 48.0, 120.0, 240.0):
        d = search.duty_cycle("15m", dte)["duty"]
        assert d >= prev, "longer DTE means more tradeable bars per contract"
        prev = d


def test_duty_threshold_is_applied_consistently():
    for tf in ("5m", "15m", "1h", "4h"):
        for dte in (8.0, 24.0, 48.0, 120.0, 240.0):
            d = search.duty_cycle(tf, dte)
            assert d["usable"] == (d["duty"] >= search.DUTY_MIN)
            assert (search.duty_warning(tf, dte) is None) == d["usable"]


def test_decompose_skips_unusable_combinations():
    from research import decompose as dc
    r = dc.decompose("ema_cross", "1h", 8.0, 365)
    assert r["trades"] == 0 and r.get("skipped") is True
    assert "UNUSABLE" in r["duty_warning"]
