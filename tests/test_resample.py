import numpy as np
import pytest

from research import data


def _series(n, start=0, step=60):
    """n bars of synthetic 1m OHLCV with a known shape."""
    t = np.arange(start, start + n * step, step, dtype="int64")
    close = np.arange(1, n + 1, dtype="float64") * 10.0
    return {
        "time": t,
        "open": close - 2.0,
        "high": close + 5.0,
        "low": close - 5.0,
        "close": close,
        "volume": np.ones(n, dtype="float64"),
    }


def test_resample_aggregates_ohlcv_correctly():
    d = _series(10)
    out = data.resample(d, "1m", "5m")
    assert len(out["time"]) == 2
    # bucket 0 = bars 0..4, bucket 1 = bars 5..9
    assert out["open"][0] == d["open"][0]
    assert out["close"][0] == d["close"][4]
    assert out["high"][0] == d["high"][:5].max()
    assert out["low"][0] == d["low"][:5].min()
    assert out["volume"][0] == 5.0
    assert out["time"][0] == d["time"][0]


def test_resample_drops_incomplete_trailing_bucket():
    d = _series(7)  # 5 complete + 2 leftover
    out = data.resample(d, "1m", "5m")
    assert len(out["time"]) == 1, "partial trailing bucket must be dropped"


def test_resample_buckets_align_to_epoch_boundaries():
    # start at 03:02 UTC so the first bar is mid-bucket
    d = _series(20, start=3 * 3600 + 2 * 60)
    out = data.resample(d, "1m", "5m")
    assert np.all(out["time"] % 300 == 0), "bucket starts must align to the resolution grid"


def test_resample_rejects_downsample_to_finer():
    d = _series(10)
    with pytest.raises(ValueError):
        data.resample(d, "5m", "1m")


def test_resample_identity_returns_a_copy():
    d = _series(10)
    out = data.resample(d, "1m", "1m")
    assert np.array_equal(out["close"], d["close"])
    out["close"][0] = -1.0
    assert d["close"][0] != -1.0, "identity resample must not alias the input"


def test_resample_handles_empty_series():
    empty = {k: v[:0] for k, v in _series(3).items()}
    out = data.resample(empty, "1m", "5m")
    assert len(out["time"]) == 0
