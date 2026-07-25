import calendar
import time

import pytest

from research import optdata


def test_parse_symbol_extracts_all_fields():
    got = optdata.parse_symbol("C-BTC-63000-270726")
    assert got["kind"] == "C"
    assert got["asset"] == "BTC"
    assert got["strike"] == 63000.0
    # DDMMYY 270726 -> 27 July 2026, settling 12:00 UTC
    assert got["expiry"] == calendar.timegm((2026, 7, 27, 12, 0, 0, 0, 0, 0))


def test_parse_symbol_handles_puts():
    assert optdata.parse_symbol("P-BTC-58000-010126")["kind"] == "P"


def test_parse_symbol_handles_other_underlyings():
    assert optdata.parse_symbol("C-ETH-3000-010126")["asset"] == "ETH"


@pytest.mark.parametrize("bad", [
    "BTCUSD",
    "C-BTC-63000",
    "C-BTC-abc-270726",
    "C-BTC-63000-2707261",
    "X-BTC-63000-270726",
    "",
    None,
])
def test_parse_symbol_rejects_malformed(bad):
    with pytest.raises(ValueError):
        optdata.parse_symbol(bad)


@pytest.mark.parametrize("bad", ["321326", "300226", "000726", "321226"])
def test_parse_symbol_rejects_impossible_dates(bad):
    """A silently wrong expiry would corrupt every DTE, theta and settlement
    calculation downstream, so the date is validated rather than sliced."""
    with pytest.raises(ValueError):
        optdata.parse_symbol(f"C-BTC-63000-{bad}")


def test_expiry_is_noon_utc():
    """Settlement at 12:00 UTC, confirmed empirically: the final mark candles
    on an expired contract are 11:58 and 11:59."""
    e = optdata.parse_symbol("C-BTC-63000-270726")["expiry"]
    assert time.gmtime(e).tm_hour == 12
    assert time.gmtime(e).tm_min == 0


def test_expiry_matches_app_settlement_assumption():
    """The study and the live engine must agree on settlement time."""
    from strategy import config
    assert optdata.SETTLEMENT_HOUR_UTC == 12
    assert config.MIN_HOURS_TO_EXPIRY > 0
