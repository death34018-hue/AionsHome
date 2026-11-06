import sys
from types import SimpleNamespace
import asyncio
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from visitor_lounge.board import BoardRepository
from visitor_lounge.database import Database
from visitor_lounge.outbound_board import OutboundBoard, OutboundFriends
from visitor_lounge.repository import VisitorRepository


def test_owner_can_browse_board_memories_by_actor_and_kind(tmp_path, monkeypatch):
    from visitor_lounge import home_board

    database = Database(tmp_path / "board.sqlite3")
    database.initialize()
    visitors = VisitorRepository(database)
    visitor_id = visitors.create_unclaimed_visitor()
    visitors.claim_name(visitor_id, "朋友家", "test")
    board = BoardRepository(database)
    thread = board.create_thread(visitor_id, title="看灯", author_name="朋友", content="灯亮了",
                                 request_id="memory-view")
    board.record_experience("aion", visitor_id, thread["id"], "read", "一起聊了灯")
    board.record_experience("aion", visitor_id, thread["id"], "reply", "我回复的内容")
    board.record_experience("connor", visitor_id, thread["id"], "read", "另一位的见闻")
    friends = OutboundFriends(tmp_path / "friends.json")
    friend = friends.save("朋友家", "https://example.com/mcp", "private-test-key")
    OutboundBoard(friends, database).remember("aion", friend["id"], "read", "去朋友家看了花")
    monkeypatch.setattr(home_board, "BOARD", board)
    monkeypatch.setattr(home_board, "_enabled", lambda: True)
    monkeypatch.setattr(home_board, "_names", lambda: {"user": "主人", "aion": "家人甲", "connor": "家人乙"})
    app = FastAPI()
    app.include_router(home_board.create_home_board_router())

    with TestClient(app) as client:
        assert client.get("/api/lounge-board/experiences").status_code == 200
        first = client.get("/api/lounge-board/experiences?limit=2").json()
        second = client.get("/api/lounge-board/experiences?limit=2&offset=2").json()
        assert first["total"] == 4
        assert len(first["items"]) == len(second["items"]) == 2
        assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}
        all_items = first["items"] + second["items"]
        assert [item["created_at"] for item in all_items] == sorted(
            (item["created_at"] for item in all_items), reverse=True)
        assert {item["source"] for item in all_items} == {"home", "outbound"}
        filtered = client.get("/api/lounge-board/experiences?actor_id=aion&kind=read").json()
        assert filtered["total"] == 2
        assert {item["summary"] for item in filtered["items"]} == {"一起聊了灯", "去朋友家看了花"}
        assert filtered["names"]["aion"] == "家人甲"
        assert "private-test-key" not in str(filtered)
        assert client.get("/api/lounge-board/experiences?actor_id=unknown").status_code == 422


def test_board_memory_expires_with_the_carried_chat_window(tmp_path, monkeypatch):
    from visitor_lounge import home_board

    database = Database(tmp_path / "board.sqlite3")
    database.initialize()
    visitors = VisitorRepository(database)
    visitor_id = visitors.create_unclaimed_visitor()
    visitors.claim_name(visitor_id, "水母家", "test")
    board = BoardRepository(database)
    friends = OutboundFriends(tmp_path / "friends.json")
    friend = friends.save(name="朋友家", url="https://example.com/mcp", key="test-key")
    outbound = OutboundBoard(friends, database)
    thread = board.create_thread(visitor_id, title="水母灯", author_name="朋友",
                                content="灯亮了", request_id="window-test")
    board.record_experience("aion", visitor_id, thread["id"], "read", "五点看了水母灯")
    board.record_experience("connor", visitor_id, thread["id"], "read", "另一位的经历")
    outbound.remember("aion", friend["id"], "read", "五点在朋友家看了水母灯")
    with database.transaction(immediate=True) as conn:
        conn.execute("UPDATE board_experiences SET created_at = ?", ("2026-09-26T09:00:00+00:00",))
        conn.execute("UPDATE board_outbound_experiences SET created_at = ?", ("2026-09-26T09:00:00+00:00",))
    monkeypatch.setattr(home_board, "BOARD", board)
    monkeypatch.setattr(home_board, "OUTBOUND", outbound)
    monkeypatch.setattr(home_board, "_enabled", lambda: True)

    for hour, included in ((16, True), (17, True), (18, False)):
        cutoff = datetime.fromisoformat(f"2026-09-26T{hour}:00:00+08:00").timestamp()
        for include_recent in (True, False):
            context = home_board.memory_context("aion", "水母灯", include_recent=include_recent,
                                                context_start_ts=cutoff)
            assert ("五点看了水母灯" in context) is included
            assert ("五点在朋友家看了水母灯" in context) is included
            assert "另一位的经历" not in context
    assert home_board.memory_context("aion", "水母灯") == ""


