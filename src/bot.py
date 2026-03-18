"""
bot.py – Main trading bot loop.

Usage:
    python -m src.bot
    python -m src.bot --config path/to/config.yaml
"""

from __future__ import annotations

import argparse
import signal
import time
from typing import Optional

from src.config import load_config
from src.data_fetcher import DataFetcher
from src.logger import get_logger, setup_logger
from src.mt5_connector import MT5Connector
from src.order_manager import OrderManager
from src.risk_manager import RiskManager
from src.strategy import TradingStrategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD

logger = get_logger("bot")

_RUNNING = True


def _handle_sigterm(signum, frame) -> None:
    global _RUNNING
    logger.info("Shutdown signal received. Stopping bot after current iteration.")
    _RUNNING = False


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


class TradingBot:
    """Orchestrates data fetching, signal generation, and order management."""

    def __init__(self, config_path: Optional[str] = None) -> None:
        self.cfg = load_config(config_path) if config_path else load_config()
        self._setup_logging()

        mt5_cfg = self.cfg["mt5"]
        self.connector = MT5Connector(
            login=mt5_cfg["login"],
            password=mt5_cfg["password"],
            server=mt5_cfg["server"],
            timeout=mt5_cfg.get("timeout", 60_000),
            portable=mt5_cfg.get("portable", False),
        )

        trade_cfg = self.cfg["trading"]
        self.symbol = trade_cfg["symbol"]
        self.timeframe = trade_cfg["timeframe"]
        self.magic_number = trade_cfg["magic_number"]

        self.data_fetcher = DataFetcher(self.symbol, self.timeframe)

        risk_cfg = self.cfg["risk"]
        self.risk_manager = RiskManager(
            risk_per_trade=risk_cfg["risk_per_trade"],
            max_trades=risk_cfg["max_trades"],
            stop_loss_pips=risk_cfg["stop_loss_pips"],
            take_profit_pips=risk_cfg["take_profit_pips"],
            max_daily_loss=risk_cfg["max_daily_loss"],
            trailing_stop=risk_cfg.get("trailing_stop", False),
            trailing_stop_pips=risk_cfg.get("trailing_stop_pips", 15),
        )

        self.order_manager = OrderManager(
            symbol=self.symbol,
            magic_number=self.magic_number,
            comment=trade_cfg.get("comment", "AI-BOT-MT5"),
        )

        strat_cfg = self.cfg["strategy"]
        self.strategy = TradingStrategy(
            features=strat_cfg["features"],
            prediction_threshold=strat_cfg["prediction_threshold"],
            train_lookback=strat_cfg["train_lookback"],
            retrain_interval_hours=strat_cfg["retrain_interval"],
            model_dir=self.cfg["model"]["model_dir"],
            model_file=self.cfg["model"]["model_file"],
            indicator_params=self.cfg.get("indicators", {}),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _setup_logging(self) -> None:
        log_cfg = self.cfg["logging"]
        setup_logger(
            name="ai-bot-mt5",
            level=log_cfg.get("level", "INFO"),
            log_dir=log_cfg.get("log_dir", "logs"),
            log_file=log_cfg.get("log_file", "bot.log"),
            max_bytes=log_cfg.get("max_bytes", 5 * 1024 * 1024),
            backup_count=log_cfg.get("backup_count", 5),
        )

    def _get_pip_size(self) -> float:
        """Return the pip size for the configured symbol."""
        info = self.data_fetcher.get_symbol_info()
        if info is None:
            return 0.0001  # Default to 4-decimal-place pair
        digits = getattr(info, "digits", 4)
        return 10 ** -(digits - 1)

    def _get_pip_value(self, account_info) -> float:
        """Return the pip value per lot in account currency.

        Falls back to 10.0 (USD standard lot EURUSD-like pair).
        """
        symbol_info = self.data_fetcher.get_symbol_info()
        if symbol_info is None or account_info is None:
            return 10.0
        # Simplified: pip_value = trade_contract_size * pip_size
        contract_size = getattr(symbol_info, "trade_contract_size", 100_000)
        pip_size = self._get_pip_size()
        return contract_size * pip_size

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the bot and run the main trading loop."""
        global _RUNNING

        logger.info("=" * 60)
        logger.info("AI-BOT-MT5 starting | Symbol: %s | TF: %s", self.symbol, self.timeframe)
        logger.info("=" * 60)

        if not self.connector.connect():
            logger.error("Could not connect to MT5. Exiting.")
            return

        try:
            # Try loading a previously saved model
            if not self.strategy.load_model():
                logger.info("Training initial model...")
                df_init = self.data_fetcher.fetch_rates(self.cfg["strategy"]["train_lookback"] + 100)
                if df_init is not None:
                    self.strategy.train(df_init)

            while _RUNNING:
                self._tick()
                # Respect the bar close; sleep 1 second between ticks
                time.sleep(1)

        except Exception as exc:  # noqa: BLE001
            logger.critical("Unhandled exception in main loop: %s", exc, exc_info=True)
        finally:
            logger.info("Closing all open positions before shutdown...")
            self.order_manager.close_all_positions()
            self.connector.disconnect()
            logger.info("Bot stopped.")

    def _tick(self) -> None:
        """Execute one iteration of the trading loop."""
        lookback = self.cfg["strategy"]["lookback_periods"]
        df = self.data_fetcher.fetch_rates(lookback + 100)
        if df is None or df.empty:
            logger.warning("No data received from MT5. Skipping tick.")
            return

        # Retrain if stale
        if self.strategy.needs_retraining():
            train_df = self.data_fetcher.fetch_rates(
                self.cfg["strategy"]["train_lookback"] + 100
            )
            if train_df is not None:
                logger.info("Retraining model...")
                self.strategy.train(train_df)

        signal, confidence = self.strategy.predict(df)

        if signal == SIGNAL_HOLD:
            return

        account_info = self.connector.get_account_info()
        if account_info is None:
            logger.warning("Could not retrieve account info.")
            return

        balance = getattr(account_info, "balance", 0.0)
        if not self.risk_manager.can_open_trade(balance):
            return

        tick = self.data_fetcher.get_current_price()
        if tick is None:
            return

        entry_price = tick["ask"] if signal == SIGNAL_BUY else tick["bid"]
        pip_size = self._get_pip_size()
        pip_value = self._get_pip_value(account_info)
        symbol_info = self.data_fetcher.get_symbol_info()

        lot_size = self.risk_manager.calculate_lot_size(balance, pip_value, symbol_info)
        sl, tp = self.risk_manager.calculate_sl_tp(entry_price, signal, pip_size)

        ticket = self.order_manager.place_order(signal, lot_size, sl, tp)
        if ticket is not None:
            self.risk_manager.on_trade_opened()


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="AI-BOT-MT5: Automated trading bot for XM.")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to the YAML configuration file (default: config/config.yaml).",
    )
    args = parser.parse_args()
    bot = TradingBot(config_path=args.config)
    bot.run()


if __name__ == "__main__":
    main()
