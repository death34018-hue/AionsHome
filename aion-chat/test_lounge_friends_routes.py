from __future__ import annotations

import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lounge_friends import LoungeFriendStore
from lounge_visit import LoungeVisitResult


ACTORS = [
    {"id": "aion", "display_name": "Configured Primary"},
    {"id": "connor", "display_name": "Configured Companion"},
]


def friend_body(actor_id: str = "aion", visitor_key: str = "private-key") -> dict:
    return {
        "actor_id": actor_id,
        "display_name": "Remote Friend",
        "lounge_url": "https://friend.example/mcp",
        "visitor_key": visitor_key,
        "relationship_note": "",
        "enabled": True,
        "allow_autonomous": True,
        "cooldown_hours": 12,
        "max_turns": 4,
    }


class FakeMCPManager:
    def __init__(self) -> None:
        self.connect_calls: list[tuple[str, str, dict[str, str]]] = []
        self.tool_calls: list[tuple[str, str, dict]] = []
        self.disconnect_calls: list[str] = []

    async def connect_ephemeral(
        self, connection_id: str, url: str, headers: dict[str, str]
    ) -> list[dict]:
        self.connect_calls.append((connection_id, url, headers))
        return [{"name": "get_lounge_info"}]

    async def call_tool_json(
        self, connection_id: str, tool_name: str, arguments: dict
    ) -> dict:
        self.tool_calls.append((connection_id, tool_name, arguments))
        return {
            "status": "ok",
            "host_name": "Configured Host",
            "lounge_state": "open",
            "identity_claimed": True,
            "max_input_chars": 500,
            "visitor_name": "Must Not Be Returned",
            "recording_disclosure": "Must Not Be Returned",
        }

    async def disconnect(self, connection_id: str) -> None:
        self.disconnect_calls.append(connection_id)


