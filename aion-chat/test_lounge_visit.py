import asyncio
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp import McpError
from mcp.types import ErrorData

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lounge_friends import LoungeFriend
import lounge_visit
from lounge_visit import LoungeVisitCoordinator
from mcp_client import (
    MCPManager,
    MCPToolTransportError,
)


APPROVED_TOOLS = [
    "get_lounge_info",
    "claim_identity",
    "begin_visit",
    "talk_to_host",
    "get_visit_state",
    "end_visit",
]


def make_friend(*, max_turns=1, enabled=True):
    return LoungeFriend(
        id="friend-1",
        actor_id="actor-1",
        display_name="Remote friend",
        lounge_url="https://friend.example/mcp",
        visitor_key="private-visitor-key",
        relationship_note="Old friend",
        enabled=enabled,
        allow_autonomous=True,
        cooldown_hours=12,
        max_turns=max_turns,
        last_visit_at=None,
        created_at=1.0,
        updated_at=1.0,
    )


class FakeFriendStore:
    def __init__(self, friend=None):
        self.friend = friend or make_friend()
        self.marked = []

    def get_owned(self, actor_id, friend_id):
        if actor_id != self.friend.actor_id or friend_id != self.friend.id:
            raise KeyError("private lookup details")
        return self.friend

    def mark_visited(self, actor_id, friend_id, when):
        self.marked.append((actor_id, friend_id, when))
        return self.friend


class FakeRepository:
    def __init__(self):
        self.started = []
        self.messages = []
        self.progress = []
        self.finished = []

    async def start(self, actor_id, friend_id, trigger_source, topic):
        self.started.append((actor_id, friend_id, trigger_source, topic))
        return "visit-1"

    async def append_message(self, visit_id, direction, content, remote_message_id=""):
        self.messages.append(
            (visit_id, direction, content, remote_message_id)
        )
        return f"local-{len(self.messages)}"

    async def finish(self, visit_id, status, turn_count, error=""):
        self.finished.append((visit_id, status, turn_count, error))

    async def update_progress(self, visit_id, turn_count):
        self.progress.append((visit_id, turn_count))


class FakeMCPManager:
    def __init__(self, responses, *, tools=None):
        self.responses = {
            name: list(values) for name, values in responses.items()
        }
        self.tools = tools or [{"name": name} for name in APPROVED_TOOLS]
        self.connects = []
        self.calls = []
        self.disconnects = []

    async def connect_ephemeral(self, connection_id, url, headers):
        self.connects.append((connection_id, url, dict(headers)))
        return self.tools

    async def call_tool_json(self, connection_id, tool_name, arguments):
        self.calls.append((connection_id, tool_name, dict(arguments)))
        value = self.responses[tool_name].pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    async def disconnect(self, connection_id):
        self.disconnects.append(connection_id)


class SDKBoundaryFakeMCPManager(FakeMCPManager):
    """Keep fake transport lifecycle while exercising real SDK error mapping."""

    async def call_tool_json(self, connection_id, tool_name, arguments):
        self.calls.append((connection_id, tool_name, dict(arguments)))
        value = self.responses[tool_name].pop(0)
        if not isinstance(value, BaseException):
            return value
        boundary = MCPManager()
        boundary._connections[connection_id] = {
            "session": SimpleNamespace(call_tool=AsyncMock(side_effect=value))
        }
        return await boundary.call_tool_json(
            connection_id, tool_name, arguments
        )


def make_coordinator(manager, *, friend=None):
    store = FakeFriendStore(friend)
    repository = FakeRepository()
    coordinator = LoungeVisitCoordinator(
        store,
        repository,
        manager,
        actor_name_resolver=lambda actor_id: "Configured visitor",
        clock=lambda: 1234.5,
    )
    return coordinator, store, repository


def standard_responses(*, talk=None, claimed=True, begin=None, end=None):
    return {
        "get_lounge_info": [
            {"status": "ok", "identity_claimed": claimed}
        ],
        "claim_identity": [{"status": "claimed"}],
        "begin_visit": [
            begin
            or {"status": "ok", "quota_remaining": 15, "messages": []}
        ],
        "talk_to_host": talk
        or [{"status": "ok", "reply": "Welcome", "action": "continue"}],
        "end_visit": [end or {"status": "ok"}],
    }


