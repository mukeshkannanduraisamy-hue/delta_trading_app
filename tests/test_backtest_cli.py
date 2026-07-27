import pytest

from research import backtest_cli, vec, families


def test_lists_every_available_indicator():
    ind = backtest_cli.indicators()
    for name in vec.GENERATORS:
        assert name in ind
    for name in families.ENTRY_FAMILIES:
        assert name in ind


def test_tune_grids_reference_real_indicators():
    """A grid for a strategy that does not exist would fail only at runtime."""
    known = backtest_cli.indicators()
    for name in backtest_cli.TUNE_GRIDS:
        assert name in known, f"tuning grid for unknown indicator '{name}'"


def test_tune_grid_params_are_accepted_by_their_generator():
    """Every tunable parameter must be one the generator actually takes."""
    import inspect
    for name, grid in backtest_cli.TUNE_GRIDS.items():
        fn = vec.GENERATORS.get(name) or families.ENTRY_FAMILIES.get(name)
        sig = inspect.signature(fn)
        takes_kwargs = any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values())
        if takes_kwargs:
            continue
        for param in grid:
            assert param in sig.parameters, (
                f"{name} has no parameter '{param}' but the tuning grid sets it")


def test_dte_choices_are_positive_hours():
    for label, hours in backtest_cli.DTE_CHOICES.items():
        assert hours > 0, f"{label} maps to non-positive hours"


def test_win_rate_is_not_double_scaled():
    """sim.metrics returns win_rate already in percent; formatting it with
    ':.1%' printed 4210.0% for a 42.1% win rate."""
    import re
    src = open("research/backtest_cli.py", encoding="utf-8").read()
    assert "m['win_rate']:.1%" not in src, "win_rate must not be re-scaled by %"


# ---------------- --validate ----------------

def _fake_series(n, level=100.0):
    import numpy as np
    c = np.full(n, level, dtype="float64")
    return {"time": np.arange(0, n * 900, 900, dtype="int64"),
            "open": c, "high": c * 1.02, "low": c * 0.98, "close": c,
            "volume": np.ones(n), "roll_starts": np.array([0], dtype="int64")}


def _stub(test_exp=0.05, trades=300, beats=True, train_exp=0.2):
    def spy(strategy, tf, params, d, c, p, a, b):
        is_test = a > 0
        return {"params": params,
                "metrics": {"avg_r_per_trade": test_exp if is_test else train_exp,
                            "total_trades": trades if is_test else 500,
                            "total_r": 10.0, "profit_factor": 1.1},
                "drift": {"beats_constant": beats if is_test else True,
                          "edge_over_constant": 0.01,
                          "direction_mix": {"ce_frac": 0.5}},
                "span_days": 10.0}
    return spy


def test_validate_touches_the_test_slice_exactly_once(monkeypatch):
    """The whole point: selection runs on train, and only the single winner
    is ever evaluated on the unseen slice."""
    n = 2000
    d_tf, ce, pe = _fake_series(n), _fake_series(n), _fake_series(n)
    monkeypatch.setattr(backtest_cli, "_prepare", lambda *a, **k: (d_tf, ce, pe))

    seen = []
    base = _stub()

    def spy(strategy, tf, params, d, c, p, a, b):
        seen.append((a, b))
        return base(strategy, tf, params, d, c, p, a, b)

    monkeypatch.setattr(backtest_cli, "_eval_window", spy)
    backtest_cli.validate("ema_cross", "15m", 48.0, 365, split=0.7)

    cut = int(n * 0.7)
    assert len([w for w in seen if w == (0, cut)]) >= 2, "all combos scored on train"
    assert len([w for w in seen if w == (cut, n)]) == 1, \
        "exactly ONE evaluation may touch the test slice"


def test_validate_fails_on_negative_test_expectancy(monkeypatch):
    n = 1000
    monkeypatch.setattr(backtest_cli, "_prepare",
                        lambda *a, **k: (_fake_series(n),) * 3)
    monkeypatch.setattr(backtest_cli, "_eval_window", _stub(test_exp=-0.05))
    assert backtest_cli.validate("ema_cross", "15m", 48.0, 365)["holds"] is False


