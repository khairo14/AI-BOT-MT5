"""
Market Scanner — scans XM's 150+ symbols, ranks by viability for scalping/day/swing.
Outputs top 20 per trading type, grouped and scored by volatility, spread, trend strength.
"""

import json
import os
import threading
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, asdict

import MetaTrader5 as mt5
import numpy as np
import pandas as pd
from loguru import logger

from engine.mt5_client import MT5Client


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    """Single symbol scan result with all metrics and scores."""
    symbol: str
    category: str
    trading_type: str
    
    # Raw metrics
    atr_pips: float
    spread_pips: float
    adx: float
    daily_volume: float
    volatility_percentile: float
    liquidity_score: float
    momentum_score: float
    
    # Component scores (0-100)
    volatility_score: float
    spread_score: float
    trend_score: float
    liquidity_subscore: float
    momentum_subscore: float
    
    # Final composite score (0-100)
    composite_score: float
    
    # Additional context
    current_price: float
    pip_value: float
    trading_hours_active: bool
    last_updated: str


@dataclass
class ScanSummary:
    """Summary of entire scan operation."""
    timestamp: str
    trading_types: Dict[str, List[ScanResult]]  # scalping/day/swing -> results
    total_scanned: int
    total_passed: int
    scan_duration_seconds: float
    config_hash: str


# ---------------------------------------------------------------------------
# Market Scanner
# ---------------------------------------------------------------------------

