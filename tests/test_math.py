"""
Phase 12 — Math & Logic Tests
Covers: lot sizing, SL/TP validation, drawdown circuit breakers,
        consecutive-loss pause, P&L direction, session/news filter stubs.
No MT5 connection required.
"""

import sys
import os
import math
from datetime import datetime, date, timedelta, timezone

# Make project root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Minimal risk config (mirrors config/risk.json defaults)
RISK_CFG = {
    "risk_per_trade_pct": 1.0,
    "max_risk_per_trade_pct": 2.0,
    "risk_reward_min": 1.5,
    "drawdown": {
        "daily_limit_pct": 5.0,
        "weekly_limit_pct": 10.0,
        "max_consecutive_losses": 5,
        "consecutive_loss_pause_hours": 4,
    },
    "max_concurrent_trades": 3,
    "max_concurrent_per_mode": {"scalping": 2, "day_trading": 1, "swing": 1},
    "allowed_symbols": [],
    "blocked_hours_utc": [],
}

# ── import RiskManager (no MT5 dependency) ─────────────────────────────────
from engine.risk_manager import RiskManager

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    mark = "✓" if condition else "✗"
    print(f"  [{mark}] {name}{' — ' + detail if detail else ''}")


# ══════════════════════════════════════════════════════════════════════════════
# 1. LOT SIZE MATH
# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 1. Lot Size Math ===")
rm = RiskManager(config=RISK_CFG)

# EURUSD: balance=10000, risk=1% ($100), SL=10 pip (0.0010), tick=0.00001, tick_val=1.0
# raw = 100 / (100 ticks × $1) = 1.00 lot  → floor to step 0.01 → 1.00
lot_10pip = rm.calculate_lot_size(
    balance=10_000, entry=1.10000, sl=1.09900,
    tick_value=1.0, tick_size=0.00001,
)
check("10-pip SL → ~1.00 lot", 0.98 <= lot_10pip <= 1.00,
      f"got {lot_10pip}")

# Double SL = half the lot
lot_20pip = rm.calculate_lot_size(
    balance=10_000, entry=1.10000, sl=1.09800,
    tick_value=1.0, tick_size=0.00001,
)
check("20-pip SL ≈ half of 10-pip lot", abs(lot_10pip / lot_20pip - 2.0) < 0.1,
      f"10pip={lot_10pip}, 20pip={lot_20pip}, ratio={lot_10pip/lot_20pip:.3f}")

# Zero SL → min_lot
lot_zero_sl = rm.calculate_lot_size(
    balance=10_000, entry=1.10000, sl=1.10000,
    tick_value=1.0, tick_size=0.00001,
)
check("Zero SL distance → min_lot (0.01)", lot_zero_sl == 0.01,
      f"got {lot_zero_sl}")

# lot stays within bounds
check("Lot ≥ 0.01 (min_lot)", lot_10pip >= 0.01)
check("Lot ≤ 100.0 (max_lot)", lot_10pip <= 100.0)

# Cap at max_risk_per_trade_pct (2%)
lot_cap = rm.calculate_lot_size(
    balance=10_000, entry=1.10000, sl=1.09900,
    tick_value=1.0, tick_size=0.00001, risk_pct=5.0  # capped to 2%
)
lot_uncapped = rm.calculate_lot_size(
    balance=10_000, entry=1.10000, sl=1.09900,
    tick_value=1.0, tick_size=0.00001, risk_pct=2.0
)
check("Max risk cap applied (5% → 2%)", abs(lot_cap - lot_uncapped) < 0.01,
      f"capped={lot_cap}, uncapped={lot_uncapped}")


# ══════════════════════════════════════════════════════════════════════════════
# 2. SL/TP VALIDATION
# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 2. SL/TP Validation ===")
rm2 = RiskManager(config=RISK_CFG)

