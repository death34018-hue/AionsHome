"""HTTP surface for Android widget state and dynamic character assets."""

from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from widget_control import (
    ACTOR_IDS,
    broadcast_widget_state_changed,
    widget_asset_catalog,
    widget_control_store,
)


router = APIRouter()
catalog = widget_asset_catalog
store = widget_control_store
broadcast_state = broadcast_widget_state_changed


class ActorStateUpdate(BaseModel):
    state: str


async def state_payload() -> dict:
    state = await store.get_state()
    snapshot = catalog.snapshot()
    actors = {}
    for actor_id in ACTOR_IDS:
        actor = snapshot.get(actor_id, {"name": "AI", "states": [], "assets": {}})
        current = str(state["actor_states"].get(actor_id) or "")
        asset = actor.get("assets", {}).get(current)
        actors[actor_id] = {
            "name": actor.get("name") or "AI",
            "current_state": current,
            "states": actor.get("states") or [],
            "asset": ({
                "url": f"/api/widget-control/assets/{actor_id}/{quote(current, safe='')}",
                "version": asset["version"],
            } if asset else None),
        }
    banner_asset = catalog.banner_asset()
    return {
        "actors": actors,
        "banner": state["banner"],
        "banner_asset": ({
            "url": "/api/widget-control/banner/image",
            "version": banner_asset["version"],
        } if banner_asset else None),
        "revision": state["revision"],
        "updated_at": state["updated_at"],
    }


@router.get("/api/widget-control/state")
async def read_widget_state():
    return await state_payload()


@router.get("/api/widget-control/assets/{actor_id}/{state_name}")
async def read_widget_asset(actor_id: str, state_name: str):
    if actor_id not in ACTOR_IDS:
        raise HTTPException(status_code=404, detail="unknown widget actor")
    asset = catalog.asset(actor_id, state_name)
    if not asset:
        raise HTTPException(status_code=404, detail="unknown widget state")
    return FileResponse(asset["path"], media_type="image/png")


@router.get("/api/widget-control/banner/image")
async def read_widget_banner_image():
    asset = catalog.banner_asset()
    if not asset:
        raise HTTPException(status_code=404, detail="widget banner image missing")
    return FileResponse(asset["path"], media_type="image/png")


@router.patch("/api/widget-control/actors/{actor_id}")
async def update_widget_actor(actor_id: str, body: ActorStateUpdate):
    try:
        state = await store.set_actor_state(actor_id, body.state.strip())
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown widget state")
    await broadcast_state(state)
    return await state_payload()


@router.post("/api/widget-control/banner/clear")
async def clear_widget_banner():
    before = await store.get_state()
    state = await store.clear_banner()
    if state["revision"] != before["revision"]:
        await broadcast_state(state)
    return await state_payload()
