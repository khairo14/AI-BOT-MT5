"""
Market Scanner — scans XM's 150+ symbols, ranks by viability for scalping/day/swing.
Outputs top 20 per trading type, grouped and scored by volatility, spread, trend strength.
"""

import json
import os
import threading
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
        # Dedicated lock that serialises concurrent scan_all() calls so that:
        # a) only one full scan runs at a time (prevents MT5 overload)
        # b) cache read + write is atomic (no TOCTOU gap)
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
        """
        Scan all available symbols across all enabled trading types.
        Returns grouped and ranked results.

        Args:
            force_refresh: If True, bypass cache and rescan
        """
        # Fast path: return cache without acquiring _scan_lock
        if not force_refresh and self._is_cache_valid():
            logger.info("Returning cached scan results")
            return self._cache

        # Serialize concurrent scans — only one thread runs the expensive scan;
        # others wait, then pick up the fresh cache on release.
        with self._scan_lock:
            # Re-check cache after acquiring lock: a concurrent scan may have
            # already completed while we were waiting.
            if not force_refresh and self._is_cache_valid():
                logger.info("Returning cached scan results (post-lock recheck)")
                return self._cache

            start_time = datetime.now(timezone.utc)
            logger.info("Starting market scan...")

            # Get all available symbols from MT5
            all_symbols = self._get_all_symbols()
            if not all_symbols:
                logger.error("No symbols available from MT5")
                return self._empty_summary()

            logger.info(f"Found {len(all_symbols)} symbols from MT5")

            # Scan each trading type
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

            # Create summary
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

            # Update cache — atomic write while holding _scan_lock
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
        """Get all tradeable symbols from MT5 via the MT5Client wrapper so that
        mutual exclusion with the trading engine's MT5 calls is guaranteed."""
        symbols_info = self.mt5.get_all_symbols()
        if not symbols_info:
            logger.error("mt5.get_all_symbols() returned empty list")
            return []
        
        # Filter to tradeable symbols, exclude those in blacklist
        exclude_list = self.cfg.get("exclude_symbols", [])
        tradeable = []
        
        for sym in symbols_info:
            # Must be visible and tradeable
            if not sym.visible or sym.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
                continue
            
            # Skip excluded symbols
            if sym.name in exclude_list:
                continue
            
            tradeable.append(sym.name)
        
        return tradeable
    
    # -----------------------------------------------------------------------
    # Scanning by Trading Type
    # -----------------------------------------------------------------------
    
    def _scan_trading_type(
        self,
        symbols: List[str],
        trading_type: str,
        type_cfg: dict
    ) -> List[ScanResult]:
        """
        Scan symbols for a specific trading type, apply criteria, rank, and return top N.
        """
        criteria = type_cfg["criteria"]
        weights = type_cfg["weights"]
        timeframe = type_cfg["timeframe"]
        lookback_bars = type_cfg["lookback_bars"]
        max_results = self.cfg.get("max_results_per_type", 20)
        
        results = []
        
        for symbol in symbols:
            # Get symbol category
            category = self._get_symbol_category(symbol)
            if not category:
                continue
            
            # Check if category is enabled
            cat_cfg = self.cfg["categories"].get(category)
            if not cat_cfg or not cat_cfg.get("enabled"):
                continue

            # Enforce allowed_categories for this trading type (strategy compatibility)
            allowed = type_cfg.get("allowed_categories")
            if allowed and category not in allowed:
                continue

            # Calculate all metrics
            metrics = self._calculate_metrics(
                symbol, 
                timeframe, 
                lookback_bars,
                category,
                cat_cfg
            )
            
            if not metrics:
                continue
            
            # Apply criteria filters
            if not self._passes_criteria(metrics, criteria):
                continue
            
            # Calculate component scores
            scores = self._calculate_scores(metrics, criteria, weights)
            
            # Create scan result
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
        
        # Sort by composite score (descending) and return top N
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
        cat_cfg: dict
    ) -> Optional[Dict[str, Any]]:
        """
        Calculate all raw metrics for a symbol.
        Returns None if data unavailable or calculation fails.
        """
        try:
            # Get symbol info
            sym_info = self.mt5.get_symbol_info(symbol)
            if not sym_info:
                return None
            
            # Get OHLCV data
            df = self.mt5.get_ohlcv(symbol, timeframe, lookback_bars)
            if df is None or len(df) < 50:  # Need minimum bars for indicators
                return None
            
            # Calculate ATR (14-period) in pips
            atr_pips = self._calculate_atr(df, 14, sym_info["point"], cat_cfg["pip_multiplier"])
            
            # Spread
            spread_pips = sym_info["spread_pips"]
            
            # ADX (14-period)
            adx = self._calculate_adx(df, 14)
            
            # Daily volume (approximate from tick volume)
            daily_volume = float(df["volume"].tail(24).sum()) if len(df) >= 24 else 0.0
            
            # Volatility percentile (last 30 bars vs full lookback)
            volatility_percentile = self._calculate_volatility_percentile(df, 30)
            
            # Liquidity score (0-10 based on volume and spread)
            liquidity_score = self._calculate_liquidity_score(daily_volume, spread_pips, sym_info)
            
            # Momentum score (RSI + ROC combination)
            momentum_score = self._calculate_momentum_score(df)
            
            # Current price
            current_price = float(df["close"].iloc[-1])
            
            # Trading hours check
            trading_hours_active = self._is_trading_hours(symbol, category)
            
            return {
                "atr_pips": atr_pips,
                "spread_pips": spread_pips,
                "adx": adx,
                "daily_volume": daily_volume,
                "volatility_percentile": volatility_percentile,
                "liquidity_score": liquidity_score,
                "momentum_score": momentum_score,
                "current_price": current_price,
                "pip_value": sym_info["pip_value"],
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
        atr_pips = (atr / point) * pip_mult
        return round(atr_pips, 2)
    
    def _calculate_adx(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate Average Directional Index."""
        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        
        # Calculate +DM and -DM
        plus_dm = np.zeros(len(high))
        minus_dm = np.zeros(len(high))
        
        for i in range(1, len(high)):
            high_diff = high[i] - high[i-1]
            low_diff = low[i-1] - low[i]
            
            if high_diff > low_diff and high_diff > 0:
                plus_dm[i] = high_diff
            if low_diff > high_diff and low_diff > 0:
                minus_dm[i] = low_diff
        
        # Calculate True Range
        tr = np.zeros(len(high))
        for i in range(1, len(high)):
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i-1]),
                abs(low[i] - close[i-1])
            )
        
        # Smooth with Wilder's moving average
        atr = self._wilder_smooth(tr, period)
        # Add epsilon to prevent division by zero
        plus_di = 100 * self._wilder_smooth(plus_dm, period) / (atr + 1e-10)
        minus_di = 100 * self._wilder_smooth(minus_dm, period) / (atr + 1e-10)
        
        # Calculate DX and ADX
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
        adx = self._wilder_smooth(dx, period)
        
        return round(float(adx[-1]), 2)
    
    def _wilder_smooth(self, data: np.ndarray, period: int) -> np.ndarray:
        """Wilder's smoothing (EMA-like with alpha=1/period)."""
        result = np.zeros_like(data)
        result[period-1] = np.mean(data[:period])
        
        for i in range(period, len(data)):
            result[i] = (result[i-1] * (period - 1) + data[i]) / period
        
        return result
    
    def _calculate_volatility_percentile(self, df: pd.DataFrame, recent_window: int) -> float:
        """Calculate what percentile the recent volatility is vs historical."""
        if len(df) < recent_window * 2:
            return 50.0  # Default to median
        
        # Calculate rolling ATR
        returns = df["close"].pct_change().values[1:]
        
        # Recent volatility (last N bars)
        recent_vol = np.std(returns[-recent_window:]) if len(returns) >= recent_window else 0
        
        # Historical volatility distribution
        all_vols = [np.std(returns[max(0, i-recent_window):i]) 
                    for i in range(recent_window, len(returns), recent_window)]
        
        if not all_vols or recent_vol == 0:
            return 50.0
        
        percentile = (np.sum(np.array(all_vols) <= recent_vol) / len(all_vols)) * 100
        return round(percentile, 1)
    
    def _calculate_liquidity_score(
        self, 
        volume: float, 
        spread_pips: float,
        sym_info: dict
    ) -> float:
        """
        Calculate liquidity score (0-10) based on volume and spread.
        Higher is better (high volume, low spread).
        """
        # Volume component (normalize to 0-5)
        vol_score = min(5.0, (volume / 10000) * 5) if volume > 0 else 0
        
        # Spread component (inverse, normalize to 0-5)
        # Lower spread = better
        max_acceptable_spread = 10.0
        spread_score = max(0, 5.0 * (1 - spread_pips / max_acceptable_spread))
        
        total = vol_score + spread_score
        return round(total, 1)
    
    def _calculate_momentum_score(self, df: pd.DataFrame) -> float:
        """
        Calculate momentum score (0-100) using RSI and ROC.
        50 is neutral, >50 is bullish, <50 is bearish.
        """
        close = df["close"].values
        
        # RSI (14-period)
        rsi = self._calculate_rsi(close, 14)
        
        # Rate of Change (10-period)
        roc = ((close[-1] - close[-10]) / close[-10] * 100) if len(close) >= 10 else 0
        
        # Combine (RSI 70%, ROC 30%)
        momentum = 0.7 * rsi + 0.3 * (50 + roc)  # ROC scaled to 0-100 range
        
        return round(np.clip(momentum, 0, 100), 1)
    
    def _calculate_rsi(self, prices: np.ndarray, period: int = 14) -> float:
        """Calculate Relative Strength Index."""
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains[-period:]) if len(gains) >= period else 0
        avg_loss = np.mean(losses[-period:]) if len(losses) >= period else 0
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return round(rsi, 1)
    
    def _is_trading_hours(self, symbol: str, category: str) -> bool:
        """Check if symbol is in active trading hours."""
        if not self.cfg["filters"].get("respect_trading_hours"):
            return True
        
        now = datetime.now(timezone.utc)
        hour = now.hour
        weekday = now.weekday()  # 0=Monday, 6=Sunday
        
        # Forex: 24/5 (closes Friday 22:00 UTC, opens Sunday 22:00 UTC)
        if category == "forex":
            if weekday == 6:  # Sunday
                return hour >= 22
            if weekday == 5:  # Saturday
                return False
            if weekday == 4:  # Friday
                return hour < 22
            return True
        
        # Crypto: 24/7
        if category == "crypto":
            return True
        
        # US markets: 14:30-21:00 UTC (Monday-Friday)
        if category in ["us_index", "stock"]:
            if weekday >= 5:  # Weekend
                return False
            return 14 <= hour < 21
        
        # EU markets: 08:00-16:30 UTC (Monday-Friday)
        if category == "eu_index":
            if weekday >= 5:
                return False
            return 8 <= hour < 17
        
        # Default: assume 24/5
        return weekday < 5
    
    # -----------------------------------------------------------------------
    # Criteria & Scoring
    # -----------------------------------------------------------------------
    
    def _passes_criteria(self, metrics: dict, criteria: dict) -> bool:
        """Check if metrics pass all criteria filters."""
        # ATR range
        if not (criteria["atr_min_pips"] <= metrics["atr_pips"] <= criteria["atr_max_pips"]):
            return False
        
        # Spread threshold
        if metrics["spread_pips"] > criteria["max_spread_pips"]:
            return False
        
        # ADX range
        if not (criteria["adx_min"] <= metrics["adx"] <= criteria["adx_max"]):
            return False
        
        # Volume threshold
        if metrics["daily_volume"] < criteria["min_daily_volume_lots"]:
            return False
        
        # Volatility percentile range
        vol_pct = metrics["volatility_percentile"]
        if not (criteria["volatility_percentile_min"] <= vol_pct <= criteria["volatility_percentile_max"]):
            return False
        
        # Liquidity score
        if metrics["liquidity_score"] < criteria["liquidity_score_min"]:
            return False
        
        # Trading hours (if filter enabled)
        if self.cfg["filters"].get("respect_trading_hours"):
            if not metrics["trading_hours_active"]:
                return False
        
        return True
    
    def _calculate_scores(self, metrics: dict, criteria: dict, weights: dict) -> dict:
        """
        Calculate normalized component scores and weighted composite score.
        All scores are 0-100.
        """
        # Volatility score (higher ATR = higher score, up to max threshold)
        vol_score = self._normalize_score(
            metrics["atr_pips"],
            criteria["atr_min_pips"],
            criteria["atr_max_pips"],
            inverse=False
        )
        
        # Spread score (lower spread = higher score)
        spread_score = self._normalize_score(
            metrics["spread_pips"],
            0,
            criteria["max_spread_pips"],
            inverse=True
        )
        
        # Trend score (ADX, optimal around 25-45)
        trend_score = self._normalize_bell_curve(
            metrics["adx"],
            optimal=35,
            min_val=criteria["adx_min"],
            max_val=criteria["adx_max"]
        )
        
        # Liquidity score (already 0-10, scale to 0-100)
        liquidity_score = metrics["liquidity_score"] * 10
        
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
    
    def _normalize_score(
        self, 
        value: float, 
        min_val: float, 
        max_val: float, 
        inverse: bool = False
    ) -> float:
        """Normalize value to 0-100 scale."""
        if max_val == min_val:
            return 50.0
        
        normalized = (value - min_val) / (max_val - min_val)
        normalized = np.clip(normalized, 0, 1)
        
        if inverse:
            normalized = 1 - normalized
        
        return normalized * 100
    
    def _normalize_bell_curve(
        self,
        value: float,
        optimal: float,
        min_val: float,
        max_val: float
    ) -> float:
        """
        Normalize with peak at optimal value, declining toward min/max.
        Used for metrics like ADX where mid-range is best.
        """
        if value == optimal:
            return 100.0
        
        # Distance from optimal
        if value < optimal:
            # Scale from min to optimal (0 to 100)
            if optimal == min_val:
                return 100.0
            normalized = (value - min_val) / (optimal - min_val)
        else:
            # Scale from optimal to max (100 to 0)
            if max_val == optimal:
                return 100.0
            normalized = 1 - (value - optimal) / (max_val - optimal)
        
        normalized = np.clip(normalized, 0, 1)
        return normalized * 100
    
    # -----------------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------------
    
    def _get_symbol_category(self, symbol: str) -> Optional[str]:
        """Determine symbol category (forex, crypto, us_index, etc.)."""
        symbol_upper = symbol.upper()
        
        # Forex pairs (7 characters, ends with USD/JPY/EUR/GBP/CHF/CAD/AUD/NZD)
        forex_endings = ["USD", "JPY", "EUR", "GBP", "CHF", "CAD", "AUD", "NZD"]
        if len(symbol) == 6 and any(symbol_upper.endswith(end) for end in forex_endings):
            return "forex"
        
        # Crypto
        crypto_symbols = ["BTC", "ETH", "XRP", "SOL", "ADA", "DOT", "DOGE", "LINK"]
        if any(crypto in symbol_upper for crypto in crypto_symbols):
            return "crypto"
        
        # US indices
        if any(x in symbol_upper for x in ["US100", "US30", "US500", "NAS100", "DOW", "SPX"]):
            return "us_index"
        
        # EU indices
        if any(x in symbol_upper for x in ["GER40", "GER30", "UK100", "FRA40", "ESP35", "EU50"]):
            return "eu_index"
        
        # Commodities
        commodities = ["GOLD", "SILVER", "XAUUSD", "XAGUSD", "OIL", "BRENT", "NGAS", "WTI"]
        if any(com in symbol_upper for com in commodities):
            return "commodity"
        
        # Stocks (everything else with proper formatting)
        return "stock"
    
    def _is_cache_valid(self) -> bool:
        """Check if cached results are still valid."""
        if self._cache is None or self._cache_time is None:
            return False
        
        cache_duration = self.cfg.get("advanced", {}).get("cache_duration_minutes", 30)
        age_minutes = (datetime.now(timezone.utc) - self._cache_time).total_seconds() / 60
        
        return age_minutes < cache_duration
    
    def _compute_config_hash(self) -> str:
        """Simple hash of config to detect changes."""
        import hashlib
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