def test_visit_uses_fixed_tool_order_and_filters_remote_timeline():
    timeline = [
        {"id": f"message-{index}", "sender": "host", "content": f"text-{index}"}
        for index in range(22)
    ] + [{"id": "binary", "content": {"not": "text"}}]
    manager = FakeMCPManager(
        standard_responses(
            begin={"status": "ok", "quota_remaining": 15, "messages": timeline}
        )
    )
    coordinator, store, repository = make_coordinator(manager)
    received_timeline = []

    async def compose(actor_id, friend, remote_timeline, topic, turn):
        assert actor_id == "actor-1"
        assert friend.visitor_key == ""
        assert friend.lounge_url == ""
        assert topic == "Catch up"
        assert turn == 1
        received_timeline.extend(remote_timeline)
        return "Hello"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert [name for _, name, _ in manager.calls] == [
        "get_lounge_info",
        "begin_visit",
        "talk_to_host",
        "end_visit",
    ]
    assert len(received_timeline) == 20
    assert received_timeline[0]["content"] == "text-2"
    assert received_timeline[-1]["content"] == "text-21"
    assert result.status == "completed"
    assert result.turn_count == 1
    assert result.final_reply == "Welcome"
    assert repository.finished == [("visit-1", "completed", 1, "")]
    assert len(store.marked) == 1
    assert manager.disconnects == ["visitor-lounge:actor-1:friend-1"]
    uuid.UUID(manager.calls[2][2]["request_id"])


@pytest.mark.parametrize("trigger_source", ["manual", "chat"])
def test_exact_friend_credentials_are_scrubbed_from_all_remote_text_boundaries(
    trigger_source,
):
    friend = replace(
        make_friend(),
        display_name="ordinary name private-visitor-key",
        relationship_note="ordinary note private-visitor-key",
    )
    manager = FakeMCPManager(
        standard_responses(
            begin={
                "status": "ok",
                "messages": [
                    {
                        "id": (
                            "timeline-private-visitor-key-"
                            "https://friend.example/mcp"
                        ),
                        "sender": "host",
                        "content": (
                            "ordinary context private-visitor-key "
                            "https://friend.example/mcp remains useful"
                        ),
                    }
                ],
            },
            talk=[
                {
                    "status": "ok",
                    "reply": (
                        "ordinary reply private-visitor-key "
                        "https://friend.example/mcp remains useful"
                    ),
                    "host_message_id": (
                        "remote-private-visitor-key-"
                        "https://friend.example/mcp"
                    ),
                    "action": "end",
                }
            ],
        )
    )
    coordinator, _, repository = make_coordinator(manager, friend=friend)
    callback_inputs = []

    async def compose(_actor_id, prompt_friend, timeline, topic, _turn):
        callback_inputs.append((prompt_friend, timeline, topic))
        return "ordinary outbound"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1",
            "friend-1",
            trigger_source,
            "ordinary topic private-visitor-key",
            compose,
        )
    )

    all_local_outputs = repr(
        (callback_inputs, result, repository.started, repository.messages)
    )
    assert friend.visitor_key not in all_local_outputs
    assert friend.lounge_url not in all_local_outputs
    assert "ordinary name" in all_local_outputs
    assert "ordinary note" in all_local_outputs
    assert "ordinary topic" in all_local_outputs
    assert "ordinary context" in all_local_outputs
    assert "ordinary reply" in all_local_outputs
    assert "ordinary outbound" in all_local_outputs


def test_unclaimed_identity_is_claimed_with_configured_actor_name():
    manager = FakeMCPManager(standard_responses(claimed=False))
    coordinator, _, _ = make_coordinator(manager)

    async def compose(*_args):
        return "Hello"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "chat", "Catch up", compose
        )
    )

    assert result.status == "completed"
    assert [name for _, name, _ in manager.calls] == [
        "get_lounge_info",
        "claim_identity",
        "begin_visit",
        "talk_to_host",
        "end_visit",
    ]
    assert manager.calls[1][2] == {
        "name": "Configured visitor",
        "consent": True,
    }


def test_overlength_generated_message_is_rejected_without_sending_or_truncating():
    manager = FakeMCPManager(standard_responses())
    coordinator, _, repository = make_coordinator(manager)

    async def compose(*_args):
        return "界" * 501

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result.status == "rejected"
    assert result.reason == "message_too_long"
    assert "talk_to_host" not in [name for _, name, _ in manager.calls]
    assert [name for _, name, _ in manager.calls][-1] == "end_visit"
    assert repository.messages == []


def test_generation_failure_interrupts_and_best_effort_ends_remote_visit():
    manager = FakeMCPManager(
        standard_responses(
            talk=[{"status": "generation_failed", "request_id": "remote"}]
        )
    )
    coordinator, _, repository = make_coordinator(manager)

    async def compose(*_args):
        return "Hello"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result.status == "interrupted"
    assert result.reason == "generation_failed"
    assert [name for _, name, _ in manager.calls][-1] == "end_visit"
    assert repository.finished == [
        ("visit-1", "interrupted", 1, "generation_failed")
    ]


