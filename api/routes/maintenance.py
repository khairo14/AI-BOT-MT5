from __future__ import annotations

from fastapi import APIRouter, Request
from loguru import logger

from ai.maintenance_agent import maintenance_agent

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


def _get_client_from_app(request: Request):
    return getattr(request.app.state, "mt5_client", None)


@router.get("/health")
def health_summary(request: Request):
    try:
        client = _get_client_from_app(request)

        result = maintenance_agent.run_health_audit(
            client=client,
            notify=False,
            dry_run=True,
        )

        return {"success": True, "health": result}

    except Exception as exc:
        logger.exception("Maintenance health audit failed")
        return {"success": False, "error": str(exc)}


@router.get("/audit")
def full_audit(request: Request):
    try:
        client = _get_client_from_app(request)

        result = maintenance_agent.run_health_audit(
            client=client,
            notify=True,
            dry_run=True,
        )

        return {"success": True, "audit": result}

    except Exception as exc:
        logger.exception("Maintenance full audit failed")
        return {"success": False, "error": str(exc)}


@router.get("/dry-run-repairs")
def dry_run_repairs():
    try:
        result = maintenance_agent.dry_run_safe_repairs()
        return {"success": True, "repairs": result}

    except Exception as exc:
        logger.exception("Maintenance dry-run repairs failed")
        return {"success": False, "error": str(exc)}