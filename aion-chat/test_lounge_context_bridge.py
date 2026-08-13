import asyncio
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.lounge_context_bridge import create_router


def test_host_context_bridge_requires_local_bearer_and_returns_trusted_context():
    builder = AsyncMock(return_value=[
        {"role": "user", "content": "persona and memory"},
        {"role": "assistant", "content": "received"},
    ])
    app = FastAPI()
    app.include_router(
        create_router(token_provider=lambda: "bridge-secret", context_builder=builder)
    )
    client = TestClient(app)

    denied = client.post(
        "/api/internal/lounge/host-context",
        json={"actor_id": "connor", "query_text": "hello", "recent_messages": []},
    )
    allowed = client.post(
        "/api/internal/lounge/host-context",
        headers={"Authorization": "Bearer bridge-secret"},
        json={"actor_id": "connor", "query_text": "hello", "recent_messages": []},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json() == {"trusted_home_context": "persona and memory\nreceived"}
    assert "bridge-secret" not in allowed.text


def test_host_context_bridge_rejects_other_actor():
    app = FastAPI()
    app.include_router(
        create_router(
            token_provider=lambda: "bridge-secret",
            context_builder=AsyncMock(return_value=[]),
        )
    )
    response = TestClient(app).post(
        "/api/internal/lounge/host-context",
        headers={"Authorization": "Bearer bridge-secret"},
        json={"actor_id": "aion", "query_text": "hello", "recent_messages": []},
    )
    assert response.status_code == 404


def test_reception_report_uses_same_credential_and_inbound_publisher():
    publisher = AsyncMock(return_value={"id": "report-message"})
    app = FastAPI()
    app.include_router(
        create_router(
            token_provider=lambda: "bridge-secret",
            context_builder=AsyncMock(return_value=[]),
            inbound_publisher=publisher,
        )
    )
    response = TestClient(app).post(
        "/api/internal/lounge/reception-report",
        headers={"Authorization": "Bearer bridge-secret"},
        json={
            "visitor_name": "来访朋友",
            "status": "completed",
            "turn_count": 1,
            "messages": [{"direction": "inbound", "content": "你好"}],
        },
    )
    assert response.status_code == 200
    assert publisher.await_count == 1
    assert publisher.await_args.args[:2] == ("connor", "来访朋友")
