from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from capabilities import capabilities_payload, set_capability_enabled
from proactive_companionship import (
    proactive_status_payload,
    set_proactive_enabled,
)
from ws import manager


router = APIRouter()


class CapabilityToggle(BaseModel):
    enabled: bool


@router.get("/api/capabilities")
async def get_capabilities():
    return capabilities_payload()


@router.put("/api/capabilities/{key}")
async def update_capability(key: str, body: CapabilityToggle):
    try:
        item = set_capability_enabled(key, body.enabled)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown capability")
    await manager.broadcast({
        "type": "capability_config_changed",
        "data": item,
    })
    if key == "app_supervision" and not item["enabled"]:
        from app_supervision_ai import state_cache
        state_cache.clear()
    if key == "health_context":
        await manager.broadcast({
            "type": "health_share_changed",
            "data": {"health_share_enabled": item["enabled"]},
        })
    return {"ok": True, "capability": item, "payload": capabilities_payload()}


@router.get("/api/proactive-companionship")
async def get_proactive_companionship():
    return await proactive_status_payload()


@router.put("/api/proactive-companionship/{actor}")
async def update_proactive_companionship(actor: str, body: CapabilityToggle):
    actor = actor.strip().lower()
    if actor not in ("aion", "connor"):
        raise HTTPException(status_code=404, detail="unknown actor")
    set_proactive_enabled(actor, body.enabled)
    payload = await proactive_status_payload()
    await manager.broadcast({"type": "proactive_companionship_changed", "data": payload})
    return {"ok": True, "data": payload}