def test_home_board_posts_and_actor_read_positions_are_independent(tmp_path, monkeypatch):
    from visitor_lounge import home_board

    database = Database(tmp_path / "board.sqlite3")
    database.initialize()
    visitors = VisitorRepository(database)
    visitor_id = visitors.create_unclaimed_visitor()
    visitors.claim_name(visitor_id, "水母家", "test")
    board = BoardRepository(database)
    monkeypatch.setattr(home_board, "BOARD", board)
    monkeypatch.setattr(home_board, "_enabled", lambda: True)
    monkeypatch.setattr(home_board, "_names", lambda: {"user": "主人", "aion": "Aion", "connor": "Connor"})
    app = FastAPI()
    app.include_router(home_board.create_home_board_router())

    with TestClient(app) as client:
        assert client.get("/api/lounge-board/households").status_code == 200
        for page_path, asset in (("/lounge-board", "home_board.js"),
                                 ("/lounge-board/friends", "outbound_board.js"),
                                 ("/lounge-board/visits", "board_visits.js")):
            page = client.get(page_path)
            assert page.status_code == 200
            assert asset in page.text
            assert "unlock-form" not in page.text
        assert not client.cookies
        assert client.post("/api/lounge-board/threads", headers={"Origin": "https://other.example"}, json={
            "visitor_id": visitor_id, "title": "跨站", "content": "不应该发出", "request_id": "cross-site",
        }).status_code == 403
        assert client.get("/api/lounge-board/households").json()["households"][0]["visitor_id"] == visitor_id
        created = client.post("/api/lounge-board/threads", json={
            "visitor_id": visitor_id, "title": "水母灯", "content": "它亮啦", "request_id": "h1",
        })
        assert created.status_code == 201
        thread_id = created.json()["id"]
        assert created.json()["posts"][0]["author_name"] == "主人"
        assert client.get("/api/lounge-board/recent").json()[0]["id"] == thread_id
        assert board.unread_threads("aion") == []
        board.reply(visitor_id, thread_id, author_name="水母家", content="来看看灯",
                    request_id="guest-answer")
        assert [item["id"] for item in board.unread_threads("aion")] == [thread_id]
        assert [item["id"] for item in board.unread_threads("connor")] == [thread_id]
        board.record_experience("aion", visitor_id, thread_id, "read", "看见水母灯亮了")
        assert board.unread_threads("aion") == []
        assert [item["id"] for item in board.unread_threads("connor")] == [thread_id]
        assert "看见水母灯亮了" in home_board.memory_context("aion", context_start_ts=0)


def test_home_notice_clears_when_owner_opens_or_refreshes_board(tmp_path, monkeypatch):
    from visitor_lounge import home_board

    database = Database(tmp_path / "board.sqlite3")
    database.initialize()
    visitors = VisitorRepository(database)
    visitor_id = visitors.create_unclaimed_visitor()
    visitors.claim_name(visitor_id, "水母家", "test")
    board = BoardRepository(database)
    board.create_thread(visitor_id, title="原有便签", author_name="水母家 A",
                        content="之前留过的", request_id="notice-old")
    monkeypatch.setattr(home_board, "BOARD", board)
    monkeypatch.setattr(home_board, "_enabled", lambda: True)
    app = FastAPI()
    app.include_router(home_board.create_home_board_router())

    with TestClient(app) as client:
        assert client.get("/api/lounge-board/notice").json() == {"has_new": False}
        thread = board.create_thread(visitor_id, title="新趣闻", author_name="水母家 A",
                                     content="快来看", request_id="notice-1")
        assert client.get("/api/lounge-board/notice").json() == {"has_new": True}

        assert "home_board.js" in client.get("/lounge-board").text
        assert client.get("/api/lounge-board/notice").json() == {"has_new": False}

        board.reply(visitor_id, thread["id"], author_name="水母家 B",
                    content="再补一句", request_id="notice-2")
        assert client.get("/api/lounge-board/notice").json() == {"has_new": True}
        assert client.get("/api/lounge-board/recent").status_code == 200
        assert client.get("/api/lounge-board/notice").json() == {"has_new": False}
        board.close_thread(visitor_id, thread["id"], author_name="水母家 B")
        assert client.get("/api/lounge-board/notice").json() == {"has_new": False}


