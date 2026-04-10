"""
Test script for market scanner functionality.
Run this to verify scanner works with MT5 connection.
"""

import asyncio
from engine.mt5_client import MT5Client
from engine.market_scanner import MarketScanner, summary_to_dict


async def test_scanner():
    """Test scanner with real MT5 connection."""
    print("=" * 60)
    print("MARKET SCANNER TEST")
    print("=" * 60)
    
    # 1. Connect to MT5
    print("\n1. Connecting to MT5...")
    mt5_client = MT5Client()
    if not mt5_client.connect():
        print("❌ MT5 connection failed")
        return
    print("✓ MT5 connected")
    
    # 2. Initialize scanner
    print("\n2. Initializing scanner...")
    scanner = MarketScanner(mt5_client)
    print("✓ Scanner initialized")
    
    # 3. Run a quick scan (just scalping for speed)
    print("\n3. Scanning for scalping symbols...")
    print("   (This may take 30-60 seconds...)")
    
    results = await asyncio.to_thread(scanner.scan_type, "scalping", force_refresh=True)
    
    print(f"\n✓ Scan complete! Found {len(results)} viable scalping symbols")
    
    # 4. Display top 10 results
    print("\n" + "=" * 60)
    print("TOP 10 SCALPING SYMBOLS")
    print("=" * 60)
    print(f"{'Rank':<6} {'Symbol':<12} {'Score':<8} {'ATR':<8} {'Spread':<8} {'ADX':<6}")
    print("-" * 60)
    
    for i, result in enumerate(results[:10], 1):
        print(
            f"{i:<6} {result.symbol:<12} {result.composite_score:<8.1f} "
            f"{result.atr_pips:<8.2f} {result.spread_pips:<8.2f} {result.adx:<6.1f}"
        )
    
    # 5. Show score breakdown for top symbol
    if results:
        top = results[0]
        print("\n" + "=" * 60)
        print(f"SCORE BREAKDOWN: {top.symbol}")
        print("=" * 60)
        print(f"Composite Score:     {top.composite_score:.1f} / 100")
        print(f"  Volatility Score:  {top.volatility_score:.1f}")
        print(f"  Spread Score:      {top.spread_score:.1f}")
        print(f"  Trend Score:       {top.trend_score:.1f}")
        print(f"  Liquidity Score:   {top.liquidity_subscore:.1f}")
        print(f"  Momentum Score:    {top.momentum_subscore:.1f}")
        print(f"\nRaw Metrics:")
        print(f"  ATR:               {top.atr_pips:.2f} pips")
        print(f"  Spread:            {top.spread_pips:.2f} pips")
        print(f"  ADX:               {top.adx:.1f}")
        print(f"  Liquidity:         {top.liquidity_score:.1f} / 10")
        print(f"  Momentum:          {top.momentum_score:.1f} / 100")
        print(f"  Category:          {top.category}")
        print(f"  Trading Hours:     {'Active' if top.trading_hours_active else 'Closed'}")
    
    # 6. Test full scan (all trading types)
    print("\n" + "=" * 60)
    print("RUNNING FULL SCAN (ALL TRADING TYPES)")
    print("=" * 60)
    print("This will take 2-3 minutes...")
    
    summary = await asyncio.to_thread(scanner.scan_all, force_refresh=True)
    
    print(f"\n✓ Full scan complete!")
    print(f"  Total scanned:     {summary.total_scanned} symbols")
    print(f"  Total passed:      {summary.total_passed} symbols")
    print(f"  Scan duration:     {summary.scan_duration_seconds:.2f} seconds")
    print(f"\nResults by trading type:")
    for ttype, results_list in summary.trading_types.items():
        print(f"  {ttype:<15} {len(results_list)} symbols")
    
    # 7. Disconnect
    print("\n" + "=" * 60)
    mt5_client.disconnect()
    print("✓ Test complete")


if __name__ == "__main__":
    try:
        asyncio.run(test_scanner())
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
