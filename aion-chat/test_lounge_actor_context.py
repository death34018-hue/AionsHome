import asyncio
from unittest.mock import AsyncMock

import lounge_actor_context


def test_aion_lounge_context_adds_related_memory_and_privacy_rule(monkeypatch):
    monkeypatch.setattr(
        lounge_actor_context,
        "_base_actor_context",
        AsyncMock(return_value=[{"role": "user", "content": "configured persona"}]),
    )
    memory = AsyncMock(return_value={"time_block": "current time", "memory_block": "related memory"})
    monkeypatch.setattr(lounge_actor_context, "_actor_memory_blocks", memory)

    result = asyncio.run(
        lounge_actor_context.build_lounge_actor_context(
            "aion",
            "继续聊花草",
            [{"direction": "inbound", "content": "最近开始养花"}],
        )
    )

    joined = "\n".join(item["content"] for item in result)
    assert "configured persona" in joined
    assert "related memory" in joined
    assert "密码" in joined
    assert "吐槽" in joined
    assert "最近开始养花" in repr(memory.await_args.args[2])


def test_connor_lounge_context_uses_connor_memory_path(monkeypatch):
    monkeypatch.setattr(
        lounge_actor_context,
        "_base_actor_context",
        AsyncMock(return_value=[]),
    )
    memory = AsyncMock(return_value={"time_block": "time", "memory_block": "connor memory"})
    monkeypatch.setattr(lounge_actor_context, "_actor_memory_blocks", memory)

    result = asyncio.run(
        lounge_actor_context.build_lounge_actor_context("connor", "近况", [])
    )

    assert memory.await_args.args[0] == "connor"
    assert any("connor memory" in item["content"] for item in result)


def test_lounge_context_redacts_key_shaped_text(monkeypatch):
    monkeypatch.setattr(
        lounge_actor_context,
        "_base_actor_context",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        lounge_actor_context,
        "_actor_memory_blocks",
        AsyncMock(return_value={"time_block": "", "memory_block": ""}),
    )

    result = asyncio.run(
        lounge_actor_context.build_lounge_actor_context(
            "aion", "visitor_key=private-secret", []
        )
    )

    assert "private-secret" not in repr(result)
