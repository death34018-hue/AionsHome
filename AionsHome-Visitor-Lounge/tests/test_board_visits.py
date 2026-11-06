"""A visit must retain both sides of a paper, even when no home message is sent."""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from visitor_lounge.board import BoardRepository
from visitor_lounge.database import Database
from visitor_lounge.outbound_board import OutboundBoard, OutboundFriends
from visitor_lounge.repository import VisitorRepository


@pytest.fixture
def visit_home(tmp_path, monkeypatch):
    from visitor_lounge import home_board
    database = Database(tmp_path / "board.sqlite3")
    database.initialize()
    board = BoardRepository(database)
    visitors = VisitorRepository(database)
    visitor_id = visitors.create_unclaimed_visitor()
    visitors.claim_name(visitor_id, "朋友家", "test")
    paper = board.create_thread(visitor_id, title="晨间小事", author_name="朋友",
                                content="今天开花了", request_id="first")
    friends = OutboundFriends(tmp_path / "friends.json")
    friend = friends.save("朋友家", "https://example.com/mcp", "never-show-this-key")
    outbound = OutboundBoard(friends, database)
    monkeypatch.setattr(home_board, "BOARD", board)
    monkeypatch.setattr(home_board, "FRIENDS", friends)
    monkeypatch.setattr(home_board, "OUTBOUND", outbound)
    monkeypatch.setattr(home_board, "_enabled", lambda: True)
    monkeypatch.setattr(home_board, "_names", lambda: {"user": "主人", "aion": "家人甲", "connor": "家人乙"})

    async def context(*args, **kwargs):
        return []
    monkeypatch.setitem(sys.modules, "lounge_actor_context", SimpleNamespace(build_lounge_actor_context=context))
    return home_board, board, visitor_id, paper, friend, outbound


def test_quiet_home_close_keeps_reason_and_immutable_full_paper(visit_home, monkeypatch):
    home, board, visitor, paper, _, _ = visit_home
    async def actor(*args):
        return json.dumps({"action": "close", "reason": "已经聊完，替大家收好纸条", "share": False})
    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_call_actor=actor))
    result = asyncio.run(home.inspect_actor("connor"))
    assert "visit_id" in result
    from visitor_lounge.board_visits import VisitJournal
    journal = VisitJournal(board.database)
    saved = journal.get(result["visit_id"])
    assert saved["status"] == "closed"
    topic = saved["topics"][0]
    assert topic["status"] == "closed" and topic["reason"] == "已经聊完，替大家收好纸条"
    assert topic["snapshot"]["posts"][0]["content"] == "今天开花了"
    assert topic["snapshot"]["closed_by"] == "家人乙"
    with board.database.transaction() as conn:
        conn.execute("UPDATE board_posts SET content='后来编辑的文字' WHERE thread_id=?", (paper["id"],))
    assert journal.get(result["visit_id"])["topics"][0]["snapshot"]["posts"][0]["content"] == "今天开花了"


@pytest.mark.parametrize("outcome", ["full", "write_failed", "receipt_only"])
def test_outbound_paper_and_actual_reply_survive_offline_friend(visit_home, monkeypatch, outcome):
    home, board, _, _, friend, outbound = visit_home
    paper = {"id": "remote-paper", "title": "花开了", "status": "open", "posts": [
        {"id": "remote-1", "author_name": "朋友", "author_side": "home", "content": "完整的朋友来信"},
        {"id": "remote-2", "author_name": "主人", "author_side": "visitor", "content": "此前咱家的回应"}]}
    sent = False
    fail_write = outcome == "write_failed"
    async def call(friend_id, tool, args, name):
        nonlocal sent
        if tool == "list_message_threads":
            return {"threads": [paper]}
        if tool == "read_message_thread":
            if sent:
                raise ConnectionError("friend is now offline")
            return paper
        if fail_write:
            raise ConnectionError("offline")
        sent = True
        if outcome == "receipt_only":
            return {"id": "remote-paper", "status": "open"}
        return {**paper, "posts": [*paper["posts"], {"id": "remote-3", "author_name": name,
                "author_side": "visitor", "content": args["content"]}]}
    async def actor(*args):
        return json.dumps({"action": "reply", "thread_id": "remote-paper", "content": "完整的咱家回复",
                           "reason": "想接着聊这朵花", "share": False})
    monkeypatch.setattr(outbound, "call", call)
    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_call_actor=actor))
    if fail_write:
        with pytest.raises(ConnectionError):
            asyncio.run(home.visit_friend_board("aion", friend["id"]))
    else:
        result = asyncio.run(home.visit_friend_board("aion", friend["id"]))
        assert "visit_id" in result
    from visitor_lounge.board_visits import VisitJournal
    journal = VisitJournal(board.database)
    entry = journal.list(source="outbound")["items"][0]
    saved = journal.get(entry["id"])
    topic = saved["topics"][0]
    assert saved["status"] == ("failed" if fail_write else "replied")
    assert [p["content"] for p in topic["snapshot"]["posts"]] == (
        ["完整的朋友来信", "此前咱家的回应"] if fail_write else
        ["完整的朋友来信", "此前咱家的回应", "完整的咱家回复"])
    assert "never-show-this-key" not in json.dumps(saved)


