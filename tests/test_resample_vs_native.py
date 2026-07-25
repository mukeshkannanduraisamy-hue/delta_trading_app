"""Resampled 5m must track Delta's own 5m.

A divergence means the bucket alignment is wrong, and every multi-timeframe
result in the study would be built on bars the exchange never printed.
Uses the cached 90d series from the earlier study, so this costs no network.
"""
import numpy as np
import pytest

from research import data


def test_resampled_5m_matches_native_5m():
    d1 = data.fetch("BTCUSD", "1m", 90)
    d5 = data.fetch("BTCUSD", "5m", 90)
    got = data.resample(d1, "1m", "5m")

    common, i_got, i_nat = np.intersect1d(got["time"], d5["time"], return_indices=True)
    assert len(common) > 1000, f"too few overlapping bars to compare: {len(common)}"

    for field in ("open", "high", "low", "close"):
        a, b = got[field][i_got], d5[field][i_nat]
        rate = float((np.abs(a - b) > 0.51).mean())      # BTCUSD ticks at 0.5
        assert rate < 0.02, f"{field}: {rate:.2%} of resampled bars diverge from native"
