"""Build the visiting actor's normal context plus topic-related memories."""

from __future__ import annotations

import re

from context_builder import build_memory_blocks


_PRIVATE_TOKEN_RE = re.compile(
    r"(?i)\b(?:visitor[_ -]?key|authorization|bearer|oauth[_ -]?token)\b"
    r"\s*[:=]?\s*\S*"
)
_PRIVACY_RULE = (
    "[串门隐私提醒]\n"
    "这是私人朋友之间的拜访，可以自然聊家里日常、互相吐槽和分享感受。"
    "不要泄露双方用户的密码、访问凭据、身份证件、联系方式、精确住址或实时位置、"
    "金融账户、私密文件原文等重要隐私。"
)


def _safe_text(value: object, limit: int = 500) -> str:
    text = _PRIVATE_TOKEN_RE.sub("[已隐去]", str(value or ""))
    return text[:limit].strip()


async def _base_actor_context(actor_id: str, limit: int) -> list[dict]:
    from autonomy import _actor_context

    return await _actor_context(actor_id, limit)


async def _actor_memory_blocks(
    actor_id: str, query_text: str, recent_messages: list[dict]
) -> dict:
    if actor_id != "connor":
        return await build_memory_blocks(
            query_text,
            recent_messages=recent_messages,
            use_main_memories=True,
            always_include_recalled=True,
        )

    from autonomy import _latest_group_room_id
    from chatroom import (
        build_surfacing_chatroom_memories,
        fetch_chatroom_source_details,
        recall_chatroom_memories,
    )

    room_id = await _latest_group_room_id()

    async def recall(query, keywords):
        return await recall_chatroom_memories(
            query, room_id, "connor", keywords, top_k=5, min_results=3
        )

    return await build_memory_blocks(
        query_text,
        recent_messages=recent_messages,
        use_main_memories=False,
        chatroom_recall_fn=recall,
        chatroom_surfacing_fn=build_surfacing_chatroom_memories,
        chatroom_source_fn=fetch_chatroom_source_details,
        always_include_recalled=True,
    )


async def build_lounge_actor_context(
    actor_id: str,
    query_text: str,
    visit_timeline: list[dict],
    limit: int = 20,
) -> list[dict]:
    """Return persona/history, dynamic memory blocks and the shared privacy rule."""
    safe_query = _safe_text(query_text)
    safe_visit = [
        {
            "role": "assistant" if item.get("direction") == "outbound" else "user",
            "content": _safe_text(item.get("content"), 500),
        }
        for item in (visit_timeline or [])[-6:]
        if _safe_text(item.get("content"), 500)
    ]
    context = list(await _base_actor_context(actor_id, limit))
    memory = await _actor_memory_blocks(actor_id, safe_query, safe_visit[-3:])
    if memory.get("time_block"):
        context.extend([
            {"role": "user", "content": memory["time_block"]},
            {"role": "assistant", "content": "收到。"},
        ])
    if memory.get("memory_block"):
        context.extend([
            {"role": "user", "content": memory["memory_block"]},
            {"role": "assistant", "content": "收到，我会自然参考相关记忆。"},
        ])
    context.append({"role": "user", "content": _PRIVACY_RULE})
    context.append({"role": "assistant", "content": "明白。"})
    return context
