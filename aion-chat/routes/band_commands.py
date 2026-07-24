"""Android delivery endpoints for durable Mi Band commands."""

from fastapi import APIRouter

from band_commands import acknowledge_band_command, list_pending_band_commands


router = APIRouter(prefix="/api/health/mi-band/commands", tags=["health"])


@router.get("/pending")
async def pending_band_commands():
    return {"items": await list_pending_band_commands()}


@router.post("/{command_id}/ack")
async def ack_band_command(command_id: str):
    return {"ok": await acknowledge_band_command(command_id)}

