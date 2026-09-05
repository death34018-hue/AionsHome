"""Standalone shopping API: only verified search candidates can be collected."""
import asyncio
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from taobao_shopping import ACTIVE_TRIPS, SHOPPING_LOCK, check_connection, get_store, roam, search_and_record

router = APIRouter(prefix="/api/taobao", tags=["taobao"])
_tasks: set[asyncio.Task] = set()
_last_run = None


class SearchBody(BaseModel):
    keyword: str = Field(min_length=1, max_length=120)


class ActorBody(BaseModel):
    actor: Literal["aion", "connor"]


class SaveBody(ActorBody):
    candidate_id: str = Field(min_length=1, max_length=64)
    reflection: str = Field(default="", max_length=2000)
    purpose: str = Field(default="", max_length=2000)
    recipient: str = Field(default="", max_length=200)


class SettingsBody(BaseModel):
    transport: Literal["native_bridge", "http"]
    url: str = Field(max_length=500)
    autonomy_enabled: bool = True


@router.get("/state")
async def state():
    from chatroom import get_chatroom_names
    _, first, second = get_chatroom_names()
    store = await get_store()
    return {"names": {"aion": first, "connor": second}, "items": await store.list_items(),
            "settings": await store.settings(), "busy": SHOPPING_LOCK.locked(), "last_run": _last_run,
            "trips": await store.list_trips(), "active_trips": list(ACTIVE_TRIPS.values()),
            "portraits": {"aion": "/api/widget-control/assets/aion/索吻", "connor": "/api/widget-control/assets/connor/查岗"},
            "avatars": {"aion": "/public/gropicon1.png", "connor": "/public/codexicon.png"}}


@router.get("/trips")
async def trips(offset: int = Query(default=0, ge=0)):
    return {"trips": await (await get_store()).list_trips(offset=offset)}


@router.get("/trips/{trip_id}")
async def trip(trip_id: str):
    try:
        return await (await get_store()).get_trip(trip_id)
    except KeyError:
        raise HTTPException(404, "这次逛街记录不存在")


@router.delete("/trips/{trip_id}")
async def delete_trip(trip_id: str):
    if trip_id in ACTIVE_TRIPS:
        raise HTTPException(409, "这次逛街还没有结束，暂时不能删除")
    try:
        await (await get_store()).delete_trip(trip_id)
        return {"deleted": True, "wishlist_changed": False}
    except KeyError:
        raise HTTPException(404, "这次逛街记录已不存在")


@router.put("/settings")
async def settings(body: SettingsBody):
    if SHOPPING_LOCK.locked():
        raise HTTPException(409, "正在逛淘宝，请结束后再修改连接设置")
    try:
        return await (await get_store()).set_settings(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/check")
async def check():
    try:
        return await check_connection(await (await get_store()).settings())
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))


@router.post("/search")
async def search(body: SearchBody):
    if SHOPPING_LOCK.locked():
        raise HTTPException(409, "正在逛淘宝，请稍后再试")
    async with SHOPPING_LOCK:
        try:
            return await search_and_record(await get_store(), body.keyword)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))


async def _run(actor):
    global _last_run
    try:
        result = await roam(actor, await get_store())
        _last_run = {"actor": actor, "ok": True, "message": result["message"], "at": time.time()}
    except asyncio.CancelledError:
        _last_run = {"actor": actor, "ok": False, "message": "本次逛淘宝被服务中断。", "at": time.time()}
        raise
    except Exception as exc:
        _last_run = {"actor": actor, "ok": False, "message": str(exc), "at": time.time()}
    finally:
        SHOPPING_LOCK.release()


@router.post("/roam", status_code=202)
async def start_roam(body: ActorBody):
    global _last_run
    if SHOPPING_LOCK.locked():
        raise HTTPException(409, "正在逛淘宝，请稍后再试")
    await SHOPPING_LOCK.acquire()
    _last_run = {"actor": body.actor, "ok": None, "message": "正在结合近期经历挑选搜索词…", "at": time.time()}
    task = asyncio.create_task(_run(body.actor))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"started": True}


@router.post("/items")
async def save(body: SaveBody):
    try:
        return await (await get_store()).save_item(body.actor, body.candidate_id,
            reflection=body.reflection, purpose=body.purpose, recipient=body.recipient)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@router.delete("/items/{item_id}")
async def delete(item_id: str):
    try:
        await (await get_store()).delete_item(item_id)
        return {"deleted": True, "taobao_changed": False}
    except KeyError:
        raise HTTPException(404, "收藏已不存在")