def test_action_end_stops_after_first_turn_and_closes_visit():
    manager = FakeMCPManager(
        standard_responses(
            talk=[{"status": "ok", "reply": "Goodbye", "action": "end"}]
        )
    )
    coordinator, _, _ = make_coordinator(manager, friend=make_friend(max_turns=8))
    compose_count = 0

    async def compose(*_args):
        nonlocal compose_count
        compose_count += 1
        return "Hello"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result.status == "completed"
    assert result.turn_count == 1
    assert compose_count == 1
    assert [name for _, name, _ in manager.calls].count("talk_to_host") == 1
    assert [name for _, name, _ in manager.calls][-1] == "end_visit"


def test_visitor_goodbye_marker_is_hidden_and_host_end_closes_immediately():
    manager = FakeMCPManager(
        standard_responses(
            talk=[{"status": "ok", "reply": "好，明天见。", "action": "end"}]
        )
    )
    coordinator, _, repository = make_coordinator(
        manager, friend=make_friend(max_turns=6)
    )

    async def compose(*_args):
        return "那我回家啦，明天见。<<LOUNGE_VISIT_ACTION:closing>>"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result.status == "completed"
    assert result.turn_count == 1
    assert repository.messages[0][2] == "那我回家啦，明天见。"
    talk_call = next(call for call in manager.calls if call[1] == "talk_to_host")
    assert talk_call[2]["message"] == "那我回家啦，明天见。"
    assert "LOUNGE_VISIT_ACTION" not in str(repository.messages + manager.calls)


def test_host_closing_gets_one_final_visitor_reply_without_another_host_turn():
    manager = FakeMCPManager(
        standard_responses(
            talk=[
                {
                    "status": "ok",
                    "reply": "时间不早了，今天先聊到这里吧。",
                    "action": "closing",
                }
            ]
        )
    )
    coordinator, _, repository = make_coordinator(
        manager, friend=make_friend(max_turns=6)
    )
    compose_calls = []

    async def compose(_actor, _friend, timeline, _topic, turn):
        compose_calls.append((list(timeline), turn))
        if len(compose_calls) == 1:
            return "好呀，今天聊得很开心。"
        assert timeline[-1]["_lounge_control"] == "reply_and_end"
        return "好，那我回家啦。<<LOUNGE_VISIT_ACTION:end>>"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result.status == "completed"
    assert len(compose_calls) == 2
    assert [name for _, name, _ in manager.calls].count("talk_to_host") == 1
    end_call = next(call for call in manager.calls if call[1] == "end_visit")
    assert end_call[2] == {"final_message": "好，那我回家啦。"}
    assert repository.messages[-1][1:3] == ("outbound", "好，那我回家啦。")


def test_begin_failure_does_not_call_end_visit():
    manager = FakeMCPManager(
        standard_responses(begin={"status": "lounge_closed"})
    )
    coordinator, _, _ = make_coordinator(manager)

    async def compose(*_args):
        raise AssertionError("must not compose")

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result.status == "rejected"
    assert "end_visit" not in [name for _, name, _ in manager.calls]


def test_friend_turn_limit_prevents_generating_a_fifth_message():
    manager = FakeMCPManager(
        standard_responses(
            talk=[
                {"status": "ok", "reply": f"Reply {turn}", "action": "continue"}
                for turn in range(4)
            ]
        )
    )
    coordinator, _, _ = make_coordinator(manager, friend=make_friend(max_turns=4))
    compose_turns = []

    async def compose(_actor_id, _friend, _timeline, _topic, turn):
        compose_turns.append(turn)
        return f"Message {turn}"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "autonomy", "Catch up", compose
        )
    )

    assert result.status == "completed"
    assert result.turn_count == 4
    assert compose_turns == [1, 2, 3, 4]
    assert [name for _, name, _ in manager.calls].count("talk_to_host") == 4


def test_coordinator_hard_caps_untrusted_friend_turn_limit_at_eight():
    manager = FakeMCPManager(
        standard_responses(
            talk=[
                {"status": "ok", "reply": f"Reply {turn}", "action": "continue"}
                for turn in range(8)
            ]
        )
    )
    coordinator, _, _ = make_coordinator(manager, friend=make_friend(max_turns=99))
    compose_turns = []

    async def compose(_actor_id, _friend, _timeline, _topic, turn):
        compose_turns.append(turn)
        return f"Message {turn}"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result.status == "completed"
    assert result.turn_count == 8
    assert compose_turns == list(range(1, 9))