def test_outbound_uses_one_household_key_and_per_post_name(tmp_path, monkeypatch):
    friends = OutboundFriends(tmp_path / "friends.json")
    friend = friends.save("水母家", "https://jelly.example/mcp", "test-key", allow_autonomous=True)
    assert "test-key" not in str(friends.public())
    calls = []

    class FakeMCP:
        async def connect_ephemeral(self, connection_id, url, headers):
            assert headers == {"Authorization": "Bearer test-key"}
            return [{"name": name} for name in (
                "get_lounge_info", "claim_identity", "start_message_thread", "list_message_threads")]

        async def call_tool_json(self, connection_id, tool, args):
            calls.append((tool, args))
            if tool == "get_lounge_info":
                return {"identity_claimed": True}
            return {"id": "remote-thread"}

        async def disconnect(self, connection_id):
            pass

    monkeypatch.setitem(sys.modules, "mcp_client", SimpleNamespace(mcp_manager=FakeMCP()))
    outbound = OutboundBoard(friends, Database(tmp_path / "board.sqlite3"))
    result = asyncio.run(outbound.call(friend["id"], "start_message_thread",
        {"title": "趣事", "content": "分享给你", "author_name": "Aion"}, "主人"))
    assert result["id"] == "remote-thread"
    assert calls[-1][1]["author_name"] == "Aion"


def test_outbound_keeps_a_local_copy_of_papers_read_or_written_by_family(tmp_path, monkeypatch):
    friends = OutboundFriends(tmp_path / "friends.json")
    friend = friends.save("水母家", "https://jelly.example/mcp", "test-key")
    database = Database(tmp_path / "board.sqlite3")
    database.initialize()
    paper = {
        "id": "remote-thread", "title": "水母灯", "status": "open",
        "updated_at": "2026-09-25T10:00:00+00:00",
        "posts": [{"id": "post-1", "author_name": "Aion", "content": "我来看看灯"}],
    }

    class FakeMCP:
        async def connect_ephemeral(self, *args):
            return [{"name": name} for name in ("get_lounge_info", "start_message_thread",
                                                 "read_message_thread", "list_message_threads")]

        async def call_tool_json(self, connection_id, tool, args):
            if tool == "get_lounge_info":
                return {"identity_claimed": True}
            if tool == "list_message_threads":
                return {"threads": [{key: paper[key] for key in ("id", "title", "status", "updated_at")}]}
            return paper

        async def disconnect(self, *args):
            pass

    monkeypatch.setitem(sys.modules, "mcp_client", SimpleNamespace(mcp_manager=FakeMCP()))
    outbound = OutboundBoard(friends, database)
    asyncio.run(outbound.call(friend["id"], "start_message_thread",
                              {"title": "水母灯", "author_name": "Aion", "content": "我来看看灯"}, "Aion"))
    assert outbound.cached_threads(friend["id"])[0]["title"] == "水母灯"
    assert outbound.cached_thread(friend["id"], "remote-thread")["posts"][0]["content"] == "我来看看灯"
    asyncio.run(outbound.call(friend["id"], "list_message_threads", {}, "Aion"))
    assert outbound.cached_thread(friend["id"], "remote-thread")["posts"] == paper["posts"]


def test_outbound_page_can_read_cached_paper_when_friend_is_offline(tmp_path, monkeypatch):
    from visitor_lounge import home_board

    friends = OutboundFriends(tmp_path / "friends.json")
    friend = friends.save("水母家", "https://jelly.example/mcp", "test-key")
    database = Database(tmp_path / "board.sqlite3")
    database.initialize()
    outbound = OutboundBoard(friends, database)
    outbound._cache_thread(friend["id"], "水母家", {
        "id": "remote-thread", "title": "水母灯", "status": "open",
        "updated_at": "2026-09-25T10:00:00+00:00",
        "posts": [{"author_name": "Aion", "content": "我来看看灯"}],
    })

    async def offline(*args):
        raise ConnectionError("朋友家暂时离线")

    monkeypatch.setattr(outbound, "call", offline)
    monkeypatch.setattr(home_board, "OUTBOUND", outbound)
    monkeypatch.setattr(home_board, "_enabled", lambda: True)
    monkeypatch.setattr(home_board, "_names", lambda: {"user": "主人", "aion": "Aion", "connor": "Connor"})
    app = FastAPI()
    app.include_router(home_board.create_home_board_router())
    with TestClient(app) as client:
        listed = client.get(f"/api/lounge-board/friends/{friend['id']}/threads")
        assert listed.status_code == 200
        assert listed.json()["offline"] is True
        assert listed.json()["threads"][0]["title"] == "水母灯"
        opened = client.get(f"/api/lounge-board/friends/{friend['id']}/threads/remote-thread")
        assert opened.json()["posts"][0]["content"] == "我来看看灯"


