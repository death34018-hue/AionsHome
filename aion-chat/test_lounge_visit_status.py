import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import database
from lounge_friends import LoungeFriendStore

from lounge_visit_status import (
    VisitStatusHandle,
    create_chatroom_status,
    create_private_status,
    downgrade_status,
    recover_stale_statuses,
    remove_status,
)


async def _database():
    db = await aiosqlite.connect(":memory:")
    await db.executescript(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            attachments TEXT DEFAULT ''
        );
        CREATE TABLE chatroom_messages (
            id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            attachments TEXT DEFAULT ''
        );
        """
    )
    return db


def test_private_status_is_small_ordered_system_message_without_model_context():
    async def scenario():
        db = await _database()
        try:
            handle, message = await create_private_status(
                db,
                "conv-1",
                "Connor",
                "YUI 的朋友",
                "reply-1",
            )
            row = await (
                await db.execute(
                    "SELECT role, content, attachments FROM messages WHERE id=?",
                    (handle.message_id,),
                )
            ).fetchone()
            attachments = json.loads(row[2])
            return handle, message, row, attachments
        finally:
            await db.close()

    handle, message, row, attachments = asyncio.run(scenario())

    assert handle.channel == "private"
    assert handle.scope_id == "conv-1"
    assert row[0] == "system"
    assert row[1] == "Connor 正在前往拜访 YUI 的朋友…"
    assert message["content"] == row[1]
    assert {"type": "system_notice_order", "after_msg_id": "reply-1"} in attachments
    assert any(item.get("type") == "lounge_visit_status" for item in attachments)
    assert all(item.get("type") != "system_model_context" for item in attachments)


def test_chatroom_status_redacts_secrets_and_downgrade_is_idempotent():
    async def scenario():
        db = await _database()
        try:
            handle, _message = await create_chatroom_status(
                db,
                "room-1",
                "Connor Authorization: Bearer secret",
                "https://friend.example/mcp",
            )
            first = await downgrade_status(db, handle)
            second = await downgrade_status(db, handle)
            row = await (
                await db.execute(
                    "SELECT sender, content, attachments FROM chatroom_messages WHERE id=?",
                    (handle.message_id,),
                )
            ).fetchone()
            return handle, first, second, row
        finally:
            await db.close()

    handle, first, second, row = asyncio.run(scenario())

    assert handle.channel == "chatroom"
    assert row[0] == "system"
    assert "secret" not in row[1]
    assert "https://" not in row[1]
    assert row[1].endswith("的这次拜访中断了，可在串门记录中查看。")
    assert first is not None
    assert second is None
    marker = next(
        item for item in json.loads(row[2]) if item.get("type") == "lounge_visit_status"
    )
    assert marker["state"] == "interrupted"


def test_remove_status_is_owned_and_idempotent():
    async def scenario():
        db = await _database()
        try:
            handle, _message = await create_private_status(
                db, "conv-1", "Connor", "Friend", "reply-1"
            )
            first = await remove_status(db, handle)
            second = await remove_status(db, handle)
            count = (
                await (
                    await db.execute("SELECT COUNT(*) FROM messages")
                ).fetchone()
            )[0]
            return first, second, count
        finally:
            await db.close()

    assert asyncio.run(scenario()) == (True, False, 0)


def test_restart_recovery_downgrades_or_deletes_active_status_once():
    async def scenario():
        db = await _database()
        try:
            stale, _ = await create_private_status(
                db, "conv-1", "Connor", "Stale friend", "reply-1"
            )
            reported, _ = await create_chatroom_status(
                db, "room-1", "Connor", "Reported friend"
            )
            report_attachment = json.dumps(
                [
                    {
                        "type": "lounge_visit_report",
                        "status": "completed",
                        "status_id": reported.status_id,
                    }
                ],
                ensure_ascii=False,
            )
            await db.execute(
                "INSERT INTO chatroom_messages VALUES (?,?,?,?,?,?)",
                ("report-1", "room-1", "connor", "home", 10.0, report_attachment),
            )
            await db.commit()

            first = await recover_stale_statuses(db)
            second = await recover_stale_statuses(db)
            private_row = await (
                await db.execute(
                    "SELECT content, attachments FROM messages WHERE id=?",
                    (stale.message_id,),
                )
            ).fetchone()
            reported_count = (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM chatroom_messages WHERE id=?",
                        (reported.message_id,),
                    )
                ).fetchone()
            )[0]
            return first, second, private_row, reported_count
        finally:
            await db.close()

    first, second, private_row, reported_count = asyncio.run(scenario())

    assert {(item.action, item.channel) for item in first} == {
        ("updated", "private"),
        ("deleted", "chatroom"),
    }
    assert second == []
    assert private_row[0] == "Connor 的这次拜访中断了，可在串门记录中查看。"
    assert reported_count == 0


def test_database_lounge_recovery_includes_temporary_status_messages():
    async def scenario():
        db = await _database()
        try:
            await db.executescript(
                """
                CREATE TABLE lounge_visits (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    friend_id TEXT NOT NULL,
                    trigger_source TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    status TEXT NOT NULL,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    final_reply TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL
                );
                CREATE TABLE lounge_visit_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    visit_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    content TEXT NOT NULL,
                    remote_message_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                """
            )
            handle, _ = await create_private_status(
                db, "conv-1", "Connor", "Friend", "reply-1"
            )
            mutations = await database._recover_lounge_runtime_state(db)
            row = await (
                await db.execute(
                    "SELECT content FROM messages WHERE id=?", (handle.message_id,)
                )
            ).fetchone()
            return mutations, row[0]
        finally:
            await db.close()

    mutations, content = asyncio.run(scenario())

    assert len(mutations) == 1
    assert mutations[0].action == "updated"
    assert content == "Connor 的这次拜访中断了，可在串门记录中查看。"


def test_private_chat_visit_creates_then_removes_status_after_report(tmp_path, monkeypatch):
    async def scenario():
        from routes import chat as chat_routes
        from routes import lounge_friends as lounge_routes
        import lounge_visit_reporting

        db_path = tmp_path / "chat.sqlite3"
        async with aiosqlite.connect(db_path) as db:
            await db.executescript(
                """
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, created_at REAL NOT NULL, attachments TEXT DEFAULT ''
                );
                CREATE TABLE chatroom_messages (
                    id TEXT PRIMARY KEY, room_id TEXT NOT NULL, sender TEXT NOT NULL,
                    content TEXT NOT NULL, created_at REAL NOT NULL, attachments TEXT DEFAULT ''
                );
                """
            )

        store = LoungeFriendStore(tmp_path / "friends.json")
        friend = store.create(
            actor_id="aion",
            display_name="Remote friend",
            lounge_url="https://friend.example/mcp",
            visitor_key="secret-key",
            relationship_note="friend",
            enabled=True,
            allow_autonomous=True,
            cooldown_hours=12,
            max_turns=4,
        )
        repository = object()

        @asynccontextmanager
        async def repository_provider():
            yield repository

        class Coordinator:
            def __init__(self, *args, **kwargs):
                pass

            async def run_visit(self, *args, **kwargs):
                return SimpleNamespace(
                    visit_id="visit-1",
                    status="completed",
                    turn_count=1,
                    reason="max_turns",
                )

        tasks = []
        broadcasts = AsyncMock()
        report = AsyncMock(return_value={"id": "report-1"})
        monkeypatch.setattr(lounge_routes, "friend_store", store)
        monkeypatch.setattr(lounge_routes, "lounge_repository_provider", repository_provider)
        monkeypatch.setattr(lounge_routes, "LoungeVisitCoordinator", Coordinator)
        monkeypatch.setattr(
            lounge_routes,
            "configured_actors",
            lambda: [{"id": "aion", "display_name": "Connor"}],
        )
        monkeypatch.setattr(chat_routes, "get_db", lambda: aiosqlite.connect(db_path))
        monkeypatch.setattr(chat_routes.manager, "broadcast", broadcasts)
        monkeypatch.setattr(lounge_visit_reporting, "publish_outbound_report", report)
        monkeypatch.setattr(chat_routes.asyncio, "create_task", lambda coro: tasks.append(coro))

        started = await chat_routes._start_private_lounge_visit(
            "aion",
            friend.id,
            "聊聊近况",
            conv_id="conv-1",
            ai_msg_id="reply-1",
        )
        async with aiosqlite.connect(db_path) as db:
            active = await (
                await db.execute(
                    "SELECT content, attachments FROM messages WHERE role='system'"
                )
            ).fetchall()
        await tasks[0]
        async with aiosqlite.connect(db_path) as db:
            remaining = (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM messages WHERE role='system'"
                    )
                ).fetchone()
            )[0]
        return started, active, remaining, report, broadcasts

    started, active, remaining, report, broadcasts = asyncio.run(scenario())

    assert started
    assert len(active) == 1
    assert active[0][0] == "Connor 正在前往拜访 Remote friend…"
    marker = next(
        item
        for item in json.loads(active[0][1])
        if item.get("type") == "lounge_visit_status"
    )
    assert report.await_args.kwargs["status_id"] == marker["status_id"]
    assert remaining == 0
    assert [call.args[0]["type"] for call in broadcasts.await_args_list] == [
        "msg_created",
        "msg_deleted",
    ]


def test_private_chat_visit_downgrades_status_when_report_cannot_be_saved(tmp_path, monkeypatch):
    async def scenario():
        from routes import chat as chat_routes
        from routes import lounge_friends as lounge_routes
        import lounge_visit_reporting

        db_path = tmp_path / "chat.sqlite3"
        async with aiosqlite.connect(db_path) as db:
            await db.executescript(
                """
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, created_at REAL NOT NULL, attachments TEXT DEFAULT ''
                );
                CREATE TABLE chatroom_messages (
                    id TEXT PRIMARY KEY, room_id TEXT NOT NULL, sender TEXT NOT NULL,
                    content TEXT NOT NULL, created_at REAL NOT NULL, attachments TEXT DEFAULT ''
                );
                """
            )
        store = LoungeFriendStore(tmp_path / "friends.json")
        friend = store.create(
            actor_id="aion", display_name="Remote friend",
            lounge_url="https://friend.example/mcp", visitor_key="secret-key",
            relationship_note="friend", enabled=True, allow_autonomous=True,
            cooldown_hours=12, max_turns=4,
        )

        @asynccontextmanager
        async def repository_provider():
            yield object()

        class Coordinator:
            def __init__(self, *args, **kwargs):
                pass

            async def run_visit(self, *args, **kwargs):
                return SimpleNamespace(
                    visit_id="visit-1", status="interrupted", turn_count=0,
                    reason="connection_failed",
                )

        tasks = []
        broadcasts = AsyncMock()
        monkeypatch.setattr(lounge_routes, "friend_store", store)
        monkeypatch.setattr(lounge_routes, "lounge_repository_provider", repository_provider)
        monkeypatch.setattr(lounge_routes, "LoungeVisitCoordinator", Coordinator)
        monkeypatch.setattr(
            lounge_routes, "configured_actors",
            lambda: [{"id": "aion", "display_name": "Connor"}],
        )
        monkeypatch.setattr(chat_routes, "get_db", lambda: aiosqlite.connect(db_path))
        monkeypatch.setattr(chat_routes.manager, "broadcast", broadcasts)
        monkeypatch.setattr(
            lounge_visit_reporting, "publish_outbound_report", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(chat_routes.asyncio, "create_task", lambda coro: tasks.append(coro))

        await chat_routes._start_private_lounge_visit(
            "aion", friend.id, "聊聊", conv_id="conv-1", ai_msg_id="reply-1"
        )
        await tasks[0]
        async with aiosqlite.connect(db_path) as db:
            row = await (
                await db.execute(
                    "SELECT content, attachments FROM messages WHERE role='system'"
                )
            ).fetchone()
        return row, broadcasts

    row, broadcasts = asyncio.run(scenario())

    assert row[0] == "Connor 的这次拜访中断了，可在串门记录中查看。"
    marker = next(
        item for item in json.loads(row[1]) if item.get("type") == "lounge_visit_status"
    )
    assert marker["state"] == "interrupted"
    assert [call.args[0]["type"] for call in broadcasts.await_args_list] == [
        "msg_created",
        "msg_updated",
    ]


def test_chatroom_visit_status_is_ordered_after_reply_and_removed_after_report(tmp_path, monkeypatch):
    async def scenario():
        from routes import chatroom as chatroom_routes
        from routes import lounge_friends as lounge_routes
        import lounge_visit_reporting
        import lounge_visit_status

        store = LoungeFriendStore(tmp_path / "friends.json")
        friend = store.create(
            actor_id="connor", display_name="Remote friend",
            lounge_url="https://friend.example/mcp", visitor_key="secret-key",
            relationship_note="friend", enabled=True, allow_autonomous=True,
            cooldown_hours=12, max_turns=4,
        )

        @asynccontextmanager
        async def repository_provider():
            yield object()

        @asynccontextmanager
        async def fake_db():
            yield object()

        class Coordinator:
            def __init__(self, *args, **kwargs):
                pass

            async def run_visit(self, *args, **kwargs):
                return SimpleNamespace(
                    visit_id="visit-1", status="completed", turn_count=2,
                    reason="max_turns",
                )

        handle = VisitStatusHandle("chatroom", "room-1", "status-msg", "status-1")
        status_message = {
            "id": "status-msg", "room_id": "room-1", "sender": "system",
            "content": "Connor 正在前往拜访 Remote friend…",
            "created_at": 1.0,
            "attachments": [
                {"type": "lounge_visit_status", "status_id": "status-1", "state": "active"},
                {"type": "system_notice_order", "after_msg_id": "reply-1"},
            ],
        }
        create = AsyncMock(return_value=(handle, status_message))
        remove = AsyncMock(return_value=True)
        report = AsyncMock(return_value={"id": "report-1"})
        broadcasts = AsyncMock()
        tasks = []
        queue = asyncio.Queue()

        monkeypatch.setattr(lounge_routes, "friend_store", store)
        monkeypatch.setattr(lounge_routes, "lounge_repository_provider", repository_provider)
        monkeypatch.setattr(lounge_routes, "LoungeVisitCoordinator", Coordinator)
        monkeypatch.setattr(
            lounge_routes, "configured_actors",
            lambda: [{"id": "connor", "display_name": "Connor"}],
        )
        monkeypatch.setattr(lounge_visit_status, "create_chatroom_status", create)
        monkeypatch.setattr(lounge_visit_status, "remove_status", remove)
        monkeypatch.setattr(lounge_visit_reporting, "publish_outbound_report", report)
        monkeypatch.setattr(chatroom_routes, "get_db", fake_db)
        monkeypatch.setattr(chatroom_routes, "broadcast_synced", broadcasts)
        monkeypatch.setattr(chatroom_routes.asyncio, "create_task", lambda coro: tasks.append(coro))

        started = await chatroom_routes._start_chatroom_lounge_visit(
            "connor", friend.id, "聊聊", room_id="room-1", msg_id="reply-1", queue=queue
        )
        queued = await asyncio.wait_for(queue.get(), 0.2)
        await tasks[0]
        return started, queued, create, remove, report, broadcasts

    started, queued, create, remove, report, broadcasts = asyncio.run(scenario())

    assert started
    assert queued == {"type": "system_msg", "message": queued["message"]}
    assert queued["message"]["attachments"][-1] == {
        "type": "system_notice_order", "after_msg_id": "reply-1"
    }
    assert create.await_args.args[-1] == "reply-1"
    assert report.await_args.kwargs["status_id"] == "status-1"
    remove.assert_awaited_once()
    assert [call.args[1]["type"] for call in broadcasts.await_args_list] == [
        "chatroom_msg_created",
        "chatroom_msg_deleted",
    ]