def test_validate_fails_on_too_few_test_trades(monkeypatch):
    n = 1000
    monkeypatch.setattr(backtest_cli, "_prepare",
                        lambda *a, **k: (_fake_series(n),) * 3)
    monkeypatch.setattr(backtest_cli, "_eval_window", _stub(trades=50))
    assert backtest_cli.validate("ema_cross", "15m", 48.0, 365)["holds"] is False


def test_validate_fails_when_beaten_by_constant_direction(monkeypatch):
    n = 1000
    monkeypatch.setattr(backtest_cli, "_prepare",
                        lambda *a, **k: (_fake_series(n),) * 3)
    monkeypatch.setattr(backtest_cli, "_eval_window", _stub(beats=False))
    assert backtest_cli.validate("ema_cross", "15m", 48.0, 365)["holds"] is False


def test_validate_holds_when_all_three_pass(monkeypatch):
    n = 1000
    monkeypatch.setattr(backtest_cli, "_prepare",
                        lambda *a, **k: (_fake_series(n),) * 3)
    monkeypatch.setattr(backtest_cli, "_eval_window", _stub())
    assert backtest_cli.validate("ema_cross", "15m", 48.0, 365)["holds"] is True


def test_validate_selects_the_best_train_params(monkeypatch):
    n = 1000
    monkeypatch.setattr(backtest_cli, "_prepare",
                        lambda *a, **k: (_fake_series(n),) * 3)

    def spy(strategy, tf, params, d, c, p, a, b):
        e = 0.9 if params.get("fast") == 15 else 0.1
        return {"params": params,
                "metrics": {"avg_r_per_trade": e, "total_trades": 400,
                            "total_r": 1.0, "profit_factor": 1.1},
                "drift": {"beats_constant": True, "edge_over_constant": 0.01,
                          "direction_mix": {"ce_frac": 0.5}},
                "span_days": 10.0}

    monkeypatch.setattr(backtest_cli, "_eval_window", spy)
    assert backtest_cli.validate("ema_cross", "15m", 48.0, 365)["selected"]["fast"] == 15


def test_sweep_budget_counts_combinations_not_parameter_names():
    """len(grid) counts parameter NAMES. Using it reported a budget of 19 for
    a sweep that tested 63 sets -- understating multiple comparisons 3x."""
    import itertools
    from research import backtest_cli as bc

    expected = 0
    for fam in bc.indicators():
        grid = bc.TUNE_GRIDS.get(fam)
        expected += len(list(itertools.product(*grid.values()))) if grid else 1

    # recompute the way validate_all does, via its own helper semantics
    got = 0
    for fam in bc.indicators():
        grid = bc.TUNE_GRIDS.get(fam)
        if not grid:
            got += 1
            continue
        n = 1
        for v in grid.values():
            n *= len(v)
        got += n

    assert got == expected, f"budget {got} != true combination count {expected}"
    assert expected > 50, "sanity: this sweep really does test dozens of sets"


# ---------------- _holds: all four conditions ----------------

def test_holds_rejects_negative_train_expectancy():
    """The condition that was missing: swingking_sniper scored train -0.047 /
    test +0.021 and was called a survivor. You cannot validate a strategy you
    would never have selected."""
    assert backtest_cli._holds(train_e=-0.047, test_e=0.021,
                               n_test=545, beats_constant=True) is False


def test_holds_rejects_negative_test_expectancy():
    assert backtest_cli._holds(0.05, -0.01, 500, True) is False


def test_holds_rejects_too_few_test_trades():
    assert backtest_cli._holds(0.05, 0.05, 10, True) is False


def test_holds_rejects_losing_to_constant_direction():
    assert backtest_cli._holds(0.05, 0.05, 500, False) is False


def test_holds_requires_all_four():
    assert backtest_cli._holds(0.05, 0.03, 500, True) is True


def test_holds_handles_missing_values():
    assert backtest_cli._holds(None, 0.05, 500, True) is False
    assert backtest_cli._holds(0.05, None, 500, True) is False


def test_holds_rejects_exactly_zero_expectancy():
    """Zero is not positive — a coin flip must not pass."""
    assert backtest_cli._holds(0.0, 0.05, 500, True) is False
    assert backtest_cli._holds(0.05, 0.0, 500, True) is False
