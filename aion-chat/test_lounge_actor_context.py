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


def test_lounge_memory_disables_source_detail_loading(monkeypatch):
    build = AsyncMock(
        return_value={"time_block": "", "memory_block": "summary only"}
    )
    monkeypatch.setattr(lounge_actor_context, "build_memory_blocks", build)

    asyncio.run(lounge_actor_context._actor_memory_blocks("aion", "近况", []))

    assert build.await_args.kwargs["include_source_details"] is False
    assert build.await_args.kwargs["max_recalled_memories"] == 3


def test_lounge_context_labels_blocks_by_eviction_priority(monkeypatch):
    monkeypatch.setattr(
        lounge_actor_context,
        "_base_actor_context",
        AsyncMock(
            return_value=[
                {"role": "user", "content": "[系统设定 - 你的角色设定]\n核心人设"},
                {"role": "assistant", "content": "收到。"},
                {"role": "user", "content": "最近家中聊天"},
            ]
        ),
    )
    monkeypatch.setattr(
        lounge_actor_context,
        "_actor_memory_blocks",
        AsyncMock(return_value={"time_block": "当前状态", "memory_block": "摘要记忆"}),
    )

    result = asyncio.run(
        lounge_actor_context.build_lounge_actor_context("connor", "近况", [])
    )

    assert [item["lounge_context_kind"] for item in result] == [
        "persona",
        "persona",
        "home_chat",
        "dynamic_state",
        "dynamic_state",
        "memory_summary",
        "memory_summary",
        "safety",
        "safety",
    ]


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
