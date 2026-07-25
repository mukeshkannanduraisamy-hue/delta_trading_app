import numpy as np
import pytest

from research import gauntlet, sizing


def _ctx(**kw):
    base = {"trades": [{"r": 0.1} for _ in range(500)],
            "walk_forward": [0.05, 0.03, -0.01, 0.04],
            "shuffle_p": 0.01, "corrected_p": 0.02,
            "band_results": [0.02, 0.01, 0.03],
            "holdout_expectancy": 0.04, "search_budget": 3000}
    base.update(kw)
    return base


def test_min_trades_fails_below_threshold():
    ok, info = gauntlet.MinTrades(200).check(_ctx(trades=[{"r": 0.1}] * 199))
    assert ok is False and info["n"] == 199


def test_min_trades_passes_at_threshold():
    assert gauntlet.MinTrades(200).check(_ctx(trades=[{"r": 0.1}] * 200))[0]


def test_positive_expectancy_fails_on_negative():
    assert not gauntlet.PositiveExpectancy().check(_ctx(trades=[{"r": -0.5}] * 300))[0]


def test_positive_expectancy_fails_on_exactly_zero():
    assert not gauntlet.PositiveExpectancy().check(
        _ctx(trades=[{"r": 1.0}] * 150 + [{"r": -1.0}] * 150))[0]


def test_walk_forward_requires_three_of_four():
    assert gauntlet.WalkForward().check(_ctx(walk_forward=[0.1, 0.1, 0.1, -0.1]))[0]
    assert not gauntlet.WalkForward().check(_ctx(walk_forward=[0.1, 0.1, -0.1, -0.1]))[0]


def test_walk_forward_treats_none_window_as_failure():
    assert not gauntlet.WalkForward().check(_ctx(walk_forward=[0.1, 0.1, None, None]))[0]


def test_shuffle_control_rejects_high_p():
    assert not gauntlet.ShuffleControl().check(_ctx(shuffle_p=0.40))[0]


def test_missing_pvalues_fail_closed():
    """An absent statistic must never be treated as a pass."""
    assert not gauntlet.ShuffleControl().check(_ctx(shuffle_p=None))[0]
    assert not gauntlet.CorrectedSignificance().check(_ctx(corrected_p=None))[0]
    assert not gauntlet.FinalHoldout().check(_ctx(holdout_expectancy=None))[0]


def test_corrected_significance_rejects_high_p():
    assert not gauntlet.CorrectedSignificance().check(_ctx(corrected_p=0.20))[0]


def test_overlay_band_requires_every_perturbation_positive():
    assert not gauntlet.OverlayBand().check(_ctx(band_results=[0.02, -0.01, 0.03]))[0]
    assert gauntlet.OverlayBand().check(_ctx(band_results=[0.02, 0.01, 0.03]))[0]


def test_final_holdout_requires_positive():
    assert not gauntlet.FinalHoldout().check(_ctx(holdout_expectancy=-0.01))[0]


def test_run_reports_the_first_failing_criterion():
    res = gauntlet.run(_ctx(walk_forward=[0.1, -0.1, -0.1, -0.1]))
    assert res["passed"] is False and res["failed_at"] == "walk_forward"


def test_run_short_circuits_and_skips_later_criteria():
    res = gauntlet.run(_ctx(trades=[{"r": 0.1}] * 10))
    assert res["failed_at"] == "min_trades"
    assert "final_holdout" not in res["details"]


def test_run_passes_a_fully_qualifying_candidate():
    res = gauntlet.run(_ctx())
    assert res["passed"] is True and res["failed_at"] is None


def test_criteria_order_is_cheapest_first():
    assert gauntlet.ORDER[0] == "min_trades"
    assert gauntlet.ORDER[-1] == "final_holdout"


# ---------------- sizing ----------------

def test_kelly_is_zero_for_zero_edge():
    assert sizing.kelly_fraction([{"r": 1.0}, {"r": -1.0}] * 100) == pytest.approx(0.0, abs=1e-9)


