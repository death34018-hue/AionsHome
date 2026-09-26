"""Family-level address book and MCP client for visiting a friend's board."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from visitor_lounge.board import BoardInvalidInput
from visitor_lounge.database import Database, utc_now


class OutboundFriends:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text("utf-8"))
        if not isinstance(data, list):
            raise ValueError("好友名册格式错误")
        return data

    def public(self) -> list[dict[str, object]]:
        return [{"id": item["id"], "name": item["name"], "url": item["url"],
                 "has_key": bool(item.get("key")),
                 "allow_autonomous": bool(item.get("allow_autonomous", False))} for item in self._read()]

    def get(self, friend_id: str) -> dict[str, str]:
        for item in self._read():
            if item["id"] == friend_id:
                return item
        raise KeyError("朋友不存在")

    def save(self, name: str, url: str, key: str, friend_id: str | None = None,
             allow_autonomous: bool = False) -> dict[str, object]:
        name, key = name.strip(), key.strip()
        if not name or len(name) > 80 or not key or len(key) > 4096:
            raise BoardInvalidInput("请填写朋友名字和有效的 Key")
        parsed = urlsplit(url.strip())
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise BoardInvalidInput("请填写 HTTPS 会客室地址")
        host = parsed.hostname.lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            raise BoardInvalidInput("请填写朋友公开的会客室地址")
        try:
            port = parsed.port
        except ValueError:
            raise BoardInvalidInput("MCP 地址端口无效") from None
        netloc = f"[{host}]" if ":" in host else host
        if port not in {None, 443}:
            netloc = f"{netloc}:{port}"
        normalized_url = urlunsplit(("https", netloc, "/mcp", "", ""))
        items = self._read()
        new = {"id": friend_id or str(uuid4()), "name": name, "url": normalized_url,
               "key": key, "allow_autonomous": bool(allow_autonomous)}
        if friend_id:
            if not any(item["id"] == friend_id for item in items):
                raise KeyError("朋友不存在")
            items = [new if item["id"] == friend_id else item for item in items]
        else:
            items.append(new)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)
        return {"id": new["id"], "name": name, "url": normalized_url,
                "has_key": True, "allow_autonomous": bool(allow_autonomous)}

    def delete(self, friend_id: str) -> None:
        items = [item for item in self._read() if item["id"] != friend_id]
        self.path.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")


class OutboundBoard:
    TOOL_NAMES = {"list_message_threads", "read_message_thread", "start_message_thread",
                  "reply_message_thread", "close_message_thread"}

    def __init__(self, friends: OutboundFriends, database: Database) -> None:
        self.friends = friends
        self.database = database

    def _cache_thread(self, friend_id: str, friend_name: str, thread: object) -> None:
        if not isinstance(thread, dict) or not thread.get("id") or not thread.get("title"):
            return
        full_copy = json.dumps(thread, ensure_ascii=False) if isinstance(thread.get("posts"), list) else None
        with self.database.transaction(immediate=True) as conn:
            conn.execute(
                """INSERT INTO board_outbound_threads
                   (friend_id, thread_id, friend_name, title, status, updated_at, thread_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(friend_id, thread_id) DO UPDATE SET
                   friend_name=excluded.friend_name, title=excluded.title,
                   status=excluded.status, updated_at=excluded.updated_at,
                   thread_json=COALESCE(excluded.thread_json, board_outbound_threads.thread_json)""",
                (friend_id, str(thread["id"]), friend_name, str(thread["title"]),
                 str(thread.get("status") or "open"),
                 str(thread.get("updated_at") or utc_now().isoformat()), full_copy),
            )

    def cached_threads(self, friend_id: str) -> list[dict[str, object]]:
        with self.database.connection() as conn:
            rows = conn.execute(
                """SELECT thread_id, friend_name, title, status, updated_at, thread_json IS NOT NULL
                   FROM board_outbound_threads WHERE friend_id = ?
                   ORDER BY updated_at DESC, rowid DESC""", (friend_id,)
            ).fetchall()
        return [dict(zip(("id", "friend_name", "title", "status", "updated_at", "has_copy"), row))
                for row in rows]

    def cached_thread(self, friend_id: str, thread_id: str) -> dict[str, object] | None:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT thread_json FROM board_outbound_threads WHERE friend_id = ? AND thread_id = ?",
                (friend_id, thread_id),
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    async def call(self, friend_id: str, tool: str, args: dict[str, object], identity_name: str) -> dict[str, object]:
        if tool not in self.TOOL_NAMES:
            raise BoardInvalidInput("不支持的留言板操作")
        friend = self.friends.get(friend_id)
        from mcp_client import mcp_manager
        connection_id = f"family-board:{uuid4()}"
        try:
            tools = await mcp_manager.connect_ephemeral(
                connection_id, friend["url"], {"Authorization": f"Bearer {friend['key']}"})
            available = {entry.get("name") for entry in tools}
            if tool not in available or "get_lounge_info" not in available:
                raise BoardInvalidInput("朋友家的会客室尚未开放留言板")
            info = await mcp_manager.call_tool_json(connection_id, "get_lounge_info", {})
            if info.get("identity_claimed") is not True:
                if "claim_identity" not in available:
                    raise BoardInvalidInput("朋友家的会客室需要先登记")
                claimed = await mcp_manager.call_tool_json(connection_id, "claim_identity",
                                                           {"name": identity_name, "consent": True})
                if claimed.get("status") not in {"claimed", "already_claimed"}:
                    raise BoardInvalidInput("朋友家的会客室未接受登记")
            result = await mcp_manager.call_tool_json(connection_id, tool, args)
            if result.get("isError"):
                raise BoardInvalidInput("朋友家的留言板操作失败")
            try:
                if tool == "list_message_threads":
                    for thread in result.get("threads") or []:
                        self._cache_thread(friend_id, friend["name"], thread)
                else:
                    self._cache_thread(friend_id, friend["name"], result)
            except (sqlite3.Error, OSError, ValueError) as error:
                logging.getLogger(__name__).warning("Could not cache outbound board paper: %s", error)
            return result
        finally:
            await mcp_manager.disconnect(connection_id)

    def remember(self, actor_id: str, friend_id: str, kind: str, summary: str) -> None:
        if actor_id not in {"aion", "connor"}:
            raise BoardInvalidInput("家庭成员无效")
        friend = self.friends.get(friend_id)
        with self.database.transaction(immediate=True) as conn:
            conn.execute(
                """INSERT INTO board_outbound_experiences
                   (id, actor_id, friend_id, friend_name, kind, summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid4()), actor_id, friend_id, friend["name"], kind, summary[:1000], utc_now().isoformat()),
            )

    def recent(self, actor_id: str, limit: int = 5) -> list[dict[str, str]]:
        if not self.database.path.exists():
            return []
        try:
            with self.database.connection() as conn:
                rows = conn.execute(
                    """SELECT friend_name, kind, summary, created_at FROM board_outbound_experiences
                       WHERE actor_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                    (actor_id, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(zip(("friend_name", "kind", "summary", "created_at"), row)) for row in rows]

    def search(self, actor_id: str, words: list[str], limit: int = 4) -> list[dict[str, str]]:
        words = [word.strip()[:40] for word in words if word.strip()][:6]
        if actor_id not in {"aion", "connor"} or not words or not self.database.path.exists():
            return []
        where = " OR ".join("(friend_name LIKE ? OR summary LIKE ?)" for _ in words)
        params = [f"%{word}%" for word in words for _ in range(2)]
        try:
            with self.database.connection() as conn:
                rows = conn.execute(
                    f"""SELECT friend_name, kind, summary, created_at
                        FROM board_outbound_experiences WHERE actor_id = ? AND ({where})
                        ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                    (actor_id, *params, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(zip(("friend_name", "kind", "summary", "created_at"), row)) for row in rows]
