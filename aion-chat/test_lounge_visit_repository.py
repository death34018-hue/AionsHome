import asyncio
import sys
from pathlib import Path

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database
from lounge_visit_repository import (
    LoungeVisitRepository,
    ensure_lounge_visit_tables,
    sanitize_error,
)


def test_visit_repository_records_text_without_credentials():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await ensure_lounge_visit_tables(db)
            repo = LoungeVisitRepository(db)
            visit_id = await repo.start("actor-1", "friend-1", "manual", "Catch up")
            await repo.append_message(
                visit_id, "outbound", "Hello", remote_message_id="message-1"
            )
            await repo.finish(visit_id, "completed", turn_count=1, error="")

            visit = await repo.get("actor-1", visit_id)

            assert visit["actor_id"] == "actor-1"
            assert visit["status"] == "completed"
            assert len(visit["messages"]) == 1
            assert visit["messages"][0]["direction"] == "outbound"
            assert visit["messages"][0]["content"] == "Hello"
            assert visit["messages"][0]["remote_message_id"] == "message-1"
            assert "Authorization" not in repr(visit)
            assert "Bearer" not in repr(visit)
            recent = await repo.recent("actor-1", "friend-1")
            assert len(recent) == 1
            assert recent[0]["id"] == visit_id
            assert recent[0]["topic"] == "Catch up"
            assert "messages" not in recent[0]

    asyncio.run(scenario())


def test_visit_repository_updates_running_turn_progress():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await ensure_lounge_visit_tables(db)
            repo = LoungeVisitRepository(db)
            visit_id = await repo.start("actor-1", "friend-1", "manual", "Catch up")

            await repo.update_progress(visit_id, 2)

            recent = await repo.recent("actor-1")
            assert recent[0]["status"] == "running"
            assert recent[0]["turn_count"] == 2

    asyncio.run(scenario())


def test_visit_repository_redacts_and_bounds_exception_errors():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await ensure_lounge_visit_tables(db)
            repo = LoungeVisitRepository(db)
            visit_id = await repo.start("actor-1", "friend-1", "manual", "Catch up")
            await repo.finish(
                visit_id,
                "interrupted",
                turn_count=0,
                error=RuntimeError(
                    "Authorization: Bearer private-visitor-key "
                    "access_token=oauth-secret "
                    + "x" * 400
                ),
            )

            error = (await repo.get("actor-1", visit_id))["error"]

            assert error.startswith("RuntimeError: ")
            assert len(error) <= 300
            assert "Authorization" not in error
            assert "Bearer" not in error
            assert "private-visitor-key" not in error
            assert "oauth-secret" not in error
            assert "RuntimeError(" not in error

    asyncio.run(scenario())


def test_visit_repository_redacts_credential_shaped_text():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await ensure_lounge_visit_tables(db)
            repo = LoungeVisitRepository(db)
            visit_id = await repo.start(
                "actor-1",
                "friend-1",
                "manual",
                "Authorization: Bearer private-visitor-key",
            )
            await repo.append_message(
                visit_id,
                "inbound",
                "access_token=oauth-secret",
                remote_message_id="token=remote-secret",
            )

            payload = repr(await repo.get("actor-1", visit_id))

            assert "Authorization" not in payload
            assert "Bearer" not in payload
            assert "private-visitor-key" not in payload
            assert "oauth-secret" not in payload
            assert "remote-secret" not in payload

    asyncio.run(scenario())


def test_visit_repository_redacts_quoted_non_bearer_authorization_values():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await ensure_lounge_visit_tables(db)
            repo = LoungeVisitRepository(db)
            visit_id = await repo.start("actor-1", "friend-1", "manual", "Catch up")
            await repo.append_message(
                visit_id,
                "inbound",
                '{"Authorization": "Basic persisted-secret"}',
            )
            await repo.finish(
                visit_id,
                "interrupted",
                turn_count=0,
                error=RuntimeError('{"Authorization": "Token error-secret"}'),
            )

            rows = await (
                await db.execute(
                    "SELECT content FROM lounge_visit_messages WHERE visit_id=?",
                    (visit_id,),
                )
            ).fetchall()
            error = (
                await (
                    await db.execute("SELECT error FROM lounge_visits WHERE id=?", (visit_id,))
                ).fetchone()
            )[0]
            payload = repr((rows, error))

            assert "Authorization" not in payload
            assert "Basic" not in payload
            assert "Token" not in payload
            assert "persisted-secret" not in payload
            assert "error-secret" not in payload

    asyncio.run(scenario())


