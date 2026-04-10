"""
Profitability reporting and performance validation API endpoints
"""

from fastapi import APIRouter, Query
from engine.performance_report import PerformanceReporter

router = APIRouter(prefix="/profitability", tags=["Profitability"])


@router.get("/")
async def get_profitability_report(days: int = Query(None, description="Filter trades by last N days")):
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
    return reporter.generate_report(days)


@router.get("/status")
async def get_profitability_status():
    """Quick check of profitability metrics and success criteria"""
    reporter = PerformanceReporter()
    report = reporter.generate_report()
    return {
        "task": "Profitability Validation",
        "status": report["task_2_status"]["status"],
        "message": report["task_2_status"]["message"],
        "overall_metrics": report["overall"],
        "success_criteria": report["success_criteria"],
    }
