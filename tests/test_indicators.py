"""
test_indicators.py – Unit tests for the Indicators class.
"""

import numpy as np
import pandas as pd
import pytest

from src.indicators import Indicators


@pytest.fixture
def ohlcv_df():
    """Generate a synthetic OHLCV DataFrame with 200 rows."""
    rng = np.random.default_rng(42)
    n = 200
    close = 1.1000 + np.cumsum(rng.normal(0, 0.0005, n))
    high = close + rng.uniform(0.0001, 0.001, n)
    low = close - rng.uniform(0.0001, 0.001, n)
    open_ = close - rng.normal(0, 0.0002, n)
    volume = rng.integers(100, 1000, n).astype(float)
    idx = pd.date_range("2024-01-01", periods=n, freq="15min")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "tick_volume": volume},
        index=idx,
    )


class TestRSI:
    def test_length(self, ohlcv_df):
        ind = Indicators()
        result = ind.rsi(ohlcv_df["close"])
        assert len(result) == len(ohlcv_df)

    def test_range(self, ohlcv_df):
        ind = Indicators()
        result = ind.rsi(ohlcv_df["close"]).dropna()
        assert (result >= 0).all() and (result <= 100).all()

    def test_series_name(self, ohlcv_df):
        ind = Indicators()
        assert ind.rsi(ohlcv_df["close"]).name == "rsi"


class TestMACD:
    def test_returns_three_series(self, ohlcv_df):
        ind = Indicators()
        macd, signal, hist = ind.macd(ohlcv_df["close"])
        assert len(macd) == len(ohlcv_df)
        assert len(signal) == len(ohlcv_df)
        assert len(hist) == len(ohlcv_df)

    def test_histogram_equals_macd_minus_signal(self, ohlcv_df):
        ind = Indicators()
        macd, signal, hist = ind.macd(ohlcv_df["close"])
        pd.testing.assert_series_equal(hist, (macd - signal).rename("macd_hist"))


class TestBollingerBands:
    def test_upper_above_lower(self, ohlcv_df):
        ind = Indicators()
        upper, middle, lower = ind.bollinger_bands(ohlcv_df["close"])
        valid = upper.dropna()
        assert (upper.dropna() >= lower.dropna()).all()

    def test_middle_is_sma(self, ohlcv_df):
        ind = Indicators(bb_period=20)
        _, middle, _ = ind.bollinger_bands(ohlcv_df["close"])
        expected = ohlcv_df["close"].rolling(20).mean()
        pd.testing.assert_series_equal(middle.rename(expected.name), expected, check_names=False)


class TestATR:
    def test_non_negative(self, ohlcv_df):
        ind = Indicators()
        atr = ind.atr(ohlcv_df["high"], ohlcv_df["low"], ohlcv_df["close"]).dropna()
        assert (atr >= 0).all()

    def test_length(self, ohlcv_df):
        ind = Indicators()
        atr = ind.atr(ohlcv_df["high"], ohlcv_df["low"], ohlcv_df["close"])
        assert len(atr) == len(ohlcv_df)


class TestStochastic:
    def test_range(self, ohlcv_df):
        ind = Indicators()
        k, d = ind.stochastic(ohlcv_df["high"], ohlcv_df["low"], ohlcv_df["close"])
        assert (k.dropna() >= 0).all() and (k.dropna() <= 100).all()

    def test_length(self, ohlcv_df):
        ind = Indicators()
        k, d = ind.stochastic(ohlcv_df["high"], ohlcv_df["low"], ohlcv_df["close"])
        assert len(k) == len(ohlcv_df)


class TestComputeAll:
    def test_columns_present(self, ohlcv_df):
        ind = Indicators()
        result = ind.compute_all(ohlcv_df)
        expected_cols = {
            "rsi", "macd", "macd_signal", "macd_hist",
            "bb_upper", "bb_middle", "bb_lower",
            "ema_fast", "ema_slow",
            "atr", "stoch_k", "stoch_d",
        }
        assert expected_cols.issubset(set(result.columns))

    def test_missing_required_column_raises(self, ohlcv_df):
        ind = Indicators()
        with pytest.raises(ValueError, match="missing required columns"):
            ind.compute_all(ohlcv_df.drop(columns=["close"]))

    def test_volume_column_mapped(self, ohlcv_df):
        ind = Indicators()
        result = ind.compute_all(ohlcv_df)
        assert "volume" in result.columns
