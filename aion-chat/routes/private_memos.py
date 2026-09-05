"""快捷备忘录：供桌面小组件与 App 共用，不自动注入 AI 上下文。"""

import time
import uuid
from typing import Annotated, Literal, Optional

import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from database import get_db
from ws import manager


router = APIRouter()
MemoStatus = Literal["active", "completed"]


class PrivateMemoCreate(BaseModel):
    id: str = ""
    content: str
    source: str = "app"
    status: MemoStatus = "active"
    created_at: Optional[float] = None
    updated_at: Optional[float] = None


class PrivateMemoUpdate(BaseModel):
    content: Optional[str] = None
    status: Optional[MemoStatus] = None


def _clean_content(content: str) -> str:
    value = (content or "").strip()
    if not value:
        raise HTTPException(status_code=422, detail="备忘内容不能为空")
    return value


async def _get_memo(db, memo_id: str):
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM private_memos WHERE id=?", (memo_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def _notify_changed():
    await manager.broadcast({"type": "private_memos_changed"})


@router.get("/api/private-memos")
async def list_private_memos(
    status: Annotated[MemoStatus, Query()] = "active",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM private_memos WHERE status=? "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (status, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]


@router.post("/api/private-memos")
async def create_private_memo(body: PrivateMemoCreate):
    memo_id = (body.id or "").strip() or str(uuid.uuid4())
    content = _clean_content(body.content)
    now = time.time()
    created_at = body.created_at if body.created_at is not None else now
    updated_at = body.updated_at if body.updated_at is not None else created_at
    completed_at = updated_at if body.status == "completed" else None
    source = (body.source or "app").strip() or "app"
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO private_memos
                (id, content, status, source, created_at, updated_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                status=excluded.status,
                source=excluded.source,
                updated_at=excluded.updated_at,
                completed_at=excluded.completed_at
            WHERE excluded.updated_at >= private_memos.updated_at
            """,
            (memo_id, content, body.status, source, created_at, updated_at, completed_at),
        )
        await db.commit()
        item = await _get_memo(db, memo_id)
    await _notify_changed()
    return item


@router.patch("/api/private-memos/{memo_id}")
async def update_private_memo(memo_id: str, body: PrivateMemoUpdate):
    async with get_db() as db:
        existing = await _get_memo(db, memo_id)
        if not existing:
            raise HTTPException(status_code=404, detail="备忘不存在")
        content = (
            _clean_content(body.content) if body.content is not None else existing["content"]
        )
        status = body.status or existing["status"]
        now = time.time()
        completed_at = now if status == "completed" else None
        await db.execute(
            "UPDATE private_memos SET content=?, status=?, updated_at=?, completed_at=? "
            "WHERE id=?",
            (content, status, now, completed_at, memo_id),
        )
        await db.commit()
        item = await _get_memo(db, memo_id)
    await _notify_changed()
    return item


@router.delete("/api/private-memos/{memo_id}")
async def delete_private_memo(memo_id: str):
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM private_memos WHERE id=?", (memo_id,))
        await db.commit()
        changed = cursor.rowcount > 0
    if changed:
        await _notify_changed()
    return {"ok": changed}
