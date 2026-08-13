import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from mcp import McpError
from mcp.types import ErrorData
from pydantic import BaseModel, ValidationError

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mcp_client
from mcp_client import MCPManager


class _RequiredResult(BaseModel):
    value: int


try:
    _RequiredResult.model_validate({"value": "not-an-integer"})
except ValidationError as error:
    INSTALLED_PYDANTIC_VALIDATION_ERROR = error


def test_ephemeral_connection_never_persists_bearer_key(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_client, "MCP_SERVERS_PATH", tmp_path / "mcp_servers.json")
    manager = MCPManager()
    monkeypatch.setattr(manager, "_connect_http", AsyncMock(return_value=[]))

    asyncio.run(
        manager.connect_ephemeral(
            "visitor-lounge:aion:friend-1",
            "https://friend.example/mcp",
            {"Authorization": "Bearer private-key"},
        )
    )

    assert not (tmp_path / "mcp_servers.json").exists()


def test_ephemeral_connection_reuses_active_connection_id(monkeypatch):
    manager = MCPManager()
    first_session = object()
    second_session = object()
    created_sessions = [first_session, second_session]

    async def connect_http(connection_id, _config):
        session = created_sessions.pop(0)
        manager._connections[connection_id] = {
            "session": session,
            "tools": [{"name": "visitor.accept"}],
        }
        return manager._connections[connection_id]["tools"]

    connect_http_mock = AsyncMock(side_effect=connect_http)
    monkeypatch.setattr(manager, "_connect_http", connect_http_mock)

    first_tools = asyncio.run(
        manager.connect_ephemeral(
            "visitor-lounge:aion:friend-1",
            "https://friend.example/mcp",
            {"Authorization": "Bearer private-key"},
        )
    )
    repeated_tools = asyncio.run(
        manager.connect_ephemeral(
            "visitor-lounge:aion:friend-1",
            "https://friend.example/mcp",
            {"Authorization": "Bearer private-key"},
        )
    )

    assert repeated_tools == first_tools
    assert manager._connections["visitor-lounge:aion:friend-1"]["session"] is first_session
    assert connect_http_mock.await_count == 1


def test_http_connection_unwinds_transport_when_session_enter_fails(
    monkeypatch, caplog
):
    events = []

    class Transport:
        async def __aenter__(self):
            events.append("transport_enter")
            return object(), object(), object()

        async def __aexit__(self, *_args):
            events.append("transport_exit")

    class Session:
        async def __aenter__(self):
            events.append("session_enter_failed")
            raise RuntimeError("Bearer private-key")

        async def __aexit__(self, *_args):
            events.append("session_exit")

    monkeypatch.setattr(mcp_client, "streamablehttp_client", lambda **_kwargs: Transport())
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *_args: Session())
    manager = MCPManager()

    with pytest.raises(RuntimeError, match="private-key"):
        asyncio.run(
            manager.connect_ephemeral(
                "visitor-lounge:actor-1:friend-1",
                "https://friend.example/mcp",
                {"Authorization": "Bearer private-key"},
            )
        )

    assert events == ["transport_enter", "session_enter_failed", "transport_exit"]
    assert manager._connections == {}
    assert "private-key" not in caplog.text
    assert "friend.example" not in caplog.text


def test_http_connection_unwinds_session_then_transport_on_cancellation(
    monkeypatch, caplog
):
    events = []

    class Transport:
        async def __aenter__(self):
            events.append("transport_enter")
            return object(), object(), object()

        async def __aexit__(self, *_args):
            events.append("transport_exit")

    class Session:
        async def __aenter__(self):
            events.append("session_enter")
            return self

        async def initialize(self):
            events.append("initialize_cancelled")
            raise asyncio.CancelledError()

        async def __aexit__(self, *_args):
            events.append("session_exit")

    monkeypatch.setattr(mcp_client, "streamablehttp_client", lambda **_kwargs: Transport())
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *_args: Session())
    manager = MCPManager()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            manager.connect_ephemeral(
                "visitor-lounge:actor-1:friend-1",
                "https://friend.example/mcp",
                {"Authorization": "Bearer private-key"},
            )
        )

    assert events == [
        "transport_enter",
        "session_enter",
        "initialize_cancelled",
        "session_exit",
        "transport_exit",
    ]
    assert manager._connections == {}
    assert "private-key" not in caplog.text
    assert "friend.example" not in caplog.text


