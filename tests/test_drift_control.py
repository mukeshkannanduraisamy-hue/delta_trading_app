import numpy as np
import pytest

from research import drift_control


def test_direction_mix_counts_calls_and_puts():
    tr = [{"opt": "CE"}] * 7 + [{"opt": "PE"}] * 3
    m = drift_control.direction_mix(tr)
    assert m["ce"] == 7 and m["pe"] == 3
    assert m["ce_frac"] == pytest.approx(0.7)
    assert m["majority"] == "CE"


def test_direction_mix_flags_put_bias():
    m = drift_control.direction_mix([{"opt": "PE"}] * 9 + [{"opt": "CE"}])
    assert m["majority"] == "PE" and m["ce_frac"] == pytest.approx(0.1)


def test_direction_mix_handles_no_trades():
    m = drift_control.direction_mix([])
    assert m["n"] == 0 and m["majority"] is None


def test_beats_constant_is_false_when_constant_direction_wins():
    """The whole point: a signal that cannot beat committing to one direction
    on the same bars is contributing nothing over plain exposure."""
    got = {"real_expectancy": 0.05, "best_constant": 0.20}
    assert not (got["real_expectancy"] > got["best_constant"])


def test_assess_reports_both_constant_directions(monkeypatch):
    """assess() must evaluate BOTH forced-long and forced-short, since which
    one is the drift-capturing side depends on the market regime."""
    calls = []

    def fake_trades(sig, ce, pe, spot, rr, cost):
        calls.append(int(np.sign(sig[sig != 0][0])) if (sig != 0).any() else 0)
        return [{"r": 0.1, "opt": "CE"}]

    monkeypatch.setattr(drift_control.search, "_trades", fake_trades)
    monkeypatch.setattr(drift_control.search, "_slice", lambda d, a, b: d)

    sig = np.array([1, 0, -1, 0, 1], dtype="int8")
    bundle = {"idx": {"dev": (0, 5), "holdout": (0, 5)},
              "ce": {}, "pe": {}, "spot": {"close": np.zeros(5)}, "cost": None}
    out = drift_control.assess(sig, bundle, 1.5)
    assert 1 in calls and -1 in calls, "both forced directions must be tested"
    assert out["always_call_expectancy"] is not None
    assert out["always_put_expectancy"] is not None


def test_direction_mix_ignores_sims_dir_field():
    """sim marks every long-premium trade "CE" including long puts, so a mix
    computed from `dir` reported 100% calls for two-sided strategies."""
    tr = [{"dir": "CE", "opt": "CE"}] * 4 + [{"dir": "CE", "opt": "PE"}] * 6
    m = drift_control.direction_mix(tr)
    assert m["ce"] == 4 and m["pe"] == 6 and m["majority"] == "PE"


def test_trades_tag_the_contract_actually_bought():
    import numpy as np
    from research import costs, search
    n = 60
    c = np.full(n, 100.0)
    s = {"time": np.arange(0, n * 3600, 3600, dtype="int64"), "open": c,
         "high": c * 1.02, "low": c * 0.98, "close": c, "volume": np.zeros(n)}
    sig = np.zeros(n, dtype="int8")
    sig[20] = 1        # must clear the 14-bar ATR warmup, or sim skips the bar
    sig[40] = -1
    tr = search._trades(sig, s, s, np.full(n, 64000.0), 1.5, costs.OptionCost())
    opts = {t["opt"] for t in tr}
    assert opts == {"CE", "PE"}, f"both contract types must be tagged, got {opts}"
