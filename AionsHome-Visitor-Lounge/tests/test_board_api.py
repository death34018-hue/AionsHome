from datetime import datetime, timezone

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from visitor_lounge.container import Container
from visitor_lounge.repository import VisitorRepository
from visitor_lounge.security import KeyService, SessionService
from visitor_lounge.settings import Settings
from visitor_lounge.visitor_app import create_visitor_app


def test_board_page_and_routes_are_key_scoped_while_chat_is_closed(tmp_path, database):
    root = tmp_path / "lounge"
    (root / "config").mkdir(parents=True)
    (root / "config" / "persona.md").write_text("友好。", encoding="utf-8")
    settings = Settings(
        root=root, database_path=database.path,
        visitor_host="127.0.0.1", visitor_port=8001,
        admin_host="127.0.0.1", admin_port=8002,
        max_generations=1, max_generations_hard_limit=2, max_waiting=3,
        queue_timeout_seconds=120, generation_timeout_seconds=120,
        key_pepper=b"test-pepper", master_key=Fernet.generate_key(),
        session_secret=b"test-secret", codex_workdir=root / "workdir",
        chat_enabled=False, board_enabled=True,
    )
    database.initialize()
    visitors = VisitorRepository(database)
    first = visitors.create_unclaimed_visitor()
    second = visitors.create_unclaimed_visitor()
    visitors.claim_name(first, "水母家", "test")
    visitors.claim_name(second, "另一家", "test")
    sessions = SessionService(visitors, settings)
    first_cookie = sessions.issue(first, "first")
    second_cookie = sessions.issue(second, "second")
    container = Container(
        settings=settings, database=database, codex_adapter=object(),
        clock=lambda: datetime(2026, 9, 25, tzinfo=timezone.utc),
    )

    with TestClient(create_visitor_app(container)) as client:
        tools = {tool.name for tool in client.app.state.mcp_server._tool_manager.list_tools()}
        assert {"list_message_threads", "read_message_thread", "start_message_thread",
                "reply_message_thread", "close_message_thread"}.issubset(tools)
        assert "talk_to_host" not in tools
        first_headers = {"Cookie": f"visitor_session={first_cookie}"}
        second_headers = {"Cookie": f"visitor_session={second_cookie}"}
        assert "留言板" in client.get("/", headers=first_headers).text
        created = client.post(
            "/api/board/threads", headers=first_headers,
            json={"title": "水母灯", "author_name": "水母家 A", "content": "亮了！", "request_id": "r1"},
        )
        assert created.status_code == 201
        thread_id = created.json()["id"]
        assert client.get("/api/board/threads", headers=second_headers).json() == []
        assert client.get(f"/api/board/threads/{thread_id}", headers=second_headers).status_code == 404
        assert client.post(
            f"/api/board/threads/{thread_id}/posts", headers=second_headers,
            json={"author_name": "另一家", "content": "偷看", "request_id": "r2"},
        ).status_code == 404
        assert client.post(
            f"/api/board/threads/{thread_id}/close", headers=second_headers,
            json={"author_name": "另一家"},
        ).status_code == 404
        assert client.post(
            "/api/messages", headers=first_headers,
            json={"text": "实时聊天？", "request_id": "chat-1"},
        ).status_code == 403

        newcomer = visitors.create_unclaimed_visitor()
        key = KeyService(visitors, settings).create(newcomer).value
        assert client.post("/api/login", json={"key": key}).json()["next"] == "claim"
        assert client.post("/api/claim", json={"name": "水母家新人", "consent": True}).json()["next"] == "board"
        with database.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages WHERE visitor_id = ?", (newcomer,)).fetchone()[0] == 0
