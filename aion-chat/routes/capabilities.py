from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from capabilities import capabilities_payload, set_capability_enabled
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
    if key == "health_context":
        await manager.broadcast({
            "type": "health_share_changed",
            "data": {"health_share_enabled": item["enabled"]},
        })
    return {"ok": True, "capability": item, "payload": capabilities_payload()}