def test_visit_archive_opens_without_code_and_includes_honest_old_records(visit_home, monkeypatch, tmp_path):
    home, board, visitor, paper, _, _ = visit_home
    board.record_experience("connor", visitor, paper["id"], "read", "看了花，没有回复")
    app = FastAPI()
    app.include_router(home.create_home_board_router())
    with TestClient(app) as client:
        assert client.get("/api/lounge-board/visits").status_code == 200
        response = client.get("/api/lounge-board/visits?actor_id=connor&source=home")
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1 and items[0]["legacy"] is True
        detail = client.get("/api/lounge-board/visits/" + items[0]["id"]).json()
        assert detail["topics"][0]["snapshot_kind"] == "current"
        assert detail["topics"][0]["snapshot"]["posts"][0]["content"] == "今天开花了"
        assert client.get("/api/lounge-board/visits?actor_id=aion").json()["total"] == 0


def test_a_friend_closing_during_read_is_not_reported_as_our_ais_action(visit_home, monkeypatch):
    home, board, visitor, paper, _, _ = visit_home
    async def actor(*args):
        board.close_thread(visitor, paper["id"], author_name="朋友")
        return '{"action":"close","reason":"聊完了","share":false}'
    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_call_actor=actor))
    result = asyncio.run(home.inspect_actor("connor"))
    assert result["status"] == "already_closed"
    from visitor_lounge.board_visits import VisitJournal
    topic = VisitJournal(board.database).get(result["visit_id"])["topics"][0]
    assert topic["status"] == "already_closed" and topic["snapshot"]["closed_by"] == "朋友"
    assert board.list_experiences(actor_id="connor", kind="close")["total"] == 0


def test_cancellation_after_success_receipt_still_keeps_confirmed_reply(visit_home, monkeypatch):
    home, board, _, _, friend, outbound = visit_home
    sent = False
    async def call(friend_id, tool, args, name):
        nonlocal sent
        if tool == "list_message_threads":
            return {"threads": [{"id": "remote", "status": "open"}]}
        if tool == "reply_message_thread":
            sent = True
            return {"id": "remote", "status": "open"}
        if sent:
            raise asyncio.CancelledError()
        return {"id": "remote", "title": "早安", "posts": [
            {"id": "before", "author_name": "朋友", "author_side": "home", "content": "朋友的早安"}]}
    async def actor(*args):
        return '{"action":"reply","thread_id":"remote","content":"咱家的早安","share":false}'
    monkeypatch.setattr(outbound, "call", call)
    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_call_actor=actor))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(home.visit_friend_board("aion", friend["id"]))
    from visitor_lounge.board_visits import VisitJournal
    journal = VisitJournal(board.database)
    visit = journal.get(journal.list()["items"][0]["id"])
    topic = visit["topics"][0]
    assert visit["status"] == "interrupted" and topic["status"] == "replied"
    assert topic["copy_complete"] is False
    assert [post["content"] for post in topic["snapshot"]["posts"]] == ["朋友的早安", "咱家的早安"]


def test_user_returning_during_outbound_read_preserves_paper_and_pauses(visit_home, monkeypatch):
    home, board, _, _, friend, outbound = visit_home
    from visitor_lounge import board_patrol
    from visitor_lounge.board_visits import VisitJournal
    latest, calls = [0.0], []
    async def latest_message():
        return latest[0]
    async def call(friend_id, tool, args, name):
        calls.append(tool)
        if tool == "list_message_threads":
            return {"threads": [{"id": "first", "status": "open"}, {"id": "second", "status": "open"}]}
        latest[0] = board_patrol.time.time()
        return {"id": "first", "title": "完整留存", "posts": [{"author_name": "朋友", "content": "朋友的整段留言"}]}
    monkeypatch.setattr(board_patrol, "latest_user_message_ts", latest_message)
    monkeypatch.setattr(outbound, "call", call)
    result = asyncio.run(home.visit_friend_board("aion", friend["id"]))
    assert result["status"] == "waiting_user"
    assert calls == ["list_message_threads", "read_message_thread"]
    visit = VisitJournal(board.database).get(result["visit_id"])
    assert visit["topics"][0]["status"] == "waiting_user"
    assert visit["topics"][0]["snapshot"]["posts"][0]["content"] == "朋友的整段留言"
