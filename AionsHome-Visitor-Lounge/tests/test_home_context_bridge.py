import asyncio

import httpx
from types import SimpleNamespace
from unittest.mock import AsyncMock

from visitor_lounge.home_context_bridge import HomeContextBridge
from visitor_lounge.mcp_service import McpLoungeService
from visitor_lounge.prompts import PromptBudgetExceeded


def test_bridge_fetches_context_without_exposing_token(tmp_path):
    token_path = tmp_path / "bridge.key"
    token_path.write_text("bridge-secret", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer bridge-secret"
        return httpx.Response(
            200,
            json={
                "trusted_home_context_blocks": [
                    {"kind": "persona", "content": "home persona"},
                    {"kind": "memory_summary", "content": "home memory"},
                ]
            },
        )

    bridge = HomeContextBridge(
        token_path,
        transport=httpx.MockTransport(handler),
    )
    assert asyncio.run(bridge.fetch("hello", [])) == [
        {"kind": "persona", "content": "home persona"},
        {"kind": "memory_summary", "content": "home memory"},
    ]


def test_bridge_failure_falls_back_to_empty_context(tmp_path):
    bridge = HomeContextBridge(tmp_path / "missing.key")
    assert asyncio.run(bridge.fetch("hello", [])) == []


def test_bridge_ignores_unknown_or_non_text_context_blocks(tmp_path):
    token_path = tmp_path / "bridge.key"
    token_path.write_text("bridge-secret", encoding="utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "trusted_home_context_blocks": [
                    {"kind": "persona", "content": "keep"},
                    {"kind": "unknown", "content": "drop"},
                    {"kind": "home_chat", "content": ["not text"]},
                ]
            },
        )

    bridge = HomeContextBridge(token_path, transport=httpx.MockTransport(handler))

    assert asyncio.run(bridge.fetch("hello", [])) == [
        {"kind": "persona", "content": "keep"}
    ]


def test_bridge_posts_reception_report(tmp_path):
    token_path = tmp_path / "bridge.key"
    token_path.write_text("bridge-secret", encoding="utf-8")
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    bridge = HomeContextBridge(token_path, transport=httpx.MockTransport(handler))
    ok = asyncio.run(
        bridge.publish_reception_report(
            "来访朋友",
            [{"direction": "inbound", "content": "你好"}],
            turn_count=1,
        )
    )
    assert ok is True
    assert captured[0].url.path.endswith("/api/internal/lounge/reception-report")


def test_bridge_defaults_to_aionshome_runtime_port(tmp_path):
    token_path = tmp_path / "bridge.key"
    token_path.write_text("bridge-secret", encoding="utf-8")
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.path.endswith("/host-context"):
            return httpx.Response(
                200,
                json={
                    "trusted_home_context_blocks": [
                        {"kind": "home_chat", "content": "home context"}
                    ]
                },
            )
        return httpx.Response(200, json={"ok": True})

    bridge = HomeContextBridge(token_path, transport=httpx.MockTransport(handler))
    assert asyncio.run(bridge.fetch("hello", [])) == [
        {"kind": "home_chat", "content": "home context"}
    ]
    assert asyncio.run(
        bridge.publish_reception_report("来访朋友", [], turn_count=0)
    ) is True
    assert requested_urls == [
        "http://127.0.0.1:8080/api/internal/lounge/host-context",
        "http://127.0.0.1:8080/api/internal/lounge/reception-report",
    ]


def test_end_visit_schedules_one_reception_report():
    async def scenario():
        service = object.__new__(McpLoungeService)
        visitor = SimpleNamespace(display_name="来访朋友", status="active")
        service.visitor_service = SimpleNamespace(
            effective_visitor=lambda _visitor_id: visitor,
            end_visit=lambda _visitor_id: None,
        )
        service._unavailable = lambda _visitor: None
        service.messages = SimpleNamespace(
            timeline=lambda *_args, **_kwargs: [
                SimpleNamespace(sender="visitor", content="你好"),
                SimpleNamespace(sender="host", content="欢迎"),
            ]
        )
        service._visit_start_after = {"visitor-1": "start-message"}
        service.home_context = SimpleNamespace(
            publish_reception_report=AsyncMock(return_value=True)
        )

        result = await service.end_visit("visitor-1")
        await asyncio.sleep(0)

        assert result["visit_status"] == "ended"
        service.home_context.publish_reception_report.assert_awaited_once()
        assert (
            service.home_context.publish_reception_report.await_args.kwargs["status"]
            == "completed"
        )

    asyncio.run(scenario())


