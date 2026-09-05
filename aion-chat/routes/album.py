"""用户相册接口；AI 仅在启用的自主行动中读取家庭相册。"""

import asyncio

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from album import MAX_IMAGE_BYTES, get_album_store

router = APIRouter(prefix="/api/album", tags=["album"])


def display_photo(photo):
    from chatroom import get_chatroom_names
    user_name, ai_name, companion_name = get_chatroom_names()
    names = {"user": user_name, "aion": ai_name, "connor": companion_name}
    photo["actor_name"] = names.get(photo["actor"], "AI") if photo["source"] == "generated" else user_name
    photo["prompt_display"] = photo["prompt"] or "（无）"
    return photo


def album_categories():
    from chatroom import get_chatroom_names
    _, ai_name, companion_name = get_chatroom_names()
    return [{"id": "family", "name": "家庭相册"},
            {"id": "aion", "name": f"{ai_name}相册"},
            {"id": "connor", "name": f"{companion_name}相册"}]


@router.get("/photos")
async def list_photos(offset: int = Query(0, ge=0), limit: int = Query(60, ge=1, le=100),
                      source: str = Query("", pattern="^(generated|upload)?$"),
                      favorite: bool = False, query: str = Query("", max_length=200),
                      album_id: str = Query("", pattern="^(family|aion|connor)?$")):
    result = await asyncio.to_thread(get_album_store().list_photos, offset=offset, limit=limit,
                                    source=source, favorite=favorite, query=query, album_id=album_id)
    result["photos"] = [display_photo(photo) for photo in result["photos"]]
    result["albums"] = album_categories()
    return result


@router.post("/upload")
async def upload_photo(file: UploadFile = File(...), taken_on: str = Form(""), album_id: str = Form("family")):
    try:
        content = await file.read(MAX_IMAGE_BYTES + 1)
        photo = await asyncio.to_thread(get_album_store().save_photo, content, source="upload",
                                        actor="user", original_name=file.filename or "",
                                        taken_on=taken_on, album_id=album_id)
        return display_photo(photo)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        await file.close()


@router.get("/photos/{photo_id}")
async def get_photo(photo_id: str):
    store = get_album_store()
    photo = await asyncio.to_thread(store.get_photo, photo_id)
    if not photo:
        raise HTTPException(404, "照片已移出相册或不存在")
    from chatroom import get_chatroom_names
    _, ai_name, companion_name = get_chatroom_names()
    names = {"aion": ai_name, "connor": companion_name}
    views = await asyncio.to_thread(store.get_photo_views, photo_id)
    photo["viewed_by"] = [{**view, "name": names[view["actor"]]} for view in views if view["actor"] in names]
    return display_photo(photo)


class PhotoMove(BaseModel):
    photo_ids: list[str] = Field(min_length=1, max_length=1000)
    album_id: str = Field(pattern="^(family|aion|connor)$")


@router.post("/photos/move")
async def move_photos(body: PhotoMove):
    moved = await asyncio.to_thread(get_album_store().move_photos, body.photo_ids, body.album_id)
    return {"moved": moved, "skipped": len(set(body.photo_ids)) - moved}


class PhotoUpdate(BaseModel):
    title: str | None = Field(None, max_length=160)
    note: str | None = Field(None, max_length=5000)
    taken_on: str | None = None
    favorite: bool | None = None
    album_id: str | None = Field(None, pattern="^(family|aion|connor)$")


@router.put("/photos/{photo_id}")
async def update_photo(photo_id: str, body: PhotoUpdate):
    try:
        photo = await asyncio.to_thread(get_album_store().update_photo, photo_id, **body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not photo:
        raise HTTPException(404, "照片已移出相册或不存在")
    return display_photo(photo)


@router.delete("/photos/{photo_id}")
async def remove_photo(photo_id: str):
    removed = await asyncio.to_thread(get_album_store().remove_photo, photo_id)
    if not removed:
        raise HTTPException(404, "照片已移出相册或不存在")
    return {"ok": True, "files_preserved": True}


@router.get("/photos/{photo_id}/thumbnail")
async def thumbnail(photo_id: str):
    store = get_album_store()
    photo = await asyncio.to_thread(store.get_photo, photo_id)
    if not photo:
        raise HTTPException(404, "照片不存在")
    path = store.thumbnails_dir / (photo["id"] + ".webp")
    if not path.is_file():
        raise HTTPException(404, "缩略图文件已被手动删除")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/photos/{photo_id}/reference")
async def reference(photo_id: str):
    store = get_album_store()
    photo = await asyncio.to_thread(store.get_photo, photo_id)
    if not photo or not photo["reference_filename"]:
        raise HTTPException(404, "未保存参考图")
    path = store.references_dir / photo["reference_filename"]
    if not path.is_file():
        raise HTTPException(404, "参考图文件已被手动删除")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/photos/{photo_id}/download")
async def download(photo_id: str):
    store = get_album_store()
    photo = await asyncio.to_thread(store.get_photo, photo_id)
    if not photo:
        raise HTTPException(404, "照片不存在")
    path = store.images_dir / photo["filename"]
    if not path.is_file():
        raise HTTPException(404, "原图文件已被手动删除")
    return FileResponse(path, filename=photo["filename"])