def test_kelly_is_positive_for_positive_edge():
    assert sizing.kelly_fraction([{"r": 1.0}] * 60 + [{"r": -1.0}] * 40) > 0


def test_kelly_is_zero_for_negative_edge():
    assert sizing.kelly_fraction([{"r": 1.0}] * 40 + [{"r": -1.0}] * 60) == 0.0


def test_quarter_kelly_is_a_quarter_of_full():
    t = [{"r": 1.0}] * 60 + [{"r": -1.0}] * 40
    assert sizing.kelly_fraction(t, 0.25) == pytest.approx(sizing.kelly_fraction(t, 1.0) * 0.25)


def test_kelly_returns_zero_on_insufficient_trades():
    assert sizing.kelly_fraction([{"r": 1.0}] * 5) == 0.0


def test_unvalidated_strategy_gets_zero_size():
    """The structural guarantee: no gauntlet pass means no position."""
    t = [{"r": 1.0}] * 60 + [{"r": -1.0}] * 40
    rec = sizing.recommend(t, sizing.equity_curve(t), validated=False)
    assert rec["applied_f"] == 0.0 and rec["max_lot_pct"] == 0.0
    assert "not validated" in rec["rationale"].lower()


def test_drawdown_cap_shrinks_as_drawdown_grows():
    mild = np.array([100.0, 101, 102, 101, 103, 104])
    severe = np.array([100.0, 60, 90, 40, 80, 30])
    assert sizing.drawdown_derived_cap(severe) < sizing.drawdown_derived_cap(mild)


def test_drawdown_cap_stays_in_unit_range():
    for curve in (np.array([100.0, 1.0]), np.arange(1.0, 50.0)):
        assert 0.0 <= sizing.drawdown_derived_cap(curve) <= 1.0


def test_recommend_is_capped_by_drawdown_not_just_kelly():
    t = [{"r": 3.0}] * 60 + [{"r": -1.0}] * 40      # large edge -> large Kelly
    harsh = np.array([100.0, 20.0, 60.0, 10.0, 40.0])
    rec = sizing.recommend(t, harsh, validated=True)
    assert rec["applied_f"] <= rec["drawdown_cap"] + 1e-12


# ---------------- lazy evaluation must not change verdicts ----------------

def test_gauntlet_verdict_is_unchanged_by_missing_later_fields():
    """A candidate that dies early has no later statistics computed. The
    gauntlet must still return the SAME failure point it would have with
    those fields present -- laziness is a speed optimization, never a
    change to the bar."""
    dead = {"trades": [{"r": -0.5}] * 300, "search_budget": 540}
    lazy = gauntlet.run(dead)
    eager = gauntlet.run({**dead, "walk_forward": [0.1] * 4, "shuffle_p": 0.01,
                          "corrected_p": 0.01, "band_results": [0.02],
                          "holdout_expectancy": 0.03})
    assert lazy["failed_at"] == eager["failed_at"] == "positive_expectancy"


def test_sentinel_pass_values_do_not_mask_a_real_failure():
    """The PENDING sentinels in search.evaluate must let a genuine failure
    surface, not paper over it."""
    from research import search
    ctx = {"trades": [{"r": 0.1}] * 500, "search_budget": 540,
           "walk_forward": [0.1, -0.1, -0.1, -0.1]}   # real WF failure
    PENDING = {"walk_forward": [1.0] * 4, "shuffle_p": 0.0, "corrected_p": 0.0,
               "band_results": [1.0], "holdout_expectancy": 1.0}
    assert gauntlet.run({**PENDING, **ctx})["failed_at"] == "walk_forward"


def test_sentinels_allow_a_still_alive_candidate_through():
    PENDING = {"walk_forward": [1.0] * 4, "shuffle_p": 0.0, "corrected_p": 0.0,
               "band_results": [1.0], "holdout_expectancy": 1.0}
    ctx = {"trades": [{"r": 0.1}] * 500, "search_budget": 540}
    assert gauntlet.run({**PENDING, **ctx})["passed"] is True
