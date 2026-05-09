"""
Profitability reporting and performance validation API endpoints
"""

from fastapi import APIRouter, Query
from engine.performance_report import PerformanceReporter
from engine.account_store import current_mode as _cur_mode

router = APIRouter(tags=["Profitability"])


@router.get("/")
async def get_profitability_report(
    days: int = Query(None, description="Filter trades by last N days"),
    account: str = Query("all", description="Filter by account: demo, live, or all"),
    account_login: int = Query(None, description="Filter by specific MT5 account login number"),
):
    """
    Get comprehensive profitability report

    Returns:
    - Overall metrics (win rate, profit factor, total trades)
    - Breakdown by symbol
    - Breakdown by trading type
    - Success criteria validation
    - System status
    """
    reporter = PerformanceReporter()
    mode = "all" if account == "all" else account
    return reporter.generate_report(days, mode=mode, account_login=account_login)


@router.get("/status")
async def get_profitability_status(
    account: str = Query("all", description="Filter by account: demo, live, or all"),
    account_login: int = Query(None, description="Filter by specific MT5 account login number"),
):
    """Quick check of profitability metrics and success criteria"""
    reporter = PerformanceReporter()
    mode = "all" if account == "all" else account
    report = reporter.generate_report(mode=mode , account_login=account_login)
    
    return {
        "task": "Profitability Validation",
        "status": report["task_2_status"]["status"],
        "message": report["task_2_status"]["message"],
        "overall_metrics": report["overall"],
        "success_criteria": report["success_criteria"],
    }