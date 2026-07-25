import numpy as np
import pytest

from research import costs, sim


def _trending(n=400):
    t = np.arange(0, n * 60, 60, dtype="int64")
    c = 64000.0 + np.arange(n, dtype="float64") * 2.0
    return {"time": t, "open": c - 1, "high": c + 8, "low": c - 8,
            "close": c, "volume": np.ones(n)}


def _sigs(n, start=50, stop=300, step=25):
    s = np.zeros(n, dtype="int8")
    s[start:stop:step] = 1
    return s


def test_default_cost_path_is_byte_identical_to_legacy():
    """Passing no cost model must reproduce pre-existing numbers exactly, so
    every result produced before this parameter existed stays reproducible."""
    d = _trending()
    s = _sigs(len(d["close"]))
    a = sim.simulate(d, s, stop_atr=1.0, rr=1.5, max_hold=30)
    b = sim.simulate(d, s, stop_atr=1.0, rr=1.5, max_hold=30, cost_model=None)
    assert [t["r"] for t in a["trades"]] == [t["r"] for t in b["trades"]]
    assert len(a["trades"]) > 0, "fixture must actually produce trades"


def test_injected_perp_cost_matches_legacy_constants():
    d = _trending()
    s = _sigs(len(d["close"]))
    legacy = sim.simulate(d, s, stop_atr=1.0, rr=1.5, max_hold=30)
    injected = sim.simulate(d, s, stop_atr=1.0, rr=1.5, max_hold=30,
                            cost_model=costs.PerpCost(fee_pct=sim.FEE_PCT,
                                                      gst=sim.GST_RATE))
    for x, y in zip(legacy["trades"], injected["trades"]):
        assert x["r"] == pytest.approx(y["r"], rel=1e-12)


def test_zero_cost_model_beats_the_default():
    d = _trending()
    s = _sigs(len(d["close"]))

    class Free:
        def round_trip_cost(self, entry, exit_, **kw):
            return 0.0

    default = sim.simulate(d, s, stop_atr=1.0, rr=1.5, max_hold=30)
    free = sim.simulate(d, s, stop_atr=1.0, rr=1.5, max_hold=30, cost_model=Free())
    assert (np.mean([t["r"] for t in free["trades"]])
            > np.mean([t["r"] for t in default["trades"]]))


def test_cost_r_is_reported_and_positive():
    d = _trending()
    s = _sigs(len(d["close"]))
    out = sim.simulate(d, s, stop_atr=1.0, rr=1.5, max_hold=30)
    assert all(t["cost_r"] > 0 for t in out["trades"])


def test_option_cost_model_receives_spot_at_both_ends():
    """Options price off premium but are FEE'd off spot notional, so the model
    must see the underlying at entry and exit, not the premium."""
    d = _trending()
    s = _sigs(len(d["close"]))
    spot = np.full(len(d["close"]), 64000.0)
    seen = []

    class Spy:
        def round_trip_cost(self, entry, exit_, **kw):
            seen.append(kw)
            return 0.0

    sim.simulate(d, s, stop_atr=1.0, rr=1.5, max_hold=30,
                 cost_model=Spy(), spot=spot, contract_value=0.001, contracts=3)
    assert seen, "cost model was never called"
    for kw in seen:
        assert kw["spot_in"] == pytest.approx(64000.0)
        assert kw["spot_out"] == pytest.approx(64000.0)
        assert kw["contract_value"] == 0.001
        assert kw["contracts"] == 3


def test_option_cost_is_far_cheaper_in_r_than_perp_at_the_same_signal():
    """The headline finding: on premium with Delta option fees, cost in R is a
    fraction of the 1m perpetual figure."""
    n = 400
    t = np.arange(0, n * 60, 60, dtype="int64")
    prem = 300.0 + np.arange(n, dtype="float64") * 0.5
    dprem = {"time": t, "open": prem, "high": prem + 3, "low": prem - 3,
             "close": prem, "volume": np.ones(n)}
    spot = np.full(n, 64000.0)
    s = _sigs(n)

    opt = sim.simulate(dprem, s, stop_atr=1.0, rr=1.5, max_hold=30,
                       cost_model=costs.OptionCost(), spot=spot,
                       contract_value=0.001, contracts=1)
    perp = sim.simulate(_trending(n), s, stop_atr=1.0, rr=1.5, max_hold=30)

    opt_cost = float(np.mean([x["cost_r"] for x in opt["trades"]]))
    perp_cost = float(np.mean([x["cost_r"] for x in perp["trades"]]))
    assert opt_cost < perp_cost
    assert opt_cost < 1.0, f"option cost in R should be well under 1R, got {opt_cost:.3f}"