def test_disconnect_cleanup_failures_do_not_log_connection_secrets(caplog):
    class FailingContext:
        async def __aexit__(self, *_args):
            raise RuntimeError(
                "https://friend.example/mcp Authorization: Bearer private-key"
            )

    manager = MCPManager()
    manager._connections["visitor-lounge:actor-1:friend-1"] = {
        "session": FailingContext(),
        "transport_cm": FailingContext(),
        "tools": [],
    }

    asyncio.run(manager.disconnect("visitor-lounge:actor-1:friend-1"))

    assert manager._connections == {}
    assert "private-key" not in caplog.text
    assert "friend.example" not in caplog.text


def test_call_tool_json_prefers_structured_content():
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                structuredContent={"accepted": True},
                content=[SimpleNamespace(text='{"ignored": true}')],
            )
        )
    )
    manager = MCPManager()
    manager._connections["visitor-lounge:aion:friend-1"] = {"session": session}

    result = asyncio.run(
        manager.call_tool_json(
            "visitor-lounge:aion:friend-1", "visitor.accept", {"visit_id": "visit-1"}
        )
    )

    assert result == {"accepted": True}


def test_call_tool_json_accepts_one_text_json_object():
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                structuredContent=None,
                content=[SimpleNamespace(text='{"visit_id": "visit-1"}')],
            )
        )
    )
    manager = MCPManager()
    manager._connections["visitor-lounge:aion:friend-1"] = {"session": session}

    result = asyncio.run(
        manager.call_tool_json(
            "visitor-lounge:aion:friend-1", "visitor.accept", {"visit_id": "visit-1"}
        )
    )

    assert result == {"visit_id": "visit-1"}


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(structuredContent=None, content=[]),
        SimpleNamespace(
            structuredContent=None,
            content=[SimpleNamespace(text="{}"), SimpleNamespace(text="{}")],
        ),
        SimpleNamespace(
            structuredContent=None,
            content=[SimpleNamespace(text="not-json")],
        ),
        SimpleNamespace(
            structuredContent=None,
            content=[SimpleNamespace(text="[]")],
        ),
        SimpleNamespace(structuredContent=[], content=[]),
    ],
)
def test_call_tool_json_classifies_malformed_payloads_as_protocol_errors(result):
    session = SimpleNamespace(call_tool=AsyncMock(return_value=result))
    manager = MCPManager()
    manager._connections["visitor-lounge:actor-1:friend-1"] = {"session": session}

    with pytest.raises(mcp_client.MCPToolProtocolError) as captured:
        asyncio.run(
            manager.call_tool_json(
                "visitor-lounge:actor-1:friend-1", "talk_to_host", {}
            )
        )

    assert "not-json" not in str(captured.value)


@pytest.mark.parametrize(
    "failure",
    [
        McpError(
            ErrorData(
                code=-32603,
                message="private deterministic JSON-RPC failure",
            )
        ),
        McpError(
            ErrorData(
                code=-32000,
                message="private server error using connection code",
            )
        ),
        McpError(
            ErrorData(
                code=408,
                message="application failure",
            )
        ),
        INSTALLED_PYDANTIC_VALIDATION_ERROR,
        RuntimeError("private output-schema validation failure"),
        ValueError("private deterministic SDK failure"),
        httpx.LocalProtocolError("private deterministic HTTP request failure"),
        httpx.UnsupportedProtocol("private deterministic URL scheme failure"),
    ],
)
def test_call_tool_json_classifies_sdk_protocol_and_validation_failures(failure):
    session = SimpleNamespace(call_tool=AsyncMock(side_effect=failure))
    manager = MCPManager()
    manager._connections["visitor-lounge:actor-1:friend-1"] = {"session": session}

    with pytest.raises(mcp_client.MCPToolProtocolError) as captured:
        asyncio.run(
            manager.call_tool_json(
                "visitor-lounge:actor-1:friend-1", "talk_to_host", {}
            )
        )

    assert "private" not in str(captured.value)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadError(
            "https://friend.example/mcp private-key",
            request=httpx.Request("POST", "https://friend.example/mcp"),
        ),
        anyio.BrokenResourceError("private broken stream"),
        anyio.ClosedResourceError("private closed stream"),
        anyio.EndOfStream("private ended stream"),
        TimeoutError("private timeout"),
        McpError(
            ErrorData(
                code=-32000,
                message="Connection closed",
            )
        ),
    ],
)
def test_call_tool_json_classifies_known_transport_and_stream_failures(failure):
    session = SimpleNamespace(
        call_tool=AsyncMock(side_effect=failure)
    )
    manager = MCPManager()
    manager._connections["visitor-lounge:actor-1:friend-1"] = {"session": session}

    with pytest.raises(mcp_client.MCPToolTransportError) as captured:
        asyncio.run(
            manager.call_tool_json(
                "visitor-lounge:actor-1:friend-1", "talk_to_host", {}
            )
        )

    assert "private" not in str(captured.value)
    assert "friend.example" not in str(captured.value)