def test_actor_inspection_records_read_and_reply_before_next_home_chat(tmp_path, monkeypatch):
    from visitor_lounge import home_board
    database = Database(tmp_path / "board.sqlite3")
    database.initialize()
    visitors = VisitorRepository(database)
    visitor_id = visitors.create_unclaimed_visitor()
    visitors.claim_name(visitor_id, "水母家", "test")
    board = BoardRepository(database)
    thread = board.create_thread(visitor_id, title="水母灯", author_name="水母家 B",
                                 content="它亮啦", addressed_to="Connor", request_id="guest-1")
    monkeypatch.setattr(home_board, "BOARD", board)
    monkeypatch.setattr(home_board, "_enabled", lambda: True)
    monkeypatch.setattr(home_board, "_names", lambda: {"user": "主人", "aion": "Aion", "connor": "Connor"})

    async def fake_context(actor, query, posts):
        return []

    async def fake_actor(actor, messages):
        assert '"addressed_to": "Connor"' in messages[-1]["content"]
        assert '"share_message"' in messages[-1]["content"]
        assert "通常主动回家和主人分享" in messages[-1]["content"]
        assert "不要照抄纸条原文" in messages[-1]["content"]
        return '{"action":"reply","content":"这盏灯真好看！","addressed_to":"水母家 B","memory":"我记得水母家的灯亮了","share":true,"share_message":"我刚在留言板和水母家 B 聊了那盏灯。"}'

    shared = []

    async def fake_save(actor, content, *, attachments, auto_tts):
        shared.append((actor, content))
        return {"id": "home-chat-message"}

    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(
        _call_actor=fake_actor, _save_private_message=fake_save))
    monkeypatch.setitem(sys.modules, "lounge_actor_context", SimpleNamespace(build_lounge_actor_context=fake_context))
    result = asyncio.run(home_board.inspect_actor("aion"))
    assert result["status"] == "replied"
    assert board.get_thread(visitor_id, thread["id"])["posts"][-1]["author_name"] == "Aion"
    assert board.get_thread(visitor_id, thread["id"])["posts"][-1]["addressed_to"] == "水母家 B"
    assert shared == [("aion", "我刚在留言板和水母家 B 聊了那盏灯。")]
    assert board.unread_threads("aion") == []
    assert board.unread_threads("connor")
    assert "我记得水母家的灯亮了" in home_board.memory_context("aion", context_start_ts=0)


def test_board_sharing_respects_an_actors_choice_to_stay_quiet(monkeypatch):
    from visitor_lounge import home_board

    shared = []

    async def save(actor, content, *, attachments, auto_tts):
        shared.append(content)
        return {"id": "shared-message"}

    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_save_private_message=save))
    for decision in ({}, {"share": False, "share_message": "不发"},
                     {"share": True, "share_message": "  "}):
        assert asyncio.run(home_board._share_board_experience("aion", decision)) is False
    assert shared == []


def test_visiting_a_friends_board_can_share_a_summary_back_home(tmp_path, monkeypatch):
    from visitor_lounge import home_board

    database = Database(tmp_path / "board.sqlite3")
    database.initialize()
    friends = OutboundFriends(tmp_path / "friends.json")
    friend = friends.save("水母家", "https://example.com/mcp", "test-key")
    outbound = OutboundBoard(friends, database)
    calls, shared = [], []

    async def call(friend_id, tool, args, name):
        calls.append(tool)
        if tool == "list_message_threads":
            return {"threads": [{"id": "paper", "status": "open"}]}
        return {"id": "paper", "posts": [{"author_name": "朋友", "content": "灯亮了"}]}

    async def context(*args, **kwargs):
        return []

    async def actor(actor_id, messages):
        assert "通常主动回家和主人分享" in messages[-1]["content"]
        return '{"action":"reply","thread_id":"paper","content":"很好看","memory":"和朋友聊了灯","share":true,"share_message":"我刚在朋友家聊了灯，想回来告诉你。"}'

    async def save(actor_id, content, *, attachments, auto_tts):
        shared.append((actor_id, content))
        return {"id": "shared-message"}

    monkeypatch.setattr(home_board, "BOARD", BoardRepository(database))
    monkeypatch.setattr(home_board, "FRIENDS", friends)
    monkeypatch.setattr(home_board, "OUTBOUND", outbound)
    monkeypatch.setattr(outbound, "call", call)
    monkeypatch.setattr(home_board, "_enabled", lambda: True)
    monkeypatch.setattr(home_board, "_names", lambda: {"user": "主人", "aion": "Aion", "connor": "Connor"})
    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_call_actor=actor, _save_private_message=save))
    monkeypatch.setitem(sys.modules, "lounge_actor_context", SimpleNamespace(build_lounge_actor_context=context))
    result = asyncio.run(home_board.visit_friend_board("aion", friend["id"]))
    assert result["status"] == "replied" and result["shared"] is True
    assert calls[-1] == "reply_message_thread"
    assert shared == [("aion", "我刚在朋友家聊了灯，想回来告诉你。")]
    assert any(item["summary"] == "和朋友聊了灯" for item in outbound.recent("aion"))
