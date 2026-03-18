"""
test_strategy.py – Unit tests for the TradingStrategy class.
"""

import numpy as np
import pandas as pd
import pytest

from src.strategy import TradingStrategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD


@pytest.fixture
def ohlcv_df():
    """Generate a 600-row synthetic OHLCV DataFrame for training/prediction."""
    rng = np.random.default_rng(0)
    n = 600
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


@pytest.fixture
def strategy(tmp_path):
    features = [
        "rsi", "macd", "macd_signal", "bb_upper", "bb_middle", "bb_lower",
        "ema_fast", "ema_slow", "atr", "stoch_k", "stoch_d", "close",
    ]
    return TradingStrategy(
        features=features,
        prediction_threshold=0.0,  # Accept all signals for testing
        train_lookback=500,
        retrain_interval_hours=24,
        model_dir=str(tmp_path),
        model_file="test_model.joblib",
    )


class TestTrain:
    def test_pipeline_fitted_after_train(self, strategy, ohlcv_df):
        strategy.train(ohlcv_df)
        assert strategy._pipeline is not None

    def test_skip_train_with_insufficient_data(self, strategy):
        tiny_df = pd.DataFrame(
            {"open": [1.1] * 10, "high": [1.2] * 10, "low": [1.0] * 10, "close": [1.1] * 10}
        )
        strategy.train(tiny_df)
        assert strategy._pipeline is None


class TestPredict:
    def test_returns_hold_without_model(self, strategy, ohlcv_df):
        signal, conf = strategy.predict(ohlcv_df)
        assert signal == SIGNAL_HOLD
        assert conf == 0.0

    def test_predict_after_train(self, strategy, ohlcv_df):
        strategy.train(ohlcv_df)
        signal, conf = strategy.predict(ohlcv_df)
        assert signal in (SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD)
        assert 0.0 <= conf <= 1.0

    def test_low_confidence_returns_hold(self, strategy, ohlcv_df):
        """Set threshold above 1.0 so any prediction is held."""
        strategy.prediction_threshold = 1.01
        strategy.train(ohlcv_df)
        signal, _ = strategy.predict(ohlcv_df)
        assert signal == SIGNAL_HOLD


class TestNeedsRetraining:
    def test_needs_retraining_before_train(self, strategy):
        assert strategy.needs_retraining() is True

    def test_no_retraining_immediately_after_train(self, strategy, ohlcv_df):
        strategy.train(ohlcv_df)
        assert strategy.needs_retraining() is False


class TestModelPersistence:
    def test_save_and_load(self, strategy, ohlcv_df, tmp_path):
        strategy.train(ohlcv_df)
        assert strategy.load_model() is True

    def test_load_returns_false_without_file(self, tmp_path):
        strat = TradingStrategy(
            features=["close"],
            model_dir=str(tmp_path),
            model_file="nonexistent.joblib",
        )
        assert strat.load_model() is False