def test_global_lock_serializes_visits_across_coordinator_instances():
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_connected_before_release = False

    class BlockingManager(FakeMCPManager):
        def __init__(self, responses, block):
            super().__init__(responses)
            self.block = block

        async def connect_ephemeral(self, connection_id, url, headers):
            nonlocal second_connected_before_release
            if not self.block and not release_first.is_set():
                second_connected_before_release = True
            tools = await super().connect_ephemeral(connection_id, url, headers)
            if self.block:
                first_entered.set()
                await release_first.wait()
            return tools

    async def scenario():
        first, _, _ = make_coordinator(
            BlockingManager(standard_responses(), True)
        )
        second, _, _ = make_coordinator(
            BlockingManager(standard_responses(), False)
        )

        async def compose(*_args):
            return "Hello"

        first_task = asyncio.create_task(
            first.run_visit("actor-1", "friend-1", "manual", "One", compose)
        )
        await first_entered.wait()
        second_task = asyncio.create_task(
            second.run_visit("actor-1", "friend-1", "manual", "Two", compose)
        )
        await asyncio.sleep(0)
        assert not second_task.done()
        release_first.set()
        first_result, second_result = await asyncio.gather(first_task, second_task)
        assert first_result.status == "completed"
        assert second_result.status == "completed"

    asyncio.run(scenario())
    assert second_connected_before_release is False


def test_network_failure_reconnects_once_with_same_request_id():
    secret_error = httpx.ReadError(
        "https://friend.example/mcp Authorization: Bearer private-visitor-key",
        request=httpx.Request("POST", "https://friend.example/mcp"),
    )
    manager = SDKBoundaryFakeMCPManager(
        standard_responses(
            talk=[
                secret_error,
                {"status": "ok", "reply": "Recovered", "action": "continue"},
            ]
        )
    )
    coordinator, _, repository = make_coordinator(manager)

    async def compose(*_args):
        return "Hello"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    talk_calls = [call for call in manager.calls if call[1] == "talk_to_host"]
    assert result.status == "completed"
    assert len(manager.connects) == 2
    assert len(talk_calls) == 2
    assert talk_calls[0][2]["request_id"] == talk_calls[1][2]["request_id"]
    assert "private-visitor-key" not in repr(result)
    assert "friend.example" not in repr(result)
    assert "private-visitor-key" not in repr(repository.finished)
    assert "friend.example" not in repr(repository.finished)


@pytest.mark.parametrize(
    ("error_code", "error_message"),
    [
        (-32603, "private malformed JSON-RPC payload"),
        (408, "application failure"),
    ],
)
def test_protocol_failure_does_not_reconnect_or_expose_payload_details(
    error_code, error_message
):
    manager = SDKBoundaryFakeMCPManager(
        standard_responses(
            talk=[
                McpError(
                    ErrorData(
                        code=error_code,
                        message=error_message,
                    )
                )
            ]
        )
    )
    coordinator, _, repository = make_coordinator(manager)

    async def compose(*_args):
        return "Hello"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result.status == "interrupted"
    assert result.reason == "remote_protocol_error"
    assert len(manager.connects) == 1
    assert [name for _, name, _ in manager.calls].count("talk_to_host") == 1
    assert error_message not in repr(result)
    assert repository.finished == [
        ("visit-1", "interrupted", 1, "remote_protocol_error")
    ]


def test_second_network_failure_does_not_reconnect_again_or_leak_details():
    manager = FakeMCPManager(
        standard_responses(
            talk=[
                MCPToolTransportError("Bearer private-visitor-key"),
                MCPToolTransportError("https://friend.example/mcp"),
            ]
        )
    )
    coordinator, _, repository = make_coordinator(manager)

    async def compose(*_args):
        return "Hello"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result.status == "interrupted"
    assert result.reason == "connection_failed"
    assert len(manager.connects) == 2
    assert repository.finished == [
        ("visit-1", "interrupted", 1, "connection_failed")
    ]


def test_reconnect_budget_is_one_for_the_entire_visit():
    manager = FakeMCPManager(
        standard_responses(
            talk=[
                MCPToolTransportError("first disconnect"),
                {"status": "ok", "reply": "Recovered", "action": "continue"},
                MCPToolTransportError("second disconnect"),
                {"status": "ok", "reply": "Must not retry", "action": "continue"},
            ]
        )
    )
    coordinator, _, repository = make_coordinator(
        manager, friend=make_friend(max_turns=2)
    )

    async def compose(*_args):
        return "Hello"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result.status == "interrupted"
    assert result.reason == "connection_failed"
    assert len(manager.connects) == 2
    assert [name for _, name, _ in manager.calls].count("talk_to_host") == 3
    assert repository.finished == [
        ("visit-1", "interrupted", 2, "connection_failed")
    ]


