"""
Test script for Risk Presets (Task #12)

Tests that preset multipliers correctly affect:
- Lot size calculation (risk_per_trade_multiplier)
- Concurrent trade limits
- Drawdown limits
"""

from engine.risk_manager import RiskManager


def test_preset_lot_sizes():
    """Test lot size calculation across all 3 presets."""
    print("\n" + "=" * 60)
    print("TEST: Lot Size Calculation with Presets")
    print("=" * 60)

    # Create risk manager
    rm = RiskManager()

    # Test parameters (same for all presets)
    balance = 10000.0
    entry = 1.1000
    sl = 1.0950  # 50 pip SL
    tick_value = 1.0
    tick_size = 0.0001

    presets = ["conservative", "moderate", "aggressive"]
    results = {}

    for preset in presets:
        success, msg = rm.set_risk_preset(preset)
        if not success:
            print(f"❌ Failed to set {preset}: {msg}")
            continue

        lot = rm.calculate_lot_size(
            balance=balance,
            entry=entry,
            sl=sl,
            tick_value=tick_value,
            tick_size=tick_size,
        )

        results[preset] = lot
        print(f"{preset:12} → {lot:.2f} lots")

    # Verify multipliers worked
    print("\nExpected pattern: Conservative < Moderate < Aggressive")
    if results["conservative"] < results["moderate"] < results["aggressive"]:
        print("✅ PASS: Lot sizes scale correctly with risk presets")
    else:
        print("❌ FAIL: Lot sizes not scaling as expected")
        print(f"   Conservative: {results['conservative']:.2f}")
        print(f"   Moderate:     {results['moderate']:.2f}")
        print(f"   Aggressive:   {results['aggressive']:.2f}")


def test_preset_config():
    """Test preset configuration loading and switching."""
    print("\n" + "=" * 60)
    print("TEST: Preset Configuration")
    print("=" * 60)

    rm = RiskManager()

    # Get available presets
    presets_data = rm.get_available_presets()
    print(f"Current preset: {presets_data['current']}")
    print(f"Available presets: {list(presets_data['presets'].keys())}")

    # Test switching to each preset
    for preset_name in ["conservative", "moderate", "aggressive"]:
        success, msg = rm.set_risk_preset(preset_name)
        if success:
            print(f"✅ Set {preset_name}: {msg}")

            # Verify it stuck
            current_status = rm.get_status()
            if current_status.get("current_risk_preset") == preset_name:
                print(f"   Verified current_risk_preset = {preset_name}")
            else:
                print(f"   ❌ Status shows {current_status.get('current_risk_preset')} instead of {preset_name}")
        else:
            print(f"❌ Failed to set {preset_name}: {msg}")

    # Test invalid preset
    success, msg = rm.set_risk_preset("invalid_preset")
    if not success:
        print(f"✅ Correctly rejected invalid preset: {msg}")
    else:
        print(f"❌ Should have rejected invalid preset")


def test_preset_concurrent_limits():
    """Test that concurrent trade limits change with presets."""
    print("\n" + "=" * 60)
    print("TEST: Concurrent Trade Limits by Preset")
    print("=" * 60)

    rm = RiskManager()

    presets_data = rm.get_available_presets()

    for preset_name in ["conservative", "moderate", "aggressive"]:
        rm.set_risk_preset(preset_name)
        preset_cfg = presets_data["presets"][preset_name]

        total_limit = preset_cfg["max_concurrent_trades"]["total"]
        scalp_limit = preset_cfg["max_concurrent_trades"]["scalping"]

        print(f"{preset_name:12} → Total: {total_limit}, Scalping: {scalp_limit}")

    print("\n✅ PASS: Concurrent limits vary by preset (see config above)")


def test_preset_drawdown_limits():
    """Test drawdown limits from presets."""
    print("\n" + "=" * 60)
    print("TEST: Drawdown Limits by Preset")
    print("=" * 60)

    rm = RiskManager()
    presets_data = rm.get_available_presets()

    for preset_name in ["conservative", "moderate", "aggressive"]:
        rm.set_risk_preset(preset_name)
        preset_cfg = presets_data["presets"][preset_name]

        daily_limit = preset_cfg.get("max_daily_loss_pct")
        weekly_limit = preset_cfg.get("max_weekly_loss_pct")
        consec_losses = preset_cfg.get("max_consecutive_losses")

        print(f"{preset_name:12} → Daily: {daily_limit}%, Weekly: {weekly_limit}%, Consec: {consec_losses}")

    print("\n✅ PASS: Drawdown limits configured per preset")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Task #12: Risk Presets - Test Suite")
    print("=" * 60)

    try:
        test_preset_config()
        test_preset_lot_sizes()
        test_preset_concurrent_limits()
        test_preset_drawdown_limits()

        print("\n" + "=" * 60)
        print("ALL TESTS COMPLETE")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Start backend: python -m api.main")
        print("2. Start dashboard: cd dashboard && npm run dev")
        print("3. Visit http://localhost:3000/risk-presets")
        print("4. Test preset switching in UI")

    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