class FakeRepository:
    def __init__(self) -> None:
        self.recent_calls: list[tuple[str, str | None, int]] = []
        self.visits = [
            {
                "id": "visit-primary",
                "actor_id": "aion",
                "friend_id": "friend-primary",
                "trigger_source": "manual",
                "topic": "primary-only-metadata",
                "status": "completed",
                "turn_count": 1,
                "error": "",
                "started_at": 1.0,
                "finished_at": 2.0,
                "messages": [
                    {
                        "id": "message-primary",
                        "direction": "outbound",
                        "content": "primary-only-message",
                        "remote_message_id": "",
                        "created_at": 1.0,
                    }
                ],
            }
        ]

    async def recent(
        self, actor_id: str, friend_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        self.recent_calls.append((actor_id, friend_id, limit))
        return [
            {key: value for key, value in visit.items() if key != "messages"}
            for visit in self.visits
            if visit["actor_id"] == actor_id
            and (friend_id is None or visit["friend_id"] == friend_id)
        ][:limit]

    async def get(self, actor_id: str, visit_id: str) -> dict | None:
        return next(
            (
                visit
                for visit in self.visits
                if visit["actor_id"] == actor_id and visit["id"] == visit_id
            ),
            None,
        )

    async def delete(self, actor_id: str, visit_id: str) -> bool:
        visit = await self.get(actor_id, visit_id)
        if visit is None or visit["status"] == "running":
            return False
        self.visits.remove(visit)
        return True


class FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run_visit(
        self,
        actor_id: str,
        friend_id: str,
        trigger_source: str,
        topic: str,
        compose_next,
    ) -> LoungeVisitResult:
        self.calls.append(
            {
                "actor_id": actor_id,
                "friend_id": friend_id,
                "trigger_source": trigger_source,
                "topic": topic,
                "compose_next": compose_next,
            }
        )
        return LoungeVisitResult(
            visit_id="visit-manual",
            status="completed",
            turn_count=1,
            final_reply="Welcome",
            reason="action_end",
        )


@pytest.fixture
def route_env(tmp_path):
    store = LoungeFriendStore(tmp_path / "lounge_friends.json", clock=lambda: 10.0)
    manager = FakeMCPManager()
    repository = FakeRepository()
    coordinator = FakeCoordinator()
    coordinator.report_calls = []
    coordinator.active_manual_actors = set()

    async def report_publisher(actor_id, partner_name, result, report_repository):
        coordinator.report_calls.append(
            (actor_id, partner_name, result.visit_id, report_repository)
        )
        return {"id": "report-message"}

    @asynccontextmanager
    async def repository_provider():
        yield repository

    app = FastAPI()
    try:
        from routes import lounge_friends as route_module
    except ImportError:
        route_module = None
    if route_module is not None:
        app.include_router(
            route_module.create_router(
                friend_store=store,
                mcp=manager,
                repository_provider=repository_provider,
                coordinator_factory=lambda _repository: coordinator,
                actor_provider=lambda: list(ACTORS),
                compose_next=lambda *_args: None,
                report_publisher=report_publisher,
                active_manual_actors=coordinator.active_manual_actors,
            )
        )
    client = TestClient(app)
    try:
        yield client, store, manager, repository, coordinator
    finally:
        client.close()


def test_friend_api_never_returns_visitor_key(route_env):
    client, _store, _manager, _repository, _coordinator = route_env
    created_response = client.post("/api/lounge-friends", json=friend_body())
    created = created_response.json()

    assert created_response.status_code == 200
    assert "private-key" not in repr(created)
    assert created["has_key"] is True
    assert created["visitor_key_masked"] != "private-key"

    listed = client.get("/api/lounge-friends").json()
    assert listed["actors"] == ACTORS
    assert "private-key" not in repr(listed)


def test_empty_key_update_keeps_the_existing_key(route_env):
    client, store, _manager, _repository, _coordinator = route_env
    created = client.post("/api/lounge-friends", json=friend_body()).json()

    response = client.put(
        f"/api/lounge-friends/{created['id']}",
        json={
            "actor_id": "aion",
            "display_name": "Renamed Friend",
            "visitor_key": "",
            "relationship_note": "Known for a long time",
        },
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "Renamed Friend"
    assert store.get_owned("aion", created["id"]).visitor_key == "private-key"
    assert "private-key" not in repr(response.json())


def test_empty_key_update_rejects_existing_key_in_editable_text(route_env):
    client, store, _manager, _repository, _coordinator = route_env
    created = client.post("/api/lounge-friends", json=friend_body()).json()

    response = client.put(
        f"/api/lounge-friends/{created['id']}",
        json={
            "actor_id": "aion",
            "visitor_key": "",
            "relationship_note": "ordinary private-key text",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid lounge friend data"}
    assert store.get_owned("aion", created["id"]).relationship_note == "\u200b"
    assert "private-key" not in repr(response.json())


def test_friend_mutations_and_actions_require_actor_ownership(route_env):
    client, _store, _manager, _repository, _coordinator = route_env
    friend_id = client.post("/api/lounge-friends", json=friend_body()).json()["id"]

    responses = [
        client.put(
            f"/api/lounge-friends/{friend_id}",
            json={"actor_id": "connor", "display_name": "Not Owned"},
        ),
        client.delete(
            f"/api/lounge-friends/{friend_id}", params={"actor_id": "connor"}
        ),
        client.post(
            f"/api/lounge-friends/{friend_id}/test",
            json={"actor_id": "connor"},
        ),
        client.post(
            f"/api/lounge-friends/{friend_id}/visit",
            json={"actor_id": "connor", "topic": "Hello"},
        ),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404, 404]


def test_connection_test_calls_only_lounge_info_and_disconnects(route_env):
    client, _store, manager, _repository, _coordinator = route_env
    friend_id = client.post("/api/lounge-friends", json=friend_body()).json()["id"]

    response = client.post(
        f"/api/lounge-friends/{friend_id}/test", json={"actor_id": "aion"}
    )

    assert response.status_code == 200
    assert set(response.json()) == {
        "status",
        "host_name",
        "lounge_state",
        "identity_claimed",
        "max_input_chars",
    }
    assert [call[1:] for call in manager.tool_calls] == [("get_lounge_info", {})]
    assert manager.disconnect_calls == [manager.connect_calls[0][0]]
    assert "private-key" not in repr(response.json())


def test_manual_visit_uses_manual_trigger_source(route_env):
    client, _store, _manager, _repository, coordinator = route_env
    friend_id = client.post("/api/lounge-friends", json=friend_body()).json()["id"]

    response = client.post(
        f"/api/lounge-friends/{friend_id}/visit",
        json={"actor_id": "aion", "topic": "Say hello"},
    )

    assert response.status_code == 200
    assert coordinator.calls[0]["trigger_source"] == "manual"
    assert coordinator.calls[0]["actor_id"] == "aion"
    assert coordinator.calls[0]["friend_id"] == friend_id
    assert coordinator.report_calls == [
        ("aion", "Remote Friend", "visit-manual", _repository)
    ]


def test_visit_history_checks_friend_ownership_before_listing(route_env):
    client, _store, _manager, repository, _coordinator = route_env
    friend_id = client.post("/api/lounge-friends", json=friend_body()).json()["id"]

    owned = client.get(
        "/api/lounge-visits",
        params={"actor_id": "aion", "friend_id": friend_id, "limit": 9},
    )
    mismatched = client.get(
        "/api/lounge-visits",
        params={"actor_id": "connor", "friend_id": friend_id},
    )

    assert owned.status_code == 200
    assert repository.recent_calls == [("aion", friend_id, 9)]
    assert mismatched.status_code == 404


def test_visit_detail_requires_actor_ownership_without_metadata_leak(route_env):
    client, _store, _manager, _repository, _coordinator = route_env

    response = client.get(
        "/api/lounge-visits/visit-primary", params={"actor_id": "connor"}
    )

    assert response.status_code == 404
    assert "primary-only-metadata" not in repr(response.json())
    assert "primary-only-message" not in repr(response.json())


def test_visit_history_delete_is_actor_scoped_and_rejects_running(route_env):
    client, _store, _manager, repository, _coordinator = route_env

    mismatched = client.delete(
        "/api/lounge-visits/visit-primary", params={"actor_id": "connor"}
    )
    repository.visits[0]["status"] = "running"
    running = client.delete(
        "/api/lounge-visits/visit-primary", params={"actor_id": "aion"}
    )
    repository.visits[0]["status"] = "completed"
    deleted = client.delete(
        "/api/lounge-visits/visit-primary", params={"actor_id": "aion"}
    )

    assert mismatched.status_code == 404
    assert running.status_code == 409
    assert deleted.status_code == 200
    assert repository.visits == []


def test_manual_visit_rejects_duplicate_request_for_same_actor(route_env):
    client, _store, _manager, _repository, coordinator = route_env
    friend_id = client.post("/api/lounge-friends", json=friend_body()).json()["id"]
    coordinator.active_manual_actors.add("aion")

    response = client.post(
        f"/api/lounge-friends/{friend_id}/visit",
        json={"actor_id": "aion", "topic": "Say hello again"},
    )

    assert response.status_code == 409
    assert coordinator.calls == []


def test_visit_detail_survives_friend_deletion_but_remains_actor_scoped(route_env):
    client, _store, _manager, repository, _coordinator = route_env
    friend_id = client.post("/api/lounge-friends", json=friend_body()).json()["id"]
    repository.visits[0]["friend_id"] = friend_id
    deleted = client.delete(
        f"/api/lounge-friends/{friend_id}", params={"actor_id": "aion"}
    )

    owner = client.get(
        "/api/lounge-visits/visit-primary", params={"actor_id": "aion"}
    )
    other_actor = client.get(
        "/api/lounge-visits/visit-primary", params={"actor_id": "connor"}
    )

    assert deleted.status_code == 200
    assert owner.status_code == 200
    assert owner.json()["topic"] == "primary-only-metadata"
    assert other_actor.status_code == 404
    assert "primary-only-message" not in repr(other_actor.json())


def test_invalid_friend_data_returns_generic_error_without_key(route_env):
    client, _store, _manager, _repository, _coordinator = route_env
    assert client.post("/api/lounge-friends", json=friend_body()).status_code == 200

    duplicate = friend_body(visitor_key="another-private-key")
    response = client.post("/api/lounge-friends", json=duplicate)

    assert response.status_code == 422
    assert "another-private-key" not in repr(response.json())


def test_main_registers_lounge_friend_api_and_page():
    sys.modules.setdefault("akshare", types.ModuleType("akshare"))
    calendar_module = types.ModuleType("chinese_calendar")
    calendar_module.is_workday = lambda _day: True
    sys.modules.setdefault("chinese_calendar", calendar_module)
    import main

    paths = {getattr(route, "path", "") for route in main.app.routes}
    assert "/api/lounge-friends" in paths
    assert "/lounge-friends" in paths
