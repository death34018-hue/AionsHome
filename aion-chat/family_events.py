"""Small, durable user milestones shown in the family timeline."""

import json
import time
import uuid
from pathlib import Path

import aiosqlite

from config import DB_PATH


SESSION_GAP_SECONDS = 30 * 60
ALLOWED_KINDS = {
    "english_card_learned",
    "seeky_feed",
    "seeky_clean",
}


async def _ensure_table(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS family_user_events (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            source_id TEXT NOT NULL UNIQUE,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_family_user_events_created "
        "ON family_user_events(created_at DESC)"
    )


async def record_user_event(
    kind: str,
    *,
    source_id: str = "",
    metadata: dict | None = None,
    created_at: float | None = None,
    db_path: str | Path | None = None,
) -> bool:
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"Unsupported family event kind: {kind}")
    event_id = f"family_{uuid.uuid4().hex}"
    stable_source_id = source_id.strip() or event_id
    timestamp = float(created_at if created_at is not None else time.time())
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        await _ensure_table(db)
        cursor = await db.execute(
            "INSERT OR IGNORE INTO family_user_events "
            "(id,kind,source_id,metadata,created_at) VALUES (?,?,?,?,?)",
            (
                event_id,
                kind,
                stable_source_id,
                json.dumps(metadata or {}, ensure_ascii=False),
                timestamp,
            ),
        )
        await db.commit()
        return cursor.rowcount > 0


async def list_grouped_user_events(
    *,
    since: float,
    limit: int,
    db_path: str | Path | None = None,
) -> list[dict]:
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_table(db)
        cursor = await db.execute(
            "SELECT id,kind,source_id,metadata,created_at "
            "FROM family_user_events WHERE created_at>=? "
            "ORDER BY kind,created_at,id",
            (float(since),),
        )
        rows = await cursor.fetchall()

    groups: list[dict] = []
    active_by_kind: dict[str, dict] = {}
    for row in rows:
        kind = str(row["kind"])
        timestamp = float(row["created_at"])
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except Exception:
            metadata = {}
        group = active_by_kind.get(kind)
        if group is None or timestamp - group["timestamp"] > SESSION_GAP_SECONDS:
            group = {
                "kind": kind,
                "count": 0,
                "started_at": timestamp,
                "timestamp": timestamp,
                "metadata": metadata,
                "source_ids": [],
            }
            groups.append(group)
            active_by_kind[kind] = group
        group["count"] += 1
        group["timestamp"] = timestamp
        group["metadata"] = metadata
        group["source_ids"].append(str(row["source_id"]))

    groups.sort(key=lambda item: item["timestamp"], reverse=True)
    return groups[: max(1, int(limit))]


def build_user_timeline_items(groups: list[dict], user_name: str) -> list[dict]:
    actor = (user_name or "用户").strip() or "用户"
    items = []
    for group in groups:
        kind = group.get("kind")
        count = max(1, int(group.get("count") or 1))
        metadata = group.get("metadata") or {}
        subject_name = str(metadata.get("subject_name") or "宠物").strip() or "宠物"
        if kind == "english_card_learned":
            title = f"{actor} 学习了英语，完成了 {count} 张卡片"
            detail = f"这次连续完成了 {count} 张英语学习卡片。"
        elif kind == "seeky_feed":
            title = f"{actor} 给 {subject_name} 喂了食"
            detail = f"认真照顾了 {subject_name}，让它吃得饱饱的。"
        elif kind == "seeky_clean":
            title = f"{actor} 给 {subject_name} 清理了水族箱"
            detail = f"把 {subject_name} 的水族箱收拾干净了。"
        else:
            continue
        timestamp = float(group.get("timestamp") or 0)
        items.append(
            {
                "timestamp": timestamp,
                "time": time.strftime("%H:%M", time.localtime(timestamp)),
                "kind": "user_event",
                "actor": actor,
                "title": title,
                "detail": detail,
                "source_id": (group.get("source_ids") or [""])[-1],
                "attachments": [],
            }
        )
    return items