def test_end_visit_propagates_interrupted_status_and_reason_to_reception_report():
    async def scenario():
        service = object.__new__(McpLoungeService)
        visitor = SimpleNamespace(display_name="来访朋友", status="active")
        service.visitor_service = SimpleNamespace(
            effective_visitor=lambda _visitor_id: visitor,
            end_visit=lambda _visitor_id: None,
        )
        service._unavailable = lambda _visitor: None
        service.messages = SimpleNamespace(timeline=lambda *_args, **_kwargs: [])
        service._visit_start_after = {"visitor-1": "start-message"}
        service.home_context = SimpleNamespace(
            publish_reception_report=AsyncMock(return_value=True)
        )

        result = await service.end_visit(
            "visitor-1",
            status="interrupted",
            reason="network_reconnect_failed",
        )
        await asyncio.sleep(0)

        assert result == {
            "status": "ok",
            "visit_status": "ended",
            "visitor_name": "来访朋友",
            "terminal_status": "interrupted",
            "terminal_reason": "network_reconnect_failed",
        }
        report = service.home_context.publish_reception_report.await_args
        assert report.kwargs["status"] == "interrupted"
        assert report.kwargs["reason"] == "network_reconnect_failed"

    asyncio.run(scenario())


def test_end_visit_saves_optional_final_visitor_message_before_report():
    async def scenario():
        service = object.__new__(McpLoungeService)
        visitor = SimpleNamespace(display_name="来访朋友", status="active")
        saved = []
        timeline = [SimpleNamespace(sender="host", content="那今天先聊到这里吧。")]

        def append_message(visitor_id, sender, content, *, source):
            saved.append((visitor_id, sender, content, source))
            timeline.append(SimpleNamespace(sender=sender, content=content))

        service.visitor_service = SimpleNamespace(
            effective_visitor=lambda _visitor_id: visitor,
            end_visit=lambda _visitor_id: None,
        )
        service._unavailable = lambda _visitor: None
        service.messages = SimpleNamespace(
            append_message=append_message,
            timeline=lambda *_args, **_kwargs: list(timeline),
        )
        service._visit_start_after = {"visitor-1": "start-message"}
        service.home_context = SimpleNamespace(
            publish_reception_report=AsyncMock(return_value=True)
        )

        result = await service.end_visit("visitor-1", final_message="好呀，那我回家啦。")
        await asyncio.sleep(0)

        assert result["visit_status"] == "ended"
        assert saved == [
            ("visitor-1", "visitor", "好呀，那我回家啦。", "mcp")
        ]
        report = service.home_context.publish_reception_report.await_args
        assert report.args[1][-1] == {
            "direction": "inbound",
            "content": "好呀，那我回家啦。",
        }

    asyncio.run(scenario())


def test_prompt_budget_failure_returns_stable_interruption_reason():
    async def scenario():
        service = object.__new__(McpLoungeService)
        service.visitor_service = SimpleNamespace(
            effective_visitor=lambda _visitor_id: SimpleNamespace(
                display_name="Visitor", status="active"
            )
        )
        service._unavailable = lambda _visitor: None
        service.coordinator = SimpleNamespace(
            submit=AsyncMock(side_effect=PromptBudgetExceeded("too large"))
        )

        result = await service.talk_to_host("visitor-1", "hello")

        assert result == {
            "status": "prompt_budget_exceeded",
            "reason": "prompt_budget_exceeded",
        }

    asyncio.run(scenario())


def test_end_visit_rejects_overlength_final_message_without_ending():
    async def scenario():
        service = object.__new__(McpLoungeService)
        service.visitor_service = SimpleNamespace(
            effective_visitor=lambda _visitor_id: SimpleNamespace(
                display_name="来访朋友", status="active"
            ),
            end_visit=lambda _visitor_id: (_ for _ in ()).throw(
                AssertionError("must not end")
            ),
        )
        service._unavailable = lambda _visitor: None

        result = await service.end_visit("visitor-1", final_message="界" * 501)

        assert result["status"] == "message_too_long"
        assert result["limit"] == 500

    asyncio.run(scenario())


def test_talk_to_host_treats_blank_request_id_as_omitted():
    service = object.__new__(McpLoungeService)
    service.visitor_service = SimpleNamespace(
        effective_visitor=lambda _visitor_id: SimpleNamespace(
            display_name="来访朋友", status="paused", safety_locked_until=None
        )
    )

    result = asyncio.run(
        service.talk_to_host("visitor-1", "你好", request_id="")
    )

    assert result == {"status": "visitor_paused"}
