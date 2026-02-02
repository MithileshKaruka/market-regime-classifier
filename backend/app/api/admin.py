"""Admin API endpoints for system management"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


# Shared state for live ingestion control
class IngestionState:
    """Global state for live ingestion control"""
    paused: bool = False
    paused_at: datetime | None = None
    paused_by: str | None = None


_ingestion_state = IngestionState()


def is_ingestion_paused() -> bool:
    """Check if live ingestion is paused (called by live_ingestion.py)"""
    return _ingestion_state.paused


class IngestionStatusResponse(BaseModel):
    paused: bool
    paused_at: datetime | None = None
    paused_by: str | None = None


class PauseRequest(BaseModel):
    reason: str = "manual"


@router.get("/ingestion/status", response_model=IngestionStatusResponse)
async def get_ingestion_status():
    """Get current live ingestion status"""
    return IngestionStatusResponse(
        paused=_ingestion_state.paused,
        paused_at=_ingestion_state.paused_at,
        paused_by=_ingestion_state.paused_by,
    )


@router.post("/ingestion/pause", response_model=IngestionStatusResponse)
async def pause_ingestion(request: PauseRequest = PauseRequest()):
    """Pause live data ingestion

    Use this before running historical data preload to avoid database conflicts.
    The ingestion loop will continue running but skip processing ticks.
    """
    if _ingestion_state.paused:
        raise HTTPException(status_code=400, detail="Ingestion already paused")

    _ingestion_state.paused = True
    _ingestion_state.paused_at = datetime.utcnow()
    _ingestion_state.paused_by = request.reason

    logger.warning(f"Live ingestion PAUSED by: {request.reason}")

    return IngestionStatusResponse(
        paused=True,
        paused_at=_ingestion_state.paused_at,
        paused_by=_ingestion_state.paused_by,
    )


@router.post("/ingestion/resume", response_model=IngestionStatusResponse)
async def resume_ingestion():
    """Resume live data ingestion

    Call this after historical data preload is complete.
    """
    if not _ingestion_state.paused:
        raise HTTPException(status_code=400, detail="Ingestion is not paused")

    paused_duration = None
    if _ingestion_state.paused_at:
        paused_duration = datetime.utcnow() - _ingestion_state.paused_at

    _ingestion_state.paused = False
    _ingestion_state.paused_at = None
    _ingestion_state.paused_by = None

    logger.warning(f"Live ingestion RESUMED (was paused for {paused_duration})")

    return IngestionStatusResponse(paused=False)
