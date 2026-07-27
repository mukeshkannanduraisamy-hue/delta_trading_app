import gzip
import json
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pytest

from research import forward

_SCRATCH = Path(__file__).resolve().parent / ".scratch"


@pytest.fixture
def book_dir(monkeypatch):
    _SCRATCH.mkdir(exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=_SCRATCH))
    monkeypatch.setattr(forward, "BOOK_DIR", d)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _write(d, rows, name="book_BTC_2026-07-27.jsonl.gz"):
    with gzip.open(d / name, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _q(ts, sym, kind, strike, bid, ask, dte=48.0, spot=64000.0):
    return {"ts": ts, "symbol": sym, "kind": kind, "strike": strike,
            "bid": bid, "ask": ask, "dte_hours": dte, "spot": spot,
            "mid": (bid + ask) / 2}


# ---------------- loading ----------------

def test_load_book_skips_one_sided_quotes(book_dir):
    """A quote with no bid or no ask is not tradeable and must not appear."""
    _write(book_dir, [
        _q(1000, "C-BTC-64000-010826", "C", 64000, 100.0, 102.0),
        {"ts": 1000, "symbol": "C-BTC-65000-010826", "kind": "C",
         "strike": 65000, "bid": None, "ask": 50.0, "dte_hours": 48.0},
    ])
    snaps = forward.load_book()
    assert list(snaps) == [1000]
    assert set(snaps[1000]) == {"C-BTC-64000-010826"}


def test_load_book_tolerates_corrupt_lines(book_dir):
    p = book_dir / "book_BTC_2026-07-27.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write("{not json}\n")
        fh.write(json.dumps(_q(1000, "C-BTC-64000-010826", "C", 64000, 1.0, 2.0)) + "\n")
    assert len(forward.load_book()[1000]) == 1


# ---------------- contract selection ----------------

def test_pick_contract_prefers_nearest_expiry_then_nearest_strike():
    snap = {
        "C-BTC-64000-A": _q(0, "C-BTC-64000-A", "C", 64000, 1, 2, dte=200.0),
        "C-BTC-70000-B": _q(0, "C-BTC-70000-B", "C", 70000, 1, 2, dte=48.0),
        "C-BTC-64200-B": _q(0, "C-BTC-64200-B", "C", 64200, 1, 2, dte=48.0),
    }
    assert forward.pick_contract(snap, 64000.0, 48.0, True) == "C-BTC-64200-B"


def test_pick_contract_respects_call_put():
    snap = {
        "C-BTC-64000-B": _q(0, "C-BTC-64000-B", "C", 64000, 1, 2),
        "P-BTC-64000-B": _q(0, "P-BTC-64000-B", "P", 64000, 1, 2),
    }
    assert forward.pick_contract(snap, 64000.0, 48.0, True).startswith("C-")
    assert forward.pick_contract(snap, 64000.0, 48.0, False).startswith("P-")


def test_pick_contract_returns_none_on_empty_chain():
    assert forward.pick_contract({}, 64000.0, 48.0, True) is None


# ---------------- series construction ----------------

def test_series_marks_bars_without_a_contemporaneous_quote_invalid():
    """Inventing a price for an unquoted bar is how phantom fills happen."""
    snaps = {1000: {"C-BTC-64000-A": _q(1000, "C-BTC-64000-A", "C", 64000, 100, 102)}}
    bars = np.array([1000, 99999], dtype="int64")
    s = forward.build_book_series(snaps, bars, np.array([64000.0, 64000.0]), 48.0, True)
    assert s["valid"][0] and not s["valid"][1]


def test_series_never_interpolates_a_missing_quote():
    snaps = {1000: {"C-BTC-64000-A": _q(1000, "C-BTC-64000-A", "C", 64000, 100, 102)},
             9_000_000: {"C-BTC-64000-A": _q(9_000_000, "C-BTC-64000-A", "C", 64000, 200, 202)}}
    bars = np.array([1000, 4_500_000, 9_000_000], dtype="int64")
    s = forward.build_book_series(snaps, bars, np.full(3, 64000.0), 48.0, True)
    assert not s["valid"][1], "a gap must stay a gap"


def test_series_rejects_crossed_quotes():
    snaps = {1000: {"C-BTC-64000-A": _q(1000, "C-BTC-64000-A", "C", 64000, 105, 100)}}
    bars = np.array([1000], dtype="int64")
    s = forward.build_book_series(snaps, bars, np.array([64000.0]), 48.0, True)
    assert not s["valid"][0], "ask < bid is a bad quote, not a free lunch"


# ---------------- taker fills ----------------

def test_taker_fill_charges_spread_on_both_sides():
    """Enter at ask, exit at bid — the side that actually fills."""
    n = 4
    ce = {"ask": np.array([102.0] * n), "bid": np.array([98.0] * n),
          "mid": np.array([100.0] * n)}
    trades = [{"entry_i": 0, "exit_i": 2, "opt": "CE", "r": 1.0, "stop_d": 10.0}]
    out = forward._apply_taker_fills(trades, ce, ce)
    # (ask-mid) + (mid-bid) = 2 + 2 = 4, over a stop of 10 -> 0.4 R
    assert out[0]["spread_r"] == pytest.approx(0.4)
    assert out[0]["r"] == pytest.approx(0.6)


def test_taker_fill_never_improves_a_trade():
    n = 3
    ce = {"ask": np.array([101.0] * n), "bid": np.array([99.0] * n),
          "mid": np.array([100.0] * n)}
    trades = [{"entry_i": 0, "exit_i": 1, "opt": "CE", "r": 0.5, "stop_d": 5.0}]
    assert forward._apply_taker_fills(trades, ce, ce)[0]["r"] < 0.5


def test_taker_fill_uses_the_put_series_for_put_trades():
    n = 3
    ce = {"ask": np.array([200.0] * n), "bid": np.array([100.0] * n),
          "mid": np.array([150.0] * n)}
    pe = {"ask": np.array([101.0] * n), "bid": np.array([99.0] * n),
          "mid": np.array([100.0] * n)}
    trades = [{"entry_i": 0, "exit_i": 1, "opt": "PE", "r": 1.0, "stop_d": 10.0}]
    out = forward._apply_taker_fills(trades, ce, pe)
    assert out[0]["spread_r"] == pytest.approx(0.2), "must use PE spreads, not CE"


# ---------------- the gate ----------------

def test_gate_blocks_on_insufficient_coverage(monkeypatch):
    monkeypatch.setattr(forward, "recorder_report", lambda: {
        "snapshots": 100, "covered_days": 0.2, "span_days": 2.0,
        "largest_outage_hours": 38.0})
    g = forward.check_gate()
    assert g["ok"] is False
    assert any(name.startswith("coverage") and not ok for name, ok, _ in g["checks"])


def test_gate_blocks_on_a_long_outage(monkeypatch):
    monkeypatch.setattr(forward, "recorder_report", lambda: {
        "snapshots": 9000, "covered_days": 31.0, "span_days": 32.0,
        "largest_outage_hours": 40.0})
    assert forward.check_gate()["ok"] is False


def test_gate_blocks_on_swiss_cheese_coverage(monkeypatch):
    """30 days of coverage summed from a 90-day span is not 30 days of data."""
    monkeypatch.setattr(forward, "recorder_report", lambda: {
        "snapshots": 9000, "covered_days": 31.0, "span_days": 90.0,
        "largest_outage_hours": 5.0})
    assert forward.check_gate()["ok"] is False


def test_gate_opens_only_when_every_requirement_is_met(monkeypatch):
    monkeypatch.setattr(forward, "recorder_report", lambda: {
        "snapshots": 9000, "covered_days": 31.0, "span_days": 33.0,
        "largest_outage_hours": 5.0})
    assert forward.check_gate()["ok"] is True


def test_main_refuses_to_run_while_the_gate_is_closed(monkeypatch):
    monkeypatch.setattr(forward, "recorder_report", lambda: {
        "snapshots": 100, "covered_days": 0.2, "span_days": 2.0,
        "largest_outage_hours": 38.0})
    called = []
    monkeypatch.setattr(forward, "load_book", lambda *a, **k: called.append(1) or {})
    out = forward.main()
    assert out["ran"] is False
    assert not called, "the book must not even be loaded while the gate is closed"


def test_pre_registered_constants_are_unchanged():
    """These are fixed by PREREGISTRATION_FORWARD.md; drift voids the protocol."""
    assert forward.MIN_COVERAGE_DAYS == 30.0
    assert forward.MAX_OUTAGE_HOURS == 24.0
    assert forward.MAX_OUTAGE_FRACTION == 0.15
    assert forward.MIN_SNAPSHOTS == 6000
    assert forward.MIN_TRADES == 200
    assert len(forward.CANDIDATES) == 2
    assert all(c["family"] == "mean_reversion_bollinger" for c in forward.CANDIDATES)
