"""
EVOTRADE-AI — Phase 1 Connection Test
Run this to verify MT5 connects, credentials work, and data is flowing.
"""

from engine.mt5_client import MT5Client


def main():
    print("=" * 60)
    print("EVOTRADE-AI — Phase 1 Connection Test")
    print("=" * 60)

    with MT5Client() as client:
        # 1. Account info
        print("\n[1] Account Info")
        info = client.get_account_info()
        if info:
            for k, v in info.items():
                print(f"    {k:20s}: {v}")
        else:
            print("    FAILED — check credentials in .env")
            return

        # 2. Symbol info
        print("\n[2] Symbol Info — EURUSD")
        sym = client.get_symbol_info("EURUSD")
        if sym:
            for k, v in sym.items():
                print(f"    {k:20s}: {v}")

        # 3. Current price
        print("\n[3] Current Price — EURUSD")
        price = client.get_current_price("EURUSD")
        if price:
            print(f"    Bid: {price['bid']}  Ask: {price['ask']}  Time: {price['time']}")

        # 4. OHLCV data
        print("\n[4] OHLCV — EURUSD H1 (last 5 candles)")
        df = client.get_ohlcv("EURUSD", "H1", count=5)
        if df is not None:
            print(df.to_string(index=False))

        # 5. Open positions
        print("\n[5] Open Positions")
        positions = client.get_open_positions()
        if positions:
            for p in positions:
                print(f"    #{p['ticket']} {p['symbol']} {p['type']} "
                      f"{p['volume']} lots | P&L: {p['profit']}")
        else:
            print("    No open positions.")

        print("\n✓ Phase 1 connection test complete.")
        print("=" * 60)


if __name__ == "__main__":
    main()
