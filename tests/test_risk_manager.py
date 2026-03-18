"""
test_risk_manager.py – Unit tests for the RiskManager class.
"""

import pytest

from src.risk_manager import RiskManager


@pytest.fixture
def rm():
    return RiskManager(
        risk_per_trade=0.01,
        max_trades=3,
        stop_loss_pips=30,
        take_profit_pips=60,
        max_daily_loss=0.05,
    )


class TestInitValidation:
    def test_invalid_risk_per_trade_zero(self):
        with pytest.raises(ValueError):
            RiskManager(risk_per_trade=0.0)

    def test_invalid_risk_per_trade_one(self):
        with pytest.raises(ValueError):
            RiskManager(risk_per_trade=1.0)

    def test_invalid_max_trades(self):
        with pytest.raises(ValueError):
            RiskManager(max_trades=0)


class TestCanOpenTrade:
    def test_allows_trade_initially(self, rm):
        assert rm.can_open_trade(10_000.0) is True

    def test_blocks_when_max_trades_reached(self, rm):
        rm._open_trades = 3
        assert rm.can_open_trade(10_000.0) is False

    def test_blocks_when_daily_loss_exceeded(self, rm):
        rm._daily_loss = 600.0  # 6% of 10 000
        assert rm.can_open_trade(10_000.0) is False

    def test_allows_when_just_under_daily_limit(self, rm):
        rm._daily_loss = 499.0  # 4.99% of 10 000
        assert rm.can_open_trade(10_000.0) is True


class TestCalculateLotSize:
    def test_basic_calculation(self, rm):
        # balance=10000, risk=1%, SL=30 pips, pip_value=10
        # lot = (10000 * 0.01) / (30 * 10) = 100 / 300 ≈ 0.33
        lot = rm.calculate_lot_size(10_000.0, 10.0)
        assert abs(lot - 0.33) < 0.01

    def test_minimum_lot(self, rm):
        lot = rm.calculate_lot_size(100.0, 10.0)
        assert lot >= 0.01

    def test_symbol_info_clamps(self, rm):
        class FakeSymbolInfo:
            volume_min = 0.01
            volume_max = 0.10
            volume_step = 0.01

        lot = rm.calculate_lot_size(10_000.0, 10.0, FakeSymbolInfo())
        assert lot <= 0.10


class TestCalculateSlTp:
    def test_buy_signal(self, rm):
        sl, tp = rm.calculate_sl_tp(1.1000, signal=1, pip_size=0.0001)
        assert sl < 1.1000  # SL below entry for BUY
        assert tp > 1.1000  # TP above entry for BUY

    def test_sell_signal(self, rm):
        sl, tp = rm.calculate_sl_tp(1.1000, signal=-1, pip_size=0.0001)
        assert sl > 1.1000  # SL above entry for SELL
        assert tp < 1.1000  # TP below entry for SELL

    def test_sl_distance_correct(self, rm):
        # SL should be 30 pips away
        sl, _ = rm.calculate_sl_tp(1.1000, signal=1, pip_size=0.0001)
        assert abs((1.1000 - sl) - 30 * 0.0001) < 1e-7

    def test_tp_distance_correct(self, rm):
        # TP should be 60 pips away
        _, tp = rm.calculate_sl_tp(1.1000, signal=1, pip_size=0.0001)
        assert abs((tp - 1.1000) - 60 * 0.0001) < 1e-7


class TestDailyLossTracking:
    def test_record_loss_increases_daily_loss(self, rm):
        rm.record_trade_result(-100.0)
        assert rm._daily_loss == 100.0

    def test_record_profit_does_not_increase_daily_loss(self, rm):
        rm.record_trade_result(200.0)
        assert rm._daily_loss == 0.0

    def test_on_trade_opened_increments(self, rm):
        rm.on_trade_opened()
        assert rm._open_trades == 1

    def test_record_trade_result_decrements_open_trades(self, rm):
        rm._open_trades = 2
        rm.record_trade_result(-50.0)
        assert rm._open_trades == 1
