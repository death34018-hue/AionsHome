import sys
from types import SimpleNamespace
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from visitor_lounge.board import BoardRepository
from visitor_lounge.database import Database
from visitor_lounge.outbound_board import OutboundBoard, OutboundFriends
from visitor_lounge.repository import VisitorRepository


def test_home_board_posts_and_actor_read_positions_are_independent(tmp_path, monkeypatch):
    from visitor_lounge import home_board
    from visitor_lounge import board_owner_auth

    database = Database(tmp_path / "board.sqlite3")
    database.initialize()
    visitors = VisitorRepository(database)
    visitor_id = visitors.create_unclaimed_visitor()
    visitors.claim_name(visitor_id, "水母家", "test")
    board = BoardRepository(database)
    monkeypatch.setattr(home_board, "BOARD", board)
    monkeypatch.setattr(home_board, "_enabled", lambda: True)
    monkeypatch.setattr(home_board, "_names", lambda: {"user": "主人", "aion": "Aion", "connor": "Connor"})
    monkeypatch.setattr(board_owner_auth, "CODE_PATH", tmp_path / "owner-code.txt")
    app = FastAPI()
    app.include_router(home_board.create_home_board_router())

    with TestClient(app) as client:
        assert client.get("/api/lounge-board/households").status_code == 401
        assert client.get("/api/lounge-board/recent").status_code == 401
        assert "解锁留言板" in client.get("/lounge-board").text
        assert client.post("/api/lounge-board/unlock", json={"code": "wrong"}).status_code == 401
        assert client.post("/api/lounge-board/unlock", json={"code": board_owner_auth.owner_code()}).status_code == 200
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
        assert "看见水母灯亮了" in home_board.memory_context("aion")


def test_home_notice_clears_when_owner_opens_or_refreshes_board(tmp_path, monkeypatch):
    from visitor_lounge import home_board
    from visitor_lounge import board_owner_auth

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
    monkeypatch.setattr(board_owner_auth, "CODE_PATH", tmp_path / "owner-code.txt")
    app = FastAPI()
    app.include_router(home_board.create_home_board_router())

    with TestClient(app) as client:
        assert client.get("/api/lounge-board/notice").json() == {"has_new": False}
        thread = board.create_thread(visitor_id, title="新趣闻", author_name="水母家 A",
                                     content="快来看", request_id="notice-1")
        assert client.get("/api/lounge-board/notice").json() == {"has_new": True}
        assert "解锁留言板" in client.get("/lounge-board").text
        assert client.get("/api/lounge-board/notice").json() == {"has_new": True}

        client.post("/api/lounge-board/unlock", json={"code": board_owner_auth.owner_code()})
        assert "家里的朋友留言板" in client.get("/lounge-board").text
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
    from visitor_lounge import home_board, board_owner_auth

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
    monkeypatch.setattr(board_owner_auth, "CODE_PATH", tmp_path / "owner-code.txt")
    app = FastAPI()
    app.include_router(home_board.create_home_board_router())
    with TestClient(app) as client:
        client.post("/api/lounge-board/unlock", json={"code": board_owner_auth.owner_code()})
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
    assert "我记得水母家的灯亮了" in home_board.memory_context("aion")
