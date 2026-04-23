"""
Performance Report Generator for Profitability Tracking

Analyzes trade_journal.jsonl to track:
- Win rate per symbol + trading type
- Profit factor
- Total trades
- Compliance with success criteria
"""

import json
from pathlib import Path
from typing import Dict, List, Any
from collections import defaultdict
from datetime import datetime, timedelta


class PerformanceReporter:
    def __init__(self, journal_path: str = "data/trade_journal.jsonl"):
        self.journal_path = Path(journal_path)
        
    def load_trades(self, days: int = None, mode: str = None) -> List[Dict[str, Any]]:
        """Load all trades, optionally filtered by date and account mode."""
        if not self.journal_path.exists():
            return []
            
        trades = []
        cutoff = datetime.now() - timedelta(days=days) if days else None
        
        with open(self.journal_path, "r") as f:
            for line in f:
                try:
                    trade = json.loads(line.strip())
                    # Only count closed trades with a real profit value
                    if trade.get("event") != "close" or trade.get("profit") is None:
                        continue
                    # Filter by account mode when specified
                    if mode and trade.get("account_mode") != mode:
                        continue
                    if cutoff:
                        close_time_raw = trade.get("close_time")
                        if not close_time_raw:
                            continue
                        trade_time = datetime.fromisoformat(close_time_raw)
                        if trade_time < cutoff:
                            continue
                    trades.append(trade)
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                    
        return trades
    
    def calculate_metrics(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate overall performance metrics"""
        if not trades:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "total_profit": 0.0,
                "avg_profit": 0.0,
                "avg_loss": 0.0,
            }
        
        # Filter out trades with None or missing profit
        valid_trades = [t for t in trades if t.get("profit") is not None]
        if not valid_trades:
            return {
                "total_trades": len(trades),
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "total_profit": 0.0,
                "avg_profit": 0.0,
                "avg_loss": 0.0,
            }
        
        wins = [t for t in valid_trades if t.get("profit", 0) > 0]
        losses = [t for t in valid_trades if t.get("profit", 0) < 0]
        breakeven = [t for t in valid_trades if t.get("profit", 0) == 0]
        
        total_profit = sum(t.get("profit", 0) for t in wins)
        total_loss = abs(sum(t.get("profit", 0) for t in losses))
        
        win_rate = (len(wins) / len(valid_trades)) * 100 if valid_trades else 0
        profit_factor = total_profit / total_loss if total_loss > 0 else 0
        avg_profit = total_profit / len(wins) if wins else 0
        avg_loss = total_loss / len(losses) if losses else 0
        
        return {
            "total_trades": len(valid_trades),
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": len(breakeven),
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "total_profit": round(sum(t.get("profit", 0) for t in valid_trades), 2),
            "avg_profit": round(avg_profit, 2),
            "avg_loss": round(avg_loss, 2),
        }
    
    def breakdown_by_symbol(self, trades: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Breakdown performance by symbol"""
        by_symbol = defaultdict(list)
        for trade in trades:
            symbol = trade.get("symbol", "UNKNOWN")
            by_symbol[symbol].append(trade)
        
        return {
            symbol: self.calculate_metrics(symbol_trades)
            for symbol, symbol_trades in by_symbol.items()
        }
    
    def breakdown_by_trading_type(self, trades: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Breakdown performance by trading type"""
        by_type = defaultdict(list)
        for trade in trades:
            trading_type = trade.get("trading_type", "UNKNOWN")
            by_type[trading_type].append(trade)
        
        return {
            trading_type: self.calculate_metrics(type_trades)
            for trading_type, type_trades in by_type.items()
        }
    
    def validate_success_criteria(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Check if Task #2 success criteria are met"""
        return {
            "50_trades": metrics["total_trades"] >= 50,
            "win_rate_50": metrics["win_rate"] >= 50.0,
            "win_rate_55": metrics["win_rate"] >= 55.0,
            "profit_factor_1_5": metrics["profit_factor"] >= 1.5,
            "all_criteria_met": (
                metrics["total_trades"] >= 50
                and metrics["win_rate"] >= 50.0
                and metrics["profit_factor"] >= 1.5
            ),
        }
    
    def generate_report(self, days: int = None, mode: str = None) -> Dict[str, Any]:
        """Generate full performance report"""
        trades = self.load_trades(days, mode=mode)
        overall_metrics = self.calculate_metrics(trades)
        by_symbol = self.breakdown_by_symbol(trades)
        by_type = self.breakdown_by_trading_type(trades)
        success = self.validate_success_criteria(overall_metrics)
        
        return {
            "generated_at": datetime.now().isoformat(),
            "period_days": days or "all",
            "overall": overall_metrics,
            "by_symbol": by_symbol,
            "by_trading_type": by_type,
            "success_criteria": success,
            "task_2_status": self._get_task_status(overall_metrics, success),
        }
    
    def _get_task_status(self, metrics: Dict[str, Any], success: Dict[str, Any]) -> Dict[str, str]:
        """Generate Task #2 status summary"""
        if success["all_criteria_met"]:
            return {
                "status": "PASSED",
                "message": "✅ All success criteria met! Ready to proceed to multi-user features.",
                "recommendation": "Begin Phase 2: Production Stability (Tasks 6-11)",
            }
        
        progress_msg = []
        if not success["50_trades"]:
            remaining = 50 - metrics["total_trades"]
            progress_msg.append(f"Need {remaining} more trades (currently {metrics['total_trades']}/50)")
        if not success["win_rate_50"]:
            progress_msg.append(f"Win rate: {metrics['win_rate']}% (need 50%+)")
        if not success["profit_factor_1_5"]:
            progress_msg.append(f"Profit factor: {metrics['profit_factor']} (need 1.5+)")
        
        return {
            "status": "IN_PROGRESS",
            "message": "⏳ " + " | ".join(progress_msg),
            "recommendation": "Continue running paper trades with scanner-discovered symbols.",
        }
    
    def print_report(self, days: int = None):
        """Print formatted report to console"""
        report = self.generate_report(days)
        
        print("\n" + "=" * 80)
        print(f"📊 PROFITABILITY VALIDATION REPORT")
        print(f"Generated: {report['generated_at']}")
        print(f"Period: {report['period_days']} days" if report['period_days'] != "all" else "Period: All time")
        print("=" * 80)
        
        # Overall metrics
        overall = report["overall"]
        print(f"\n🎯 OVERALL PERFORMANCE:")
        print(f"   Total Trades: {overall['total_trades']}")
        print(f"   Win Rate: {overall['win_rate']}% ({overall['wins']}W / {overall['losses']}L / {overall['breakeven']}BE)")
        print(f"   Profit Factor: {overall['profit_factor']}")
        print(f"   Total Profit: ${overall['total_profit']}")
        print(f"   Avg Win: ${overall['avg_profit']} | Avg Loss: ${overall['avg_loss']}")
        
        # Success criteria
        success = report["success_criteria"]
        status = report["task_2_status"]
        print(f"\n✅ SUCCESS CRITERIA:")
        print(f"   [ {'✓' if success['50_trades'] else ' '} ] 50+ trades: {overall['total_trades']}/50")
        print(f"   [ {'✓' if success['win_rate_50'] else ' '} ] Win rate ≥50%: {overall['win_rate']}%")
        print(f"   [ {'✓' if success['win_rate_55'] else ' '} ] Win rate ≥55%: {overall['win_rate']}%")
        print(f"   [ {'✓' if success['profit_factor_1_5'] else ' '} ] Profit factor ≥1.5: {overall['profit_factor']}")
        print(f"\n{status['status']}: {status['message']}")
        print(f"Recommendation: {status['recommendation']}")
        
        # By trading type
        print(f"\n📈 BY TRADING TYPE:")
        for trading_type, metrics in report["by_trading_type"].items():
            print(f"   {trading_type.upper()}:")
            print(f"      Trades: {metrics['total_trades']} | Win Rate: {metrics['win_rate']}% | PF: {metrics['profit_factor']} | Profit: ${metrics['total_profit']}")
        
        # Top 5 symbols
        print(f"\n🏆 TOP 5 SYMBOLS (by profit):")
        by_symbol = sorted(
            report["by_symbol"].items(),
            key=lambda x: x[1]["total_profit"],
            reverse=True
        )[:5]
        for symbol, metrics in by_symbol:
            print(f"   {symbol}: {metrics['total_trades']} trades | {metrics['win_rate']}% WR | ${metrics['total_profit']} profit")
        
        print("\n" + "=" * 80)


if __name__ == "__main__":
    import sys
    
    days = None
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            print("Usage: python performance_report.py [days]")
            sys.exit(1)
    
    reporter = PerformanceReporter()
    reporter.print_report(days)
