import asyncio
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from visitor_lounge.board import BoardRepository
from visitor_lounge.database import Database
from visitor_lounge.repository import VisitorRepository


@pytest.fixture
def board_home(tmp_path, monkeypatch):
    from visitor_lounge import home_board

    database = Database(tmp_path / "board.sqlite3")
    database.initialize()
    board = BoardRepository(database)
    visitors = VisitorRepository(database)
    visitor_ids = [visitors.create_unclaimed_visitor() for _ in range(2)]
    for visitor_id, name in zip(visitor_ids, ["朋友甲", "朋友乙"]):
        visitors.claim_name(visitor_id, name, "test")
    monkeypatch.setattr(home_board, "BOARD", board)
    monkeypatch.setattr(home_board, "_enabled", lambda: True)
    monkeypatch.setattr(home_board, "_names", lambda: {
        "user": "主人", "aion": "家人甲", "connor": "家人乙",
    })

    async def context(actor, query, posts):
        return []

    monkeypatch.setitem(sys.modules, "lounge_actor_context", SimpleNamespace(
        build_lounge_actor_context=context,
    ))
    return home_board, board, visitor_ids


def note(board, visitor_id, title, *, side="visitor"):
    return board.create_thread(visitor_id, title=title, author_name="发言人",
                               content=title, request_id=title, author_side=side)


def test_only_unhandled_visitor_posts_wake_each_actor_even_after_topic_closes(board_home):
    _, board, (visitor_id, _) = board_home
    note(board, visitor_id, "主人便签", side="home")
    thread = note(board, visitor_id, "访客便签")
    guest_id = thread["posts"][0]["id"]
    board.record_experience("aion", visitor_id, thread["id"], "read", "读过",
                            last_post_id=guest_id)
    board.reply(visitor_id, thread["id"], author_name="家人甲", content="回一句",
                request_id="home-reply", author_side="home")
    board.close_thread(visitor_id, thread["id"], author_name="家人甲")
    assert board.unread_threads("aion") == []
    assert [item["id"] for item in board.unread_threads("connor")] == [thread["id"]]


def test_actor_can_continue_to_another_household_and_preserves_messages_arriving_mid_read(board_home, monkeypatch):
    home, board, (visitor_a, visitor_b) = board_home
    first = note(board, visitor_a, "话题甲")
    second = note(board, visitor_b, "话题乙")

    async def actor(actor_id, messages):
        prompt = messages[-1]["content"]
        if "话题《话题甲》" in prompt:
            assert second["id"] in prompt
            board.reply(visitor_a, first["id"], author_name="访客", content="中途新增",
                        request_id="during-read")
            return '{"action":"reply","content":"回复甲","next_thread_id":"' + second["id"] + '"}'
        assert "话题《话题乙》" in prompt
        return '{"action":"reply","content":"回复乙","next_thread_id":""}'

    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_call_actor=actor))
    result = asyncio.run(home.inspect_actor("aion"))
    assert result["processed_count"] == 2
    assert board.get_thread(visitor_a, first["id"])["posts"][-1]["content"] == "回复甲"
    assert board.get_thread(visitor_b, second["id"])["posts"][-1]["content"] == "回复乙"
    assert [item["id"] for item in board.unread_threads("aion")] == [first["id"]]
    assert len(board.unread_threads("connor")) == 2


def test_read_without_reply_can_end_round_without_consuming_other_topics(board_home, monkeypatch):
    home, board, (visitor_id, _) = board_home
    first = note(board, visitor_id, "只看看")
    second = note(board, visitor_id, "留到下次")

    async def actor(actor_id, messages):
        return '{"action":"none","next_thread_id":""}'

    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_call_actor=actor))
    result = asyncio.run(home.inspect_actor("aion"))
    assert result["processed_count"] == 1
    assert len(board.get_thread(visitor_id, first["id"])["posts"]) == 1
    assert [item["id"] for item in board.unread_threads("aion")] == [second["id"]]


@pytest.mark.parametrize("response", [None, "not valid JSON"])
def test_failed_inspection_does_not_consume_visitor_message(board_home, monkeypatch, response):
    home, board, (visitor_id, _) = board_home
    thread = note(board, visitor_id, "不要漏掉")

    async def actor(actor_id, messages):
        if response is None:
            raise RuntimeError("模型暂不可用")
        return response

    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_call_actor=actor))
    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(home.inspect_actor("aion"))
    assert [item["id"] for item in board.unread_threads("aion")] == [thread["id"]]
    assert board.recent_experiences("aion") == []


def test_patrol_timers_persist_independently_and_empty_checks_do_not_call_models(board_home, monkeypatch):
    home, board, _ = board_home
    from visitor_lounge.board_patrol import BoardPatrolManager, PatrolStore

    store = PatrolStore(board.database)
    first = store.update("aion", enabled=True, min_interval_minutes=120,
                         max_interval_minutes=240, now=1_000)
    second = store.update("connor", enabled=True, min_interval_minutes=60,
                          max_interval_minutes=60, now=1_000)
    assert 8_200 <= first["next_check_at"] <= 15_400
    assert second["next_check_at"] == 4_600
    store.update("aion", enabled=False, now=2_000)
    assert PatrolStore(board.database).config("connor")["next_check_at"] == 4_600

    async def unexpected_call(*args):
        pytest.fail("空留言板不应调用模型")

    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_call_actor=unexpected_call))
    manager = BoardPatrolManager(store)
    result = asyncio.run(manager.run_due("connor", now=4_600))
    assert result["status"] == "nothing_new"
    config = store.config("connor")
    assert config["last_status"] == "nothing_new"
    assert config["next_check_at"] == 8_200
    assert asyncio.run(manager.run_due("aion", now=20_000))["status"] == "not_due"