class MarketScanner:
    """
    Scans all available MT5 symbols and ranks them by trading viability.
    Groups results by trading type (scalping, day_trading, swing).
    """
    
    def __init__(self, mt5_client: MT5Client):
        self.mt5 = mt5_client
        self._lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._cache: Optional[ScanSummary] = None
        self._cache_time: Optional[datetime] = None
        
        # Load configuration
        config_path = "config/market_scanner.json"
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Market scanner config not found: {config_path}")
        
        with open(config_path, "r") as f:
            self.cfg = json.load(f)
        
        logger.info("MarketScanner initialized")
    
    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------
    
    def scan_all(self, force_refresh: bool = False) -> ScanSummary:
        """Scan all available symbols across all enabled trading types."""
        if not force_refresh and self._is_cache_valid():
            logger.info("Returning cached scan results")
            return self._cache

        with self._scan_lock:
            if not force_refresh and self._is_cache_valid():
                logger.info("Returning cached scan results (post-lock recheck)")
                return self._cache

            start_time = datetime.now(timezone.utc)
            logger.info("Starting market scan...")

            all_symbols = self._get_all_symbols()
            if not all_symbols:
                logger.error("No symbols available from MT5")
                return self._empty_summary()

            logger.info(f"Found {len(all_symbols)} symbols from MT5")

            results_by_type = {}
            total_passed = 0

            for trading_type in ["scalping", "day_trading", "swing"]:
                type_cfg = self.cfg["trading_types"].get(trading_type)
                if not type_cfg or not type_cfg.get("enabled"):
                    logger.info(f"Trading type '{trading_type}' is disabled, skipping")
                    continue

                logger.info(f"Scanning for {trading_type}...")
                results = self._scan_trading_type(all_symbols, trading_type, type_cfg)
                results_by_type[trading_type] = results
                total_passed += len(results)
                logger.info(f"  → {len(results)} symbols passed {trading_type} criteria")

            end_time = datetime.now(timezone.utc)
            duration = (end_time - start_time).total_seconds()

            summary = ScanSummary(
                timestamp=start_time.isoformat(),
                trading_types=results_by_type,
                total_scanned=len(all_symbols),
                total_passed=total_passed,
                scan_duration_seconds=round(duration, 2),
                config_hash=self._compute_config_hash()
            )

            self._cache = summary
            self._cache_time = start_time

            logger.info(
                f"Market scan complete | Scanned: {len(all_symbols)} | "
                f"Passed: {total_passed} | Duration: {duration:.2f}s"
            )

            return summary
    
    def scan_type(self, trading_type: str, force_refresh: bool = False) -> List[ScanResult]:
        """Scan only for a specific trading type."""
        summary = self.scan_all(force_refresh=force_refresh)
        return summary.trading_types.get(trading_type, [])
    
    def get_cached_results(self) -> Optional[ScanSummary]:
        """Return cached results if available."""
        if self._is_cache_valid():
            return self._cache
        return None
    
    def invalidate_cache(self) -> None:
        """Clear cached scan results."""
        self._cache = None
        self._cache_time = None
        logger.info("Scanner cache invalidated")
    
    # -----------------------------------------------------------------------
    # Symbol Discovery
    # -----------------------------------------------------------------------
    
    def _get_all_symbols(self) -> List[str]:
        """Get all tradeable symbols from MT5."""
        symbols_info = self.mt5.get_all_symbols()
        if not symbols_info:
            logger.error("mt5.get_all_symbols() returned empty list")
            return []
        
        exclude_list = self.cfg.get("exclude_symbols", [])
        tradeable = []
        
        for sym in symbols_info:
            # Must be visible and tradeable
            trade_mode = getattr(sym, 'trade_mode', 4)
            if trade_mode not in (mt5.SYMBOL_TRADE_MODE_FULL, mt5.SYMBOL_TRADE_MODE_LONGONLY, mt5.SYMBOL_TRADE_MODE_SHORTONLY):
                continue
            
            # Skip excluded symbols
            if getattr(sym, 'name', '') in exclude_list:
                continue
            
            tradeable.append(sym.name)
        
        return tradeable
    
    # -----------------------------------------------------------------------
    # Dynamic Category Detection
    # -----------------------------------------------------------------------
    
    @staticmethod
    def _detect_category_by_name(symbol: str) -> str:
        """Fallback category detection from symbol name (handles suffixes)."""
        # Strip common suffixes (#, ., _, -, *)
        clean = symbol.upper().rstrip("#+*!._-")
        # Remove anything that's not a letter for forex detection
        letters = ''.join(c for c in clean if c.isalpha())
        
        # Crypto
        crypto = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "DOT", "LTC", "BNB", "XLM", "ETC", "GRT"}
        if any(c in clean for c in crypto):
            return "crypto"
        
        # US Indices
        us_indices = {"US30", "US100", "US500", "SPX", "NAS", "NDX"}
        if any(idx in clean for idx in us_indices):
            return "indices"
        
        # EU Indices
        eu_indices = {"GER40", "DAX", "UK100", "FRA40", "EU50"}
        if any(idx in clean for idx in eu_indices):
            return "indices"
        
        # Commodities
        commodities = {"GOLD", "XAU", "SILVER", "XAG", "OIL", "BRENT", "WTI", "NGAS"}
        if any(cmd in clean for cmd in commodities):
            return "commodity"
        
        # Stocks (company names)
        stocks = {"TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NFLX", "AMD", "INTC", "ADV"}
        if any(stk in clean for stk in stocks):
            return "stock"
        
        # Forex: 6 letters (e.g., EURUSD, GBPJPY) OR contains currency pair pattern
        if len(letters) == 6:
            currencies = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SGD", "HKD"}
            first_three = letters[:3]
            last_three = letters[3:]
            if first_three in currencies and last_three in currencies:
                return "forex"
        
        # Also detect by currency code presence
        if ("EUR" in clean and "USD" in clean) or \
           ("GBP" in clean and "USD" in clean) or \
           ("USD" in clean and "JPY" in clean) or \
           ("AUD" in clean and "USD" in clean):
            return "forex"
        
        # Default
        return "forex"
    
    @staticmethod
    def _get_category_from_path(path: str, symbol: str = "") -> str:
        """Determine symbol category dynamically from MT5's symbol group path."""
        if path:
            segments = path.split("\\")
            first = segments[0].lower() if segments else ""
            
            if first == "forex":
                return "forex"
            if first in ("cryptocurrencies", "crypto"):
                return "crypto"
            if first == "stocks":
                return "stock"
            if first == "indices":
                return "indices"
            if first == "derivatives":
                for seg in segments:
                    s = seg.lower()
                    if s in ("indices", "index"):
                        return "indices"
                    if s in ("metals", "metal") or s in ("energies", "energy"):
                        return "commodity"
        
        # Fallback to name-based detection
        return MarketScanner._detect_category_by_name(symbol)
    
    # -----------------------------------------------------------------------
    # Scanning by Trading Type
    # -----------------------------------------------------------------------
    
    def _scan_trading_type(
        self,
        symbols: List[str],
        trading_type: str,
        type_cfg: dict
    ) -> List[ScanResult]:
        """Scan symbols for a specific trading type."""
        criteria = type_cfg["criteria"]
        weights = type_cfg["weights"]
        timeframe = type_cfg["timeframe"]
        lookback_bars = type_cfg["lookback_bars"]
        max_results = self.cfg.get("max_results_per_type", 20)
        
        results = []
        
        for symbol in symbols:
            sym_info = self.mt5.get_symbol_info(symbol)
            if not sym_info:
                continue
            
            # Dynamic category from path or name
            category = self._get_category_from_path(sym_info.get("path", ""), symbol)
            if not category:
                continue
            
            # Check if category is enabled
            cat_cfg = self.cfg["categories"].get(category)
            if not cat_cfg or not cat_cfg.get("enabled"):
                continue
            
            # Enforce allowed categories for this trading type
            allowed = type_cfg.get("allowed_categories")
            if allowed and category not in allowed:
                continue
            
            # Calculate metrics
            metrics = self._calculate_metrics(
                symbol, timeframe, lookback_bars, category, cat_cfg, sym_info
            )
            
            if not metrics:
                continue
            
            # Apply criteria filters
            if not self._passes_criteria(metrics, criteria):
                continue
            
            # Calculate scores
            scores = self._calculate_scores(metrics, criteria, weights)
            
            result = ScanResult(
                symbol=symbol,
                category=category,
                trading_type=trading_type,
                atr_pips=metrics["atr_pips"],
                spread_pips=metrics["spread_pips"],
                adx=metrics["adx"],
                daily_volume=metrics["daily_volume"],
                volatility_percentile=metrics["volatility_percentile"],
                liquidity_score=metrics["liquidity_score"],
                momentum_score=metrics["momentum_score"],
                volatility_score=scores["volatility"],
                spread_score=scores["spread"],
                trend_score=scores["trend"],
                liquidity_subscore=scores["liquidity"],
                momentum_subscore=scores["momentum"],
                composite_score=scores["composite"],
                current_price=metrics["current_price"],
                pip_value=metrics["pip_value"],
                trading_hours_active=metrics["trading_hours_active"],
                last_updated=datetime.now(timezone.utc).isoformat()
            )
            
            results.append(result)
        
        results.sort(key=lambda x: x.composite_score, reverse=True)
        return results[:max_results]
    
    # -----------------------------------------------------------------------
    # Metrics Calculation
    # -----------------------------------------------------------------------
    
    def _calculate_metrics(
        self,
        symbol: str,
        timeframe: str,
        lookback_bars: int,
        category: str,
        cat_cfg: dict,
        sym_info: dict,
    ) -> Optional[Dict[str, Any]]:
        """Calculate all raw metrics for a symbol."""
        try:
            # Get symbol info - ensure point has a fallback
            point = sym_info.get("point", 0.00001)
            if point == 0:
                point = 0.00001  # fallback for instruments with point=0
            
            # Check trade mode
            _trade_mode = sym_info.get("trade_mode", 4)
            if _trade_mode not in (1, 2, 4):
                logger.debug(f"Scanner: skipping {symbol} — trade_mode={_trade_mode}")
                return None

            # Get OHLCV data
            df = self.mt5.get_ohlcv(symbol, timeframe, lookback_bars)
            if df is None or len(df) < 50:
                return None
            
            # Pip multiplier from category config
            pip_mult = cat_cfg.get("pip_multiplier", 10) if cat_cfg else 10
            
            # Calculate metrics
            atr_pips = self._calculate_atr(df, 14, point, pip_mult)
            spread_pips = sym_info.get("spread_pips", 0.0)
            adx = self._calculate_adx(df, 14)
            daily_volume = float(df["volume"].tail(24).sum()) if len(df) >= 24 else 0.0
            volatility_percentile = self._calculate_volatility_percentile(df, 30)
            liquidity_score = self._calculate_liquidity_score(daily_volume, spread_pips, sym_info)
            momentum_score = self._calculate_momentum_score(df)
            current_price = float(df["close"].iloc[-1])
            trading_hours_active = self._is_trading_hours(symbol, category, sym_info)
            
            return {
                "atr_pips": atr_pips,
                "spread_pips": spread_pips,
                "adx": adx,
                "daily_volume": daily_volume,
                "volatility_percentile": volatility_percentile,
                "liquidity_score": liquidity_score,
                "momentum_score": momentum_score,
                "current_price": current_price,
                "pip_value": sym_info.get("pip_value", 1.0),
                "trading_hours_active": trading_hours_active,
            }
        
        except Exception as e:
            logger.debug(f"Metrics calculation failed for {symbol}: {e}")
            return None
    
    def _calculate_atr(self, df: pd.DataFrame, period: int, point: float, pip_mult: float) -> float:
        """Calculate Average True Range in pips."""
        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        
        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(
                np.abs(high[1:] - close[:-1]),
                np.abs(low[1:] - close[:-1])
            )
        )
        
        atr = np.mean(tr[-period:]) if len(tr) >= period else np.mean(tr)
        atr_pips = (atr / point) * pip_mult if point > 0 else atr * 10000
        return round(atr_pips, 2)
    
    def _calculate_adx(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate Average Directional Index."""
        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        
        plus_dm = np.zeros(len(high))
        minus_dm = np.zeros(len(high))
        
        for i in range(1, len(high)):
            high_diff = high[i] - high[i-1]
            low_diff = low[i-1] - low[i]
            
            if high_diff > low_diff and high_diff > 0:
                plus_dm[i] = high_diff
            if low_diff > high_diff and low_diff > 0:
                minus_dm[i] = low_diff
        
        tr = np.zeros(len(high))
        for i in range(1, len(high)):
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i-1]),
                abs(low[i] - close[i-1])
            )
        
        atr = self._wilder_smooth(tr, period)
        plus_di = 100 * self._wilder_smooth(plus_dm, period) / (atr + 1e-10)
        minus_di = 100 * self._wilder_smooth(minus_dm, period) / (atr + 1e-10)
        
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
        adx = self._wilder_smooth(dx, period)
        
        return round(float(adx[-1]), 2) if len(adx) > 0 else 0.0
    
    def _wilder_smooth(self, data: np.ndarray, period: int) -> np.ndarray:
        """Wilder's smoothing (EMA-like with alpha=1/period)."""
        result = np.zeros_like(data)
        if len(data) < period:
            return result
        
        result[period-1] = np.mean(data[:period])
        for i in range(period, len(data)):
            result[i] = (result[i-1] * (period - 1) + data[i]) / period
        
        return result
    
    def _calculate_volatility_percentile(self, df: pd.DataFrame, recent_window: int) -> float:
        """Calculate what percentile the recent volatility is vs historical."""
        if len(df) < recent_window * 2:
            return 50.0
        
        returns = df["close"].pct_change().values[1:]
        recent_vol = np.std(returns[-recent_window:]) if len(returns) >= recent_window else 0
        
        all_vols = [np.std(returns[max(0, i-recent_window):i]) 
                    for i in range(recent_window, len(returns), recent_window)]
        
        if not all_vols or recent_vol == 0:
            return 50.0
        
        percentile = (np.sum(np.array(all_vols) <= recent_vol) / len(all_vols)) * 100
        return round(percentile, 1)
    
    def _calculate_liquidity_score(self, volume: float, spread_pips: float, sym_info: dict) -> float:
        """Calculate liquidity score (0-10) using percentile-based ranking."""
        # Use percentile-based scoring instead of hardcoded scaling
        # Normalize volume (use log scale)
        vol_log = np.log(volume + 1) if volume > 0 else 0
        # Normalize to 0-5 range (typical volume log range: 0-15)
        vol_score = min(5.0, (vol_log / 15) * 5) if volume > 0 else 0
        
        # Spread component (inverse)
        max_acceptable_spread = 20.0
        spread_score = max(0, 5.0 * (1 - min(1.0, spread_pips / max_acceptable_spread)))
        
        total = vol_score + spread_score
        return round(total, 1)
    
    def _calculate_momentum_score(self, df: pd.DataFrame) -> float:
        """Calculate momentum score (0-100) using RSI and ROC."""
        close = df["close"].values
        
        rsi = self._calculate_rsi(close, 14)
        
        roc = ((close[-1] - close[-10]) / close[-10] * 100) if len(close) >= 10 else 0
        
        momentum = 0.7 * rsi + 0.3 * (50 + roc)
        momentum = np.clip(momentum, 0, 100)
        
        return round(momentum, 1)
    
    def _calculate_rsi(self, prices: np.ndarray, period: int = 14) -> float:
        """Calculate Relative Strength Index."""
        if len(prices) < period + 1:
            return 50.0
        
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return round(rsi, 1)
    
    def _is_trading_hours(self, symbol: str, category: str, sym_info: dict = None) -> bool:
        """Check if symbol is in active trading hours."""
        if not self.cfg["filters"].get("respect_trading_hours"):
            return True
        
        now = datetime.now(timezone.utc)
        hour = now.hour
        weekday = now.weekday()
        
        # Crypto: 24/7
        if category == "crypto":
            return True
        
        # Forex: 24/5
        if category == "forex":
            if weekday == 6:  # Sunday
                return hour >= 22
            if weekday == 5:  # Saturday
                return False
            if weekday == 4:  # Friday
                return hour < 22
            return True
        
        # Indices and Stocks: market hours
        if category in ("indices", "stock"):
            if weekday >= 5:
                return False
            
            # Try to determine region from symbol
            clean = symbol.upper()
            # US markets (14-21 UTC)
            if any(x in clean for x in ("US30", "US100", "US500", "SPX", "NAS", "NDX")):
                return 14 <= hour < 21
            # EU markets (8-17 UTC)
            if any(x in clean for x in ("GER40", "DAX", "UK100", "FRA40", "EU50")):
                return 8 <= hour < 17
            # Asian markets (23-8 UTC)
            if any(x in clean for x in ("JP225", "AUS200", "N225")):
                return hour >= 23 or hour < 8
            
            # Default for indices
            return 8 <= hour < 21
        
        # Commodities: weekdays only
        if category == "commodity":
            return weekday < 5
        
        return weekday < 5
    
    # -----------------------------------------------------------------------
    # Criteria & Scoring
    # -----------------------------------------------------------------------
    
    def _passes_criteria(self, metrics: dict, criteria: dict) -> bool:
        """Check if metrics pass all criteria filters."""
        # ATR range
        atr_min = criteria.get("atr_min_pips", 0)
        atr_max = criteria.get("atr_max_pips", 999)
        if not (atr_min <= metrics["atr_pips"] <= atr_max):
            return False
        
        # Spread threshold
        if metrics["spread_pips"] > criteria.get("max_spread_pips", 999):
            return False
        
        # ADX range
        adx_min = criteria.get("adx_min", 0)
        adx_max = criteria.get("adx_max", 100)
        if not (adx_min <= metrics["adx"] <= adx_max):
            return False
        
        # Volume threshold
        if metrics["daily_volume"] < criteria.get("min_daily_volume_lots", 0):
            return False
        
        # Volatility percentile range
        vol_min = criteria.get("volatility_percentile_min", 0)
        vol_max = criteria.get("volatility_percentile_max", 100)
        if not (vol_min <= metrics["volatility_percentile"] <= vol_max):
            return False
        
        # Liquidity score
        if metrics["liquidity_score"] < criteria.get("liquidity_score_min", 0):
            return False
        
        # Trading hours
        if self.cfg["filters"].get("respect_trading_hours"):
            if not metrics["trading_hours_active"]:
                return False
        
        return True
    
    def _calculate_scores(self, metrics: dict, criteria: dict, weights: dict) -> dict:
        """Calculate normalized component scores and weighted composite score."""
        # Volatility score (higher ATR = higher score)
        vol_score = self._normalize_score(
            metrics["atr_pips"],
            criteria.get("atr_min_pips", 0),
            criteria.get("atr_max_pips", 100),
            inverse=False
        )
        
        # Spread score (lower spread = higher score)
        spread_score = self._normalize_score(
            metrics["spread_pips"],
            0,
            criteria.get("max_spread_pips", 20),
            inverse=True
        )
        
        # Trend score (ADX, optimal around 25-45)
        trend_score = self._normalize_bell_curve(
            metrics["adx"],
            optimal=35,
            min_val=criteria.get("adx_min", 0),
            max_val=criteria.get("adx_max", 100)
        )
        
        # Liquidity score (already 0-10, scale to 0-100)
        liquidity_score = min(100, metrics["liquidity_score"] * 10)
        
        # Momentum score (already 0-100)
        momentum_score = metrics["momentum_score"]
        
        # Weighted composite
        composite = (
            vol_score * weights.get("volatility", 0.25) +
            spread_score * weights.get("spread", 0.25) +
            trend_score * weights.get("trend_strength", 0.20) +
            liquidity_score * weights.get("liquidity", 0.15) +
            momentum_score * weights.get("momentum", 0.15)
        )
        
        return {
            "volatility": round(vol_score, 1),
            "spread": round(spread_score, 1),
            "trend": round(trend_score, 1),
            "liquidity": round(liquidity_score, 1),
            "momentum": round(momentum_score, 1),
            "composite": round(composite, 1),
        }
    
    def _normalize_score(self, value: float, min_val: float, max_val: float, inverse: bool = False) -> float:
        """Normalize value to 0-100 scale."""
        if max_val <= min_val:
            return 50.0
        
        normalized = (value - min_val) / (max_val - min_val)
        normalized = np.clip(normalized, 0, 1)
        
        if inverse:
            normalized = 1 - normalized
        
        return normalized * 100
    
    def _normalize_bell_curve(self, value: float, optimal: float, min_val: float, max_val: float) -> float:
        """Normalize with peak at optimal value."""
        if value == optimal:
            return 100.0
        
        if value < optimal:
            if optimal == min_val:
                return 100.0
            normalized = (value - min_val) / (optimal - min_val)
        else:
            if max_val == optimal:
                return 100.0
            normalized = 1 - (value - optimal) / (max_val - optimal)
        
        normalized = np.clip(normalized, 0, 1)
        return normalized * 100
    
    # -----------------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------------
    
    def _is_cache_valid(self) -> bool:
        """Check if cached results are still valid."""
        if self._cache is None or self._cache_time is None:
            return False
        
        cache_duration = self.cfg.get("advanced", {}).get("cache_duration_minutes", 30)
        age_minutes = (datetime.now(timezone.utc) - self._cache_time).total_seconds() / 60
        
        return age_minutes < cache_duration
    
    def _compute_config_hash(self) -> str:
        """Simple hash of config to detect changes."""
        cfg_str = json.dumps(self.cfg, sort_keys=True)
        return hashlib.md5(cfg_str.encode()).hexdigest()[:8]
    
    def _empty_summary(self) -> ScanSummary:
        """Return empty scan summary."""
        return ScanSummary(
            timestamp=datetime.now(timezone.utc).isoformat(),
            trading_types={},
            total_scanned=0,
            total_passed=0,
            scan_duration_seconds=0.0,
            config_hash=""
        )


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def scan_result_to_dict(result: ScanResult) -> dict:
    """Convert ScanResult dataclass to dict."""
    return asdict(result)


def summary_to_dict(summary: ScanSummary) -> dict:
    """Convert ScanSummary to dict with nested results."""
    return {
        "timestamp": summary.timestamp,
        "trading_types": {
            ttype: [scan_result_to_dict(r) for r in results]
            for ttype, results in summary.trading_types.items()
        },
        "total_scanned": summary.total_scanned,
        "total_passed": summary.total_passed,
        "scan_duration_seconds": summary.scan_duration_seconds,
        "config_hash": summary.config_hash,
    }