# BUY: SL below entry, TP above entry, RR=2.0 (>= 1.5)
ok, msg = rm2.validate_sl_tp("BUY", 1.10000, 1.09900, 1.10200)
check("BUY valid SL/TP (RR=2.0)", ok, msg)

# SELL: SL above entry, TP below entry, RR=2.0
ok, msg = rm2.validate_sl_tp("SELL", 1.10000, 1.10100, 1.09800)
check("SELL valid SL/TP (RR=2.0)", ok, msg)

# Exact minimum RR=1.5 must PASS (float precision fix)
# entry=1.0, sl=0.9990 (10 pip), tp=1.0150 (15 pip) → RR=1.5 exactly
ok, msg = rm2.validate_sl_tp("BUY", 1.00000, 0.99900, 1.00150)
check("RR exact minimum 1.5 → PASS (float fix)", ok, f"msg='{msg}'")

# Slightly below min RR=1.5 must FAIL
ok, msg = rm2.validate_sl_tp("BUY", 1.00000, 0.99900, 1.00140)
check("RR below minimum (1.4) → FAIL", not ok, f"msg='{msg}'")

# BUY SL above entry → FAIL
ok, msg = rm2.validate_sl_tp("BUY", 1.10000, 1.10100, 1.10300)
check("BUY SL above entry → FAIL", not ok, msg)

# SELL SL below entry → FAIL
ok, msg = rm2.validate_sl_tp("SELL", 1.10000, 1.09900, 1.09700)
check("SELL SL below entry → FAIL", not ok, msg)

# Missing SL → FAIL
ok, msg = rm2.validate_sl_tp("BUY", 1.10000, None, 1.10200)
check("Missing SL → FAIL", not ok, msg)

# No TP → PASS (TP is optional)
ok, msg = rm2.validate_sl_tp("BUY", 1.10000, 1.09900, None)
check("No TP → PASS (TP optional)", ok, msg)


# ══════════════════════════════════════════════════════════════════════════════
# 3. DRAWDOWN CIRCUIT BREAKERS
# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 3. Drawdown Circuit Breakers ===")
rm3 = RiskManager(config=RISK_CFG)

# Seed balance
rm3.update_balance(10_000)

# -4% → still allowed
rm3.update_balance(9_600)
allowed, reason = rm3.is_trading_allowed("scalping")
check("-4% drawdown → still allowed", allowed, reason)

# -6% → daily circuit breaker fires (limit=5%)
rm3.update_balance(9_400)
allowed, reason = rm3.is_trading_allowed("scalping")
check("-6% drawdown → circuit breaker active", not allowed, reason)

# Fresh manager: weekly drawdown (-11% > 10% limit)
rm4 = RiskManager(config=RISK_CFG)
rm4.update_balance(10_000)
# Simulate trading happened over several days without daily reset by directly
# touching internals (test-only — actual usage calls update_balance each close)
rm4._week_start_balance = 10_000
rm4.update_balance(8_900)  # -11%
allowed, reason = rm4.is_trading_allowed("day_trading")
check("-11% weekly drawdown → weekly circuit breaker", not allowed, reason)


# ══════════════════════════════════════════════════════════════════════════════
# 4. CONSECUTIVE LOSS PAUSE
# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 4. Consecutive Loss Pause ===")
rm5 = RiskManager(config=RISK_CFG)

# 4 losses → still allowed (limit=5)
for _ in range(4):
    rm5.record_loss("scalping")
allowed, reason = rm5.is_trading_allowed("scalping")
check("4 consecutive losses → still allowed", allowed, reason)

# 5th loss → mode paused
rm5.record_loss("scalping")
allowed, reason = rm5.is_trading_allowed("scalping")
check("5th consecutive loss → mode paused", not allowed, reason)
check("Other mode unaffected", rm5.is_trading_allowed("swing")[0], "swing should be allowed")