def test_visit_repository_hides_detail_and_messages_from_other_actors():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await ensure_lounge_visit_tables(db)
            repo = LoungeVisitRepository(db)
            visit_id = await repo.start("actor-1", "friend-1", "manual", "Private topic")
            await repo.append_message(visit_id, "outbound", "Private message")

            assert await repo.get("actor-2", visit_id) is None
            owned = await repo.get("actor-1", visit_id)
            assert owned["topic"] == "Private topic"
            assert owned["messages"][0]["content"] == "Private message"

    asyncio.run(scenario())


def test_visit_repository_deletes_owned_finished_visit_and_its_messages():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await ensure_lounge_visit_tables(db)
            repo = LoungeVisitRepository(db)
            visit_id = await repo.start("actor-1", "friend-1", "manual", "Old test")
            await repo.append_message(visit_id, "outbound", "Hello")
            await repo.finish(visit_id, "completed", 1)

            assert await repo.delete("actor-2", visit_id) is False
            assert await repo.delete("actor-1", visit_id) is True
            assert await repo.get("actor-1", visit_id) is None
            count = (await (await db.execute(
                "SELECT COUNT(*) FROM lounge_visit_messages WHERE visit_id=?",
                (visit_id,),
            )).fetchone())[0]
            assert count == 0

    asyncio.run(scenario())


def test_visit_repository_does_not_delete_running_visit():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await ensure_lounge_visit_tables(db)
            repo = LoungeVisitRepository(db)
            visit_id = await repo.start("actor-1", "friend-1", "manual", "Active")

            assert await repo.delete("actor-1", visit_id) is False
            assert await repo.get("actor-1", visit_id) is not None

    asyncio.run(scenario())


def test_finish_running_is_actor_scoped_and_idempotent():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await ensure_lounge_visit_tables(db)
            repo = LoungeVisitRepository(db, clock=lambda: 42.0)
            visit_id = await repo.start("actor-1", "friend-1", "manual", "Active")

            assert await repo.finish_running("actor-2", visit_id, "user_cancelled") is False
            assert await repo.finish_running("actor-1", visit_id, "user_cancelled") is True
            assert await repo.finish_running("actor-1", visit_id, "user_cancelled") is False

            visit = await repo.get("actor-1", visit_id)
            assert visit["status"] == "interrupted"
            assert visit["error"] == "Error: user_cancelled"
            assert visit["finished_at"] == 42.0

    asyncio.run(scenario())


def test_interrupt_stale_running_leaves_terminal_visits_unchanged():
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            await ensure_lounge_visit_tables(db)
            repo = LoungeVisitRepository(db, clock=lambda: 50.0)
            stale_one = await repo.start("actor-1", "friend-1", "chat", "One")
            stale_two = await repo.start("actor-2", "friend-2", "manual", "Two")
            completed = await repo.start("actor-1", "friend-3", "manual", "Done")
            await repo.finish(completed, "completed", 1)

            assert await repo.interrupt_stale_running("restart_recovery") == 2
            assert (await repo.get("actor-1", stale_one))["status"] == "interrupted"
            assert (await repo.get("actor-2", stale_two))["status"] == "interrupted"
            assert (await repo.get("actor-1", completed))["status"] == "completed"

    asyncio.run(scenario())


def test_sanitize_error_bounds_an_unusually_long_error_category():
    long_error_type = type("E" * 400, (Exception,), {})

    error = sanitize_error(long_error_type("details"))

    assert len(error) <= 300


def test_init_db_creates_lounge_visit_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "aion-chat.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", db_path)

    asyncio.run(database.init_db())

    async def scenario():
        async with aiosqlite.connect(db_path) as db:
            tables = {
                row[0]
                for row in await (
                    await db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name IN ('lounge_visits', 'lounge_visit_messages')"
                    )
                ).fetchall()
            }
            assert tables == {"lounge_visits", "lounge_visit_messages"}

    asyncio.run(scenario())


def test_init_db_interrupts_orphaned_running_lounge_visits(tmp_path, monkeypatch):
    db_path = tmp_path / "aion-chat.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    asyncio.run(database.init_db())

    async def seed_running():
        async with aiosqlite.connect(db_path) as db:
            repo = LoungeVisitRepository(db)
            return await repo.start("actor-1", "friend-1", "chat", "Orphan")

    visit_id = asyncio.run(seed_running())
    asyncio.run(database.init_db())

    async def verify():
        async with aiosqlite.connect(db_path) as db:
            visit = await LoungeVisitRepository(db).get("actor-1", visit_id)
            assert visit["status"] == "interrupted"
            assert visit["error"] == "Error: service_restarted"

    asyncio.run(verify())