def test_overall_timeout_finishes_started_visit_and_disconnects(monkeypatch):
    manager = FakeMCPManager(standard_responses())

    async def slow_connect(connection_id, url, headers):
        manager.connects.append((connection_id, url, dict(headers)))
        await asyncio.sleep(0.05)
        return manager.tools

    manager.connect_ephemeral = slow_connect
    coordinator, _, repository = make_coordinator(manager)
    monkeypatch.setattr(lounge_visit, "_TOTAL_TIMEOUT_SECONDS", 0.01)

    async def compose(*_args):
        return "Hello"

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result == lounge_visit.LoungeVisitResult(
        "visit-1", "interrupted", 0, "", "visit_timeout"
    )
    assert repository.finished == [
        ("visit-1", "interrupted", 0, "visit_timeout")
    ]
    assert manager.disconnects == ["visitor-lounge:actor-1:friend-1"]


def test_remote_availability_statuses_are_rejected():
    async def scenario(remote_status):
        manager = FakeMCPManager(
            standard_responses(begin={"status": remote_status})
        )
        coordinator, _, repository = make_coordinator(manager)

        async def compose(*_args):
            raise AssertionError("compose must not run")

        result = await coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
        assert result.status == "rejected"
        assert result.reason == remote_status
        assert repository.finished == [
            ("visit-1", "rejected", 0, remote_status)
        ]

    for status in (
        "visitor_locked",
        "visitor_paused",
        "quota_exhausted",
        "lounge_closed",
    ):
        asyncio.run(scenario(status))


def test_busy_and_unknown_remote_statuses_are_interrupted_safely():
    async def scenario(remote_status):
        manager = FakeMCPManager(
            standard_responses(talk=[{"status": remote_status, "error": "secret"}])
        )
        coordinator, _, repository = make_coordinator(manager)

        async def compose(*_args):
            return "Hello"

        result = await coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
        assert result.status == "interrupted"
        assert result.reason == (
            remote_status if remote_status in {"visitor_busy", "service_busy"}
            else "remote_protocol_error"
        )
        assert "secret" not in repr(result)
        assert "secret" not in repr(repository.finished)

    for status in ("visitor_busy", "service_busy", "tool_internal_secret"):
        asyncio.run(scenario(status))


def test_missing_approved_tools_interrupts_before_any_remote_tool_call():
    manager = FakeMCPManager(
        standard_responses(), tools=[{"name": "get_lounge_info"}, {"name": "evil"}]
    )
    coordinator, _, repository = make_coordinator(manager)

    async def compose(*_args):
        raise AssertionError("compose must not run")

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result.status == "interrupted"
    assert result.reason == "unsupported_server"
    assert manager.calls == []
    assert repository.finished == [
        ("visit-1", "interrupted", 0, "unsupported_server")
    ]


def test_disabled_friend_is_rejected_without_connection():
    manager = FakeMCPManager(standard_responses())
    coordinator, _, repository = make_coordinator(
        manager, friend=make_friend(enabled=False)
    )

    async def compose(*_args):
        raise AssertionError("compose must not run")

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result.status == "rejected"
    assert result.reason == "friend_disabled"
    assert manager.connects == []
    assert repository.finished == [
        ("visit-1", "rejected", 0, "friend_disabled")
    ]


def test_corrupt_friend_store_is_interrupted_without_exposing_details():
    class CorruptFriendStore(FakeFriendStore):
        def get_owned(self, actor_id, friend_id):
            raise ValueError(
                "https://friend.example/mcp Bearer private-visitor-key"
            )

    manager = FakeMCPManager(standard_responses())
    repository = FakeRepository()
    coordinator = LoungeVisitCoordinator(
        CorruptFriendStore(),
        repository,
        manager,
        actor_name_resolver=lambda actor_id: "Configured visitor",
        clock=lambda: 1234.5,
    )

    async def compose(*_args):
        raise AssertionError("compose must not run")

    result = asyncio.run(
        coordinator.run_visit(
            "actor-1", "friend-1", "manual", "Catch up", compose
        )
    )

    assert result == lounge_visit.LoungeVisitResult(
        "", "interrupted", 0, "", "local_state_failed"
    )
    assert manager.connects == []
    assert repository.started == []
    assert "private-visitor-key" not in repr(result)
    assert "friend.example" not in repr(result)