def test_scheduled_patrol_goes_directly_to_board_without_general_autonomy(board_home, monkeypatch):
    home, board, (visitor_id, _) = board_home
    from visitor_lounge.board_patrol import BoardPatrolManager, PatrolStore

    thread = note(board, visitor_id, "专门来看我")
    store = PatrolStore(board.database)
    store.update("connor", enabled=True, min_interval_minutes=5, max_interval_minutes=5, now=1_000)

    async def actor(actor_id, messages):
        assert actor_id == "connor"
        return '{"action":"reply","content":"我来看啦","next_thread_id":""}'

    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_call_actor=actor))
    result = asyncio.run(BoardPatrolManager(store).run_due("connor", now=1_300))
    assert result["processed_count"] == 1
    assert board.get_thread(visitor_id, thread["id"])["posts"][-1]["content"] == "我来看啦"
    assert store.config("connor")["next_check_at"] == 1_600


def test_round_limit_leaves_fourth_topic_unread(board_home, monkeypatch):
    home, board, (visitor_id, _) = board_home
    threads = [note(board, visitor_id, f"便签{index}") for index in range(4)]
    responses = iter(threads[1:])

    async def actor(actor_id, messages):
        target = next(responses)
        return '{"action":"reply","content":"看到了","next_thread_id":"' + target["id"] + '"}'

    monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_call_actor=actor))
    result = asyncio.run(home.inspect_actor("aion"))
    assert result["processed_count"] == 3
    assert [item["id"] for item in board.unread_threads("aion")] == [threads[3]["id"]]


def test_duplicate_inspection_is_skipped_without_blocking_other_actor(board_home, monkeypatch):
    home, board, (visitor_id, _) = board_home
    note(board, visitor_id, "分别看")

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def actor(actor_id, messages):
            if actor_id == "aion":
                entered.set()
                await release.wait()
            return '{"action":"none","next_thread_id":""}'

        monkeypatch.setitem(sys.modules, "autonomy", SimpleNamespace(_call_actor=actor))
        first = asyncio.create_task(home.inspect_actor("aion"))
        await entered.wait()
        assert (await home.inspect_actor("aion"))["status"] == "busy"
        assert (await home.inspect_actor("connor"))["processed_count"] == 1
        release.set()
        assert (await first)["processed_count"] == 1

    asyncio.run(run())
    assert board.unread_threads("aion") == board.unread_threads("connor") == []


def test_patrol_settings_require_owner_login_and_validate_independent_intervals(board_home, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from visitor_lounge import board_owner_auth

    home, board, _ = board_home
    monkeypatch.setattr(board_owner_auth, "CODE_PATH", tmp_path / "owner-code.txt")
    app = FastAPI()
    app.include_router(home.create_home_board_router())
    with TestClient(app) as client:
        assert client.get("/api/lounge-board/patrol").status_code == 401
        client.post("/api/lounge-board/unlock", json={"code": board_owner_auth.owner_code()})
        before = client.get("/api/lounge-board/patrol").json()
        assert [role["name"] for role in before["roles"]] == ["家人甲", "家人乙"]
        assert client.put("/api/lounge-board/actors/aion/patrol", json={
            "enabled": True, "min_interval_minutes": 240, "max_interval_minutes": 120,
        }).status_code == 422
        saved = client.put("/api/lounge-board/actors/aion/patrol", json={
            "enabled": True, "min_interval_minutes": 120, "max_interval_minutes": 240,
        })
        assert saved.status_code == 200
        after = client.get("/api/lounge-board/patrol").json()
        assert after["roles"][0]["config"]["enabled"] is True
        assert after["roles"][1]["config"] == before["roles"][1]["config"]


def test_saving_settings_cannot_erase_deadline_scheduled_by_finishing_round(board_home, monkeypatch):
    _, board, _ = board_home
    from visitor_lounge.board_patrol import PatrolStore

    store = PatrolStore(board.database)
    store.update("aion", enabled=True, min_interval_minutes=5, max_interval_minutes=5, now=1_000)
    assert store.claim_due("aion", 1_300)
    transaction = board.database.transaction
    finish_pending = True

    @contextmanager
    def finish_at_transaction_boundary(*args, **kwargs):
        nonlocal finish_pending
        with transaction(*args, **kwargs) as conn:
            yield conn
        if finish_pending:
            finish_pending = False
            store.finish("aion", "read", now=1_500)

    monkeypatch.setattr(board.database, "transaction", finish_at_transaction_boundary)
    store.update("aion", enabled=True, now=1_400)
    assert store.config("aion")["next_check_at"] == 1_800
