from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from capabilities import capabilities_payload, set_capability_enabled
from proactive_companionship import (
    proactive_status_payload,
    set_proactive_enabled,
)
from ws import manager


router = APIRouter()


class SvakomStateReport(BaseModel):
    source: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    sequence: int = Field(ge=0, strict=True)
    connected: bool = Field(strict=True)
    flap: int = Field(ge=0, le=7, strict=True)
    vibrate: int = Field(ge=0, le=10, strict=True)
    vibrate_level: int = Field(ge=0, le=10, strict=True)


@router.post('/api/svakom-ai/state')
async def report_svakom_state(body: SvakomStateReport):
    from svakom_ai import report_state
    if body.connected and body.vibrate and not body.vibrate_level:
        raise HTTPException(status_code=422, detail='running vibration requires a level')
    report_state(body.model_dump())
    return {'ok': True}


@router.get("/api/svakom-ai")
async def get_svakom_ai():
    from svakom_ai import state
    return state()


@router.post("/api/svakom-ai/takeover")
async def takeover_svakom_ai():
    from svakom_ai import invalidate_permission
    current = invalidate_permission()
    await manager.broadcast({'type': 'svakom_revoked', 'data': current})
    return current


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


class ToySelection(BaseModel):
    profile: str


@router.get('/api/toys/selection')
async def get_toy_selection():
    import toy_profiles
    return toy_profiles.state()


@router.put('/api/toys/selection')
async def select_toy(body: ToySelection):
    import toy_profiles
    try:
        current = toy_profiles.select(body.profile)
    except ValueError as error:
        raise HTTPException(422, str(error))
    await manager.broadcast({'type': 'toy_profile_changed', 'data': current})
    return current


@router.get('/api/ankni-ai')
async def get_ankni_ai():
    from ankni_ai import state
    return state()


@router.post('/api/ankni-ai/takeover')
async def takeover_ankni_ai():
    from ankni_ai import invalidate_permission
    current = invalidate_permission()
    await manager.broadcast({'type': 'ankni_revoked', 'data': current})
    return current
