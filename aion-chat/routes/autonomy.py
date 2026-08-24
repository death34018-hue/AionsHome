from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from autonomy import ACTION_DEFS, idle_autonomy_mgr
from autonomy_niches import (
    delete_niche_card,
    list_niche_cards,
    set_niche_card_mentioned,
)
from autonomy_state import (
    ACTOR_IDS,
    autonomy_status_payload,
    get_actor_config,
    update_actor_config,
)
from ws import manager


router = APIRouter(prefix="/api/idle-autonomy", tags=["idle-autonomy"])


class ActorConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    min_interval_minutes: Optional[int] = None
    max_interval_minutes: Optional[int] = None
    actions: Optional[dict[str, bool]] = None


class NicheCardUpdate(BaseModel):
    mentioned: bool


class RelationshipDateUpdate(BaseModel):
    started_on: date


def _actor(value: str) -> str:
    actor = str(value or "").strip().lower()
    if actor not in ACTOR_IDS:
        raise HTTPException(status_code=404, detail="unknown actor")
    return actor


@router.get("")
async def read_autonomy_status():
    payload = await autonomy_status_payload()
    payload["actions"] = [
        {"key": key, "label": label}
        for key, label in ACTION_DEFS.items()
        if key != "rest"
    ]
    return payload


@router.get("/niches")
async def read_niche_cards(actor: str, limit: int = 60):
    actor = _actor(actor)
    return {
        "actor": actor,
        "cards": await list_niche_cards(actor, limit=limit),
    }


@router.delete("/niches/{card_id}")
async def remove_niche_card(card_id: str, actor: str):
    actor = _actor(actor)
    if not await delete_niche_card(actor, card_id):
        raise HTTPException(status_code=404, detail="niche card not found")
    return {"ok": True, "id": card_id}


@router.patch("/niches/{card_id}")
async def update_niche_card(card_id: str, actor: str, body: NicheCardUpdate):
    actor = _actor(actor)
    card = await set_niche_card_mentioned(actor, card_id, body.mentioned)
    if not card:
        raise HTTPException(status_code=404, detail="niche card not found")
    return {"ok": True, "card": card}


@router.get("/{actor}/config")
async def read_actor_config(actor: str):
    return await get_actor_config(_actor(actor))


@router.put("/{actor}/relationship-date")
async def update_relationship_date(actor: str, body: RelationshipDateUpdate):
    actor = _actor(actor)
    if body.started_on > date.today():
        raise HTTPException(status_code=422, detail="relationship date cannot be in the future")
    config = await update_actor_config(
        actor,
        relationship_started_on=body.started_on.isoformat(),
    )
    payload = await autonomy_status_payload()
    await manager.broadcast({"type": "autonomy_state_changed", "data": payload})
    return {"ok": True, "config": config, "status": payload}


@router.put("/{actor}/config")
async def update_actor(actor: str, body: ActorConfigUpdate):
    actor = _actor(actor)
    config = await update_actor_config(
        actor,
        enabled=body.enabled,
        min_interval_minutes=body.min_interval_minutes,
        max_interval_minutes=body.max_interval_minutes,
        actions=body.actions,
    )
    payload = await autonomy_status_payload()
    await manager.broadcast({"type": "autonomy_state_changed", "data": payload})
    return {"ok": True, "config": config, "status": payload}


@router.post("/{actor}/run-once")
async def run_actor_once(actor: str):
    return await idle_autonomy_mgr.run_actor_once(_actor(actor), manual=True)