# Win resets counter
rm5.record_win("scalping")
# But pause timestamp is already set — pause expires after 4h
# Manually expire the pause to verify reset works
rm5._paused_modes["scalping"] = datetime.now(tz=timezone.utc) - timedelta(hours=5)
rm5.record_win("scalping")  # also zero out counter
allowed, reason = rm5.is_trading_allowed("scalping")
check("Pause expires after 4h → trading allowed", allowed, reason)


# ══════════════════════════════════════════════════════════════════════════════
# 5. P&L DIRECTION MATH
# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 5. P&L Direction Math ===")

def calc_pnl(direction: str, entry: float, close: float,
             lot: float = 1.0, contract: float = 100_000,
             tick_size: float = 0.00001, tick_value: float = 1.0) -> float:
    """Simplified P&L: (close - entry) × lot × contract / tick_size × tick_value"""
    price_diff = close - entry if direction == "BUY" else entry - close
    ticks = price_diff / tick_size
    return round(ticks * tick_value * lot, 2)

# BUY: entry 1.1000, close 1.1010 (+10 pip) = +$100
pnl = calc_pnl("BUY", 1.10000, 1.10100)
check("BUY profit: +10pip = +$100", abs(pnl - 100.0) < 0.01, f"pnl={pnl}")

# BUY: entry 1.1000, close 1.0990 (-10 pip) = -$100
pnl = calc_pnl("BUY", 1.10000, 1.09900)
check("BUY loss: -10pip = -$100", abs(pnl + 100.0) < 0.01, f"pnl={pnl}")

# SELL: entry 1.1000, close 1.0990 (+10 pip) = +$100
pnl = calc_pnl("SELL", 1.10000, 1.09900)
check("SELL profit: close below entry = +$100", abs(pnl - 100.0) < 0.01, f"pnl={pnl}")

# SELL: entry 1.1000, close 1.1010 (-10 pip) = -$100
pnl = calc_pnl("SELL", 1.10000, 1.10100)
check("SELL loss: close above entry = -$100", abs(pnl + 100.0) < 0.01, f"pnl={pnl}")

# Lot proportionality: 2x lot = 2x P&L
pnl_1lot = calc_pnl("BUY", 1.10000, 1.10100, lot=1.0)
pnl_2lot = calc_pnl("BUY", 1.10000, 1.10100, lot=2.0)
check("2x lot → 2x P&L", abs(pnl_2lot / pnl_1lot - 2.0) < 0.001,
      f"1lot={pnl_1lot}, 2lot={pnl_2lot}")


# ══════════════════════════════════════════════════════════════════════════════
# 6. R:R CALCULATION
# ══════════════════════════════════════════════════════════════════════════════
print("\n=== 6. R:R Calculation ===")

def rr(entry: float, sl: float, tp: float, direction: str = "BUY") -> float:
    if direction == "BUY":
        sl_d = entry - sl
        tp_d = tp - entry
    else:
        sl_d = sl - entry
        tp_d = entry - tp
    return tp_d / sl_d if sl_d > 0 else 0.0

check("BUY RR 2:1", abs(rr(1.1000, 1.0990, 1.1020) - 2.0) < 0.001)
check("SELL RR 2:1", abs(rr(1.1000, 1.1010, 1.0980, "SELL") - 2.0) < 0.001)
check("RR 1.5:1 exact", abs(rr(1.0000, 0.9990, 1.0015) - 1.5) < 1e-9)
check("RR 3:1", abs(rr(1.1000, 1.0990, 1.1030) - 3.0) < 0.001)


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
print()
passed = sum(1 for r in results if r[0] == PASS)
failed = sum(1 for r in results if r[0] == FAIL)
print(f"{'='*60}")
print(f"  Results: {passed} passed, {failed} failed  ({len(results)} total)")
print(f"{'='*60}")
if failed:
    print("\nFailed tests:")
    for r in results:
        if r[0] == FAIL:
            print(f"  ✗ {r[1]}: {r[2]}")
    sys.exit(1)
else:
    print("\n  All tests passed!")
    sys.exit(0)
