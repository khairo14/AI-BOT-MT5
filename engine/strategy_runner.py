"""
Strategy Runner / Dispatcher
Loads strategies from config/strategies.json + config/symbols.json,
fetches OHLCV data for each symbol, runs the assigned strategies,
validates signals through RiskManager, and emits them to either
auto-execution (OrderManager) or the manual-confirmation signal queue.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import MetaTrader5 as mt5
import pandas as pd
from loguru import logger

from engine.mt5_client import MT5Client
from engine.news_filter import news_filter
from engine.order_manager import OrderManager, OrderRequest
from engine.risk_manager import RiskManager
from engine.session_filter import session_filter
from engine.strategies.base_strategy import StrategyResult

# ── strategy imports ──────────────────────────────────────────────────────────
from engine.strategies.scalping.ema_scalp import EMAScalp
from engine.strategies.scalping.bb_squeeze import BBSqueeze
from engine.strategies.scalping.vwap_reversion import VWAPReversion
from engine.strategies.day_trading.macd_ema_trend import MACDEMATrend
from engine.strategies.day_trading.sr_breakout import SRBreakout
from engine.strategies.day_trading.rsi_divergence import RSIDivergence
from engine.strategies.swing.ema_trend_rider import EMATrendRider
from engine.strategies.swing.fibonacci_rsi import FibonacciRSI
from engine.strategies.swing.weekly_breakout import WeeklyBreakout

CONFIG_DIR = Path(__file__).parent.parent / "config"

STRATEGY_MAP = {
    "ema_scalp":        EMAScalp,
    "bb_squeeze":       BBSqueeze,
    "vwap_reversion":   VWAPReversion,
    "macd_ema_trend":   MACDEMATrend,
    "sr_breakout":      SRBreakout,
    "rsi_divergence":   RSIDivergence,
    "ema_trend_rider":  EMATrendRider,
    "fibonacci_rsi":    FibonacciRSI,
    "weekly_breakout":  WeeklyBreakout,
}

# Timeframes required per strategy (primary timeframe → bars to fetch)
TIMEFRAME_BARS: dict[str, dict[str, int]] = {
    "ema_scalp":       {"M1": 100, "M5": 100},
    "bb_squeeze":      {"M5": 100},
    "vwap_reversion":  {"M5": 200},
    "macd_ema_trend":  {"H1": 220, "M15": 200},
    "sr_breakout":     {"H1": 150},
    "rsi_divergence":  {"M30": 100, "H1": 100},
    "ema_trend_rider": {"H1": 250, "H4": 100, "D1": 60},
    "fibonacci_rsi":   {"H4": 100},
    "weekly_breakout": {"H4": 80, "D1": 30},
}

MT5_TF = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
    "W1":  mt5.TIMEFRAME_W1,
}


@dataclass
class StrategySignal:
    """Pending signal waiting for execution or manual confirmation."""
    trading_type: str
    symbol: str
    strategy: str
    direction: str
    entry_price: float
    sl_price: float
    tp_price: float | None
    tp2_price: float | None
    lot_size: float
    comment: str
    timeframe: str = ""  # primary timeframe from the strategy class (e.g. "M5", "H1")
    confidence: float = 0.0  # 0–1 score from AI scorer (Phase 6)
    indicators: dict[str, Any] = field(default_factory=dict)
    approved: bool = False  # set to True when user confirms (manual mode)


class StrategyRunner:

    def __init__(
        self,
        client: MT5Client,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        execution_mode: str = "manual",  # "manual" | "auto"
    ):
        self.client = client
        self.order_manager = order_manager
        self.risk_manager = risk_manager
        self.execution_mode = execution_mode

        self._strategies_cfg: dict = self._load_json("strategies.json")
        self._symbols_cfg: dict    = self._load_json("symbols.json")

        # Pending signals for manual confirmation (populated when mode == "manual")
        self.pending_signals: list[StrategySignal] = []

    # ── public interface ──────────────────────────────────────────────────────

    def run_all(self) -> list[StrategySignal]:
        """Run all enabled symbols through their assigned strategies.
        Returns any new signals generated this iteration."""
        new_signals: list[StrategySignal] = []
        for trading_type in ("scalping", "day_trading", "swing"):
            symbols = self._enabled_symbols(trading_type)
            active_strategies = self._active_strategies(trading_type)
            for symbol in symbols:
                per_symbol_strats = self._per_symbol_overrides(trading_type, symbol) or active_strategies
                for strat_name in per_symbol_strats:
                    sig = self._run_strategy(trading_type, symbol, strat_name)
                    if sig:
                        new_signals.append(sig)
                        if self.execution_mode == "auto":
                            self._execute(sig)
                        else:
                            self.pending_signals.append(sig)
        return new_signals

    def confirm_signal(self, signal_index: int) -> bool:
        """Approve and execute a pending signal by index. Returns True on success."""
        if signal_index >= len(self.pending_signals):
            logger.warning(f"confirm_signal: index {signal_index} out of range")
            return False
        sig = self.pending_signals.pop(signal_index)
        sig.approved = True
        return self._execute(sig)

    def reject_signal(self, signal_index: int) -> None:
        """Remove a pending signal without executing."""
        if signal_index < len(self.pending_signals):
            removed = self.pending_signals.pop(signal_index)
            logger.info(f"Signal rejected: {removed.strategy} {removed.symbol} {removed.direction}")

    # ── internal ──────────────────────────────────────────────────────────────

    def _run_strategy(
        self,
        trading_type: str,
        symbol: str,
        strat_name: str,
    ) -> StrategySignal | None:
        if strat_name not in STRATEGY_MAP:
            logger.warning(f"Unknown strategy: {strat_name}")
            return None

        strat_cls = STRATEGY_MAP[strat_name]
        params    = self._strategy_params(strat_name, symbol)
        strategy  = strat_cls(symbol=symbol, params=params)

        # Fetch all required timeframes
        tf_data   = self._fetch_timeframes(symbol, strat_name)
        if not tf_data:
            return None

        # Call strategy with appropriate dataframe arguments
        try:
            result = self._dispatch(strategy, strat_name, tf_data)
        except Exception as exc:
            logger.exception(f"Strategy {strat_name} raised on {symbol}: {exc}")
            return None

        if result.signal is None or not result.signal.is_actionable:
            return None

        sig = result.signal

        # Risk validation
        account = self.client.get_account_info()
        if account is None:
            return None

        balance = account.get("balance", 0.0)
        self.risk_manager.update_balance(balance)

        sl_ok, _err = self.risk_manager.validate_sl_tp(
            direction=sig.direction,
            entry_price=sig.entry_price,
            sl_price=sig.sl_price,
            tp_price=sig.tp_price,
        )
        if not sl_ok:
            logger.debug(f"{strat_name}/{symbol}: R:R validation failed — signal skipped")
            return None

        risk_allowed, risk_reason = self.risk_manager.is_trading_allowed(trading_type)
        if not risk_allowed:
            logger.info(f"Trading halted for {trading_type} — {risk_reason} ({symbol})")
            return None

        # News filter gate
        news_blocked, news_reason = news_filter.is_blocked(symbol, trading_type)
        if news_blocked:
            logger.debug(f"News filter blocked {symbol}: {news_reason}")
            return None

        # Session filter gate
        sym_category = session_filter.category_for(symbol)
        sess_open, sess_reason = session_filter.is_open(symbol, sym_category)
        if not sess_open:
            logger.debug(f"Session filter blocked {symbol}: {sess_reason}")
            return None

        sym_info = self.client.get_symbol_info(symbol)
        if sym_info is None:
            return None

        lot = self.risk_manager.calculate_lot_size(
            balance=balance,
            entry=sig.entry_price,
            sl=sig.sl_price,
            symbol=symbol,
            contract_size=sym_info.get("contract_size", 100_000),
            tick_value=sym_info.get("pip_value", 1.0),
            tick_size=sym_info.get("tick_size", 0.00001),
        )

        # Apply RL agent risk-factor multiplier (1.0 = neutral, 0.5–1.5 range).
        # The RL agent learns whether to scale position size up or down based on
        # recent win rate and confidence — this is how it feeds back into live sizing.
        try:
            from ai.rl_agent import rl_manager as _rl
            rf = _rl.risk_factor(trading_type)
            if rf != 1.0:
                min_lot = sym_info.get("min_lot", 0.01)
                lot_step = sym_info.get("lot_step", 0.01)
                lot = max(min_lot, round(round(lot * rf / lot_step) * lot_step, 2))
        except Exception:
            pass  # RL not available — use raw lot as-is

        strat_sig = StrategySignal(
            trading_type=trading_type,
            symbol=symbol,
            strategy=strat_name,
            direction=sig.direction,
            entry_price=sig.entry_price,
            sl_price=sig.sl_price,
            tp_price=sig.tp_price,
            tp2_price=sig.tp2_price,
            lot_size=lot,
            timeframe=sig.timeframe,
            comment=sig.comment,
            indicators=result.indicators,
        )

        # Phase 6: score signal confidence (safe — degrades to 0.5 if AI not ready)
        try:
            from ai.signal_scorer import scorer
            # Use the strategy's own primary TF for the scorer so EMA50/200 and
            # LSTM signal are computed on the correct timeframe, not whichever TF
            # happens to be first in the dict.
            _PRIMARY_TF = {
                "ema_scalp":       "M1",
                "macd_ema_trend":  "M15",
                "rsi_divergence":  "M30",
                "ema_trend_rider": "H1",
                "bb_squeeze":      "M5",
                "vwap_reversion":  "M5",
                "sr_breakout":     "H1",
                "fibonacci_rsi":   "H4",
                "weekly_breakout": "H4",
            }
            _ptf = _PRIMARY_TF.get(strat_name)
            primary_df = tf_data.get(_ptf, next(iter(tf_data.values()))) if _ptf else next(iter(tf_data.values()))
            strat_sig.confidence = scorer.score(
                symbol=symbol,
                direction=sig.direction,
                entry=sig.entry_price,
                sl=sig.sl_price,
                tp=sig.tp_price or sig.entry_price,
                df=primary_df,
                trading_type=trading_type,
            )
            # AI/ML confidence filter (enabled via Settings → AI → confidence_filter_enabled)
            try:
                _ai = json.loads((CONFIG_DIR / "app.json").read_text()).get("ai", {})
                if _ai.get("confidence_filter_enabled"):
                    _threshold = float(_ai.get("confidence_threshold", 60)) / 100.0
                    if strat_sig.confidence < _threshold:
                        logger.debug(
                            f"AI confidence gate blocked {strat_name}/{symbol}: "
                            f"{strat_sig.confidence:.2f} < {_threshold:.2f}"
                        )
                        return None
            except Exception:
                pass
            # RL gate — suppress low-confidence signals dynamically
            if not scorer.is_tradeable(strat_sig.confidence, trading_type):
                logger.debug(
                    f"RL gate blocked {strat_name}/{symbol}: "
                    f"confidence={strat_sig.confidence:.2f}"
                )
                return None
        except Exception as _exc:
            logger.debug(f"Signal scorer skipped for {symbol}: {_exc}")

        return strat_sig

    def run_mode(self, trading_type: str, symbols_override: list[str] | None = None) -> list[StrategySignal]:
        """Run all enabled symbols for a single trading type. Used by the runner loop.
        If symbols_override is provided (from scanner config), only those symbols are scanned."""
        # Reload configs on each run so dashboard changes take effect without restart
        self._strategies_cfg = self._load_json("strategies.json")
        self._symbols_cfg    = self._load_json("symbols.json")
        new_signals: list[StrategySignal] = []
        symbols = symbols_override if symbols_override is not None else self._enabled_symbols(trading_type)
        active_strategies = self._active_strategies(trading_type)
        for symbol in symbols:
            per_symbol_strats = self._per_symbol_overrides(trading_type, symbol) or active_strategies
            candidates: list[StrategySignal] = []
            for strat_name in per_symbol_strats:
                sig = self._run_strategy(trading_type, symbol, strat_name)
                if sig:
                    candidates.append(sig)
            if candidates:
                # Pick highest-confidence signal; if tied, first one wins
                best = max(candidates, key=lambda s: s.confidence)
                logger.debug(
                    f"Best signal for {trading_type}/{symbol}: {best.strategy} "
                    f"(conf={best.confidence:.2f}, considered {len(candidates)})"
                )
                new_signals.append(best)
        return new_signals

    def _execute(self, sig: StrategySignal) -> bool:
        open_positions = self.client.get_open_positions()
        allowed, _reason = self.risk_manager.check_concurrent_limit(
            sig.trading_type, open_positions, symbol=sig.symbol
        )
        if not allowed:
            logger.info(f"Concurrent limit reached for {sig.trading_type}/{sig.symbol}: {_reason}")
            return False

        req = OrderRequest(
            symbol=sig.symbol,
            direction=sig.direction,
            volume=sig.lot_size,
            sl=sig.sl_price,
            tp=sig.tp_price,
            comment=sig.comment,
        )
        result = self.order_manager.place_market_order(req)
        if result and result.success:
            logger.info(
                f"Order placed: {sig.strategy}/{sig.symbol} {sig.direction} "
                f"lot={sig.lot_size} sl={sig.sl_price} tp={sig.tp_price} ticket={result.ticket}"
            )
            try:
                from engine.trade_journal import trade_journal
                from engine.account_store import current_mode
                trade_journal.log(
                    ticket=result.ticket or 0,
                    symbol=sig.symbol,
                    direction=sig.direction,
                    volume=sig.lot_size,
                    entry=result.open_price or sig.entry_price,
                    sl=sig.sl_price,
                    tp=sig.tp_price,
                    profit=None,
                    trading_type=sig.trading_type,
                    account_mode=current_mode(),
                    comment=sig.comment,
                    event="open",
                )
            except Exception as _je:
                logger.warning(f"Journal write failed for {sig.strategy}/{sig.symbol}: {_je}")
            return True
        else:
            logger.warning(f"Order failed: {sig.strategy}/{sig.symbol} {sig.direction}")
            return False

    def _fetch_timeframes(self, symbol: str, strat_name: str) -> dict[str, pd.DataFrame]:
        tf_spec = TIMEFRAME_BARS.get(strat_name, {})
        result: dict[str, pd.DataFrame] = {}
        for tf_str, bars in tf_spec.items():
            df = self.client.get_ohlcv(symbol, tf_str, bars)
            if df is None or df.empty:
                logger.debug(f"No data for {symbol} {tf_str}")
                return {}  # abort — required data unavailable
            result[tf_str] = df
        return result

    def _dispatch(
        self,
        strategy: Any,
        strat_name: str,
        tf_data: dict[str, pd.DataFrame],
    ) -> StrategyResult:
        """Call each strategy's calculate() with the right keyword arguments."""
        if strat_name == "ema_scalp":
            return strategy.calculate(tf_data["M1"], df_m5=tf_data.get("M5"))
        if strat_name == "macd_ema_trend":
            # M15 is the primary entry TF; H1 is bias/signal confirmation
            return strategy.calculate(tf_data["M15"], df_h1=tf_data.get("H1"))
        if strat_name == "rsi_divergence":
            return strategy.calculate(tf_data["M30"], df_h1=tf_data.get("H1"))
        if strat_name == "ema_trend_rider":
            return strategy.calculate(tf_data["H1"], df_h4=tf_data.get("H4"), df_d1=tf_data.get("D1"))
        if strat_name == "weekly_breakout":
            return strategy.calculate(tf_data["H4"], df_daily=tf_data.get("D1"))
        # default: single dataframe using the first TF
        primary_tf = next(iter(tf_data))
        return strategy.calculate(tf_data[primary_tf])

    # ── config helpers ────────────────────────────────────────────────────────

    def _load_json(self, filename: str) -> dict:
        path = CONFIG_DIR / filename
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)

    def _enabled_symbols(self, trading_type: str) -> list[str]:
        symbols = self._symbols_cfg.get(trading_type, [])
        if isinstance(symbols, list):
            return [s["symbol"] for s in symbols if s.get("enabled", False)]
        # legacy dict format
        return [s for s, cfg in symbols.items() if cfg.get("enabled", False)]

    def _active_strategies(self, trading_type: str) -> list[str]:
        return self._strategies_cfg.get(trading_type, {}).get("active_strategies", [])

    def _per_symbol_overrides(self, trading_type: str, symbol: str) -> list[str] | None:
        return (
            self._strategies_cfg
            .get(trading_type, {})
            .get("symbol_strategy_override", {})
            .get(symbol)
        )

    def _strategy_params(self, strat_name: str, symbol: str = "") -> dict:
        """Return merged params: strategies.json defaults + optimized overrides."""
        # Base params: search per-mode params in strategies.json
        base: dict = {}
        for mode_cfg in self._strategies_cfg.values():
            if isinstance(mode_cfg, dict):
                p = mode_cfg.get("params", {}).get(strat_name)
                if p:
                    base = dict(p)
                    break

        # Optimized params from param_optimizer (per-symbol > global > none)
        try:
            from ai.param_optimizer import optimizer
            opt = optimizer.get_params(strat_name, symbol)
            if opt:
                return {**base, **opt}
        except Exception:
            pass
        return base
