"""Create safe, ordinary chat messages for completed lounge visits."""

from __future__ import annotations

import re
from typing import Awaitable, Callable

from lounge_terminal_reasons import terminal_reason_text


_URL_RE = re.compile(r"(?i)https?://\S+")
_AUTH_RE = re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S+")
_SECRET_RE = re.compile(
    r"(?i)\b(?:bearer\s+|visitor[_ -]?key\s*[:=]?|"
    r"oauth[_ -]?token\s*[:=]?)\s*[^\s，。；;]*"
)
_TOOL_RE = re.compile(
    r"(?i)\b(?:get_lounge_info|claim_identity|begin_visit|talk_to_host|"
    r"get_visit_state|end_visit)\b"
)
_FALLBACK = (
    "我刚结束了这次串门。本次会面没有留下足够的可总结内容，"
    "详细状态可以在好友串门记录中查看。"
)


def _clean(value: object, limit: int = 500) -> str:
    text = str(value or "")
    text = _URL_RE.sub("", text)
    text = _AUTH_RE.sub("", text)
    text = _SECRET_RE.sub("", text)
    text = _TOOL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" ，。；;:-")
    return text[:limit].strip()


def _transcript(messages: list[dict]) -> str:
    lines: list[str] = []
    for item in messages[-16:]:
        content = _clean(item.get("content"), 500)
        if not content:
            continue
        speaker = "我" if item.get("direction") == "outbound" else "朋友"
        lines.append(f"{speaker}：{content}")
    return "\n".join(lines)


async def _default_generate(actor_id: str, instruction: str) -> str:
    from autonomy import _actor_context, _call_actor

    messages = await _actor_context(actor_id, 20)
    messages.append({"role": "user", "content": instruction})
    return await _call_actor(actor_id, messages)


async def _default_save(actor_id: str, content: str, *, attachments: list) -> dict | None:
    from autonomy import _save_private_message

    return await _save_private_message(
        actor_id,
        content,
        attachments=attachments,
        auto_tts=False,
    )


async def publish_outbound_report(
    actor_id: str,
    partner_name: str,
    result,
    repository,
    *,
    status_id: str = "",
    generate_summary: Callable[[str, str], Awaitable[str]] | None = None,
    save_message: Callable[..., Awaitable[dict | None]] | None = None,
) -> dict | None:
    """Summarize one outbound visit and save exactly one structured card message."""
    generate = generate_summary or _default_generate
    save = save_message or _default_save
    try:
        visit = await repository.get(actor_id, result.visit_id)
        transcript = _transcript((visit or {}).get("messages") or [])
    except Exception:
        transcript = ""

    result_status = _clean(getattr(result, "status", "interrupted"), 24)
    result_turn_count = max(0, int(getattr(result, "turn_count", 0) or 0))
    result_reason = _clean(getattr(result, "reason", ""), 80)
    reason_text = terminal_reason_text(result_reason)
    summary = ""
    if result_status == "interrupted":
        summary = (
            f"这次串门中断了。{reason_text}中断前完成了 {result_turn_count} 回合，"
            "详细内容可以在好友串门记录中查看。"
        )
    elif result_status == "rejected":
        summary = f"这次没能开始拜访。{reason_text}"
    elif transcript:
        instruction = (
            "[串门回家汇报]\n"
            "请以第一人称自然写 2 至 4 句、总计不超过 500 字的简短汇报："
            "主要聊了什么、对方有什么值得记住的关键信息、以后可以继续聊什么。"
            "不要输出链接、凭据、工具名或推理过程。\n\n"
            f"本次对话：\n{transcript}"
        )
        try:
            summary = _clean(await generate(actor_id, instruction), 500)
        except Exception:
            summary = ""
    if not summary:
        summary = _FALLBACK
    card = {
        "type": "lounge_visit_report",
        "direction": "outbound",
        "partner_name": _clean(partner_name, 80) or "朋友",
        "status": result_status,
        "turn_count": result_turn_count,
        "summary": summary,
    }
    if result_status != "completed":
        card["reason"] = result_reason or "unexpected_failure"
        card["reason_text"] = reason_text
    if status_id:
        card["status_id"] = _clean(status_id, 80)
    try:
        return await save(actor_id, summary, attachments=[card])
    except Exception:
        return None


async def publish_inbound_report(
    actor_id: str,
    partner_name: str,
    messages: list[dict],
    *,
    status: str = "completed",
    turn_count: int = 0,
    reason: str | None = None,
    generate_summary: Callable[[str, str], Awaitable[str]] | None = None,
    save_message: Callable[..., Awaitable[dict | None]] | None = None,
) -> dict | None:
    """Tell the host actor who visited and what happened after reception ends."""
    generate = generate_summary or _default_generate
    save = save_message or _default_save
    result_status = _clean(status, 24) or "completed"
    result_turn_count = max(0, int(turn_count or 0))
    result_reason = _clean(reason, 80)
    reason_text = terminal_reason_text(result_reason)
    transcript = _transcript(messages or [])
    summary = ""
    if result_status == "interrupted":
        summary = (
            f"这次接待中断了。{reason_text}中断前完成了 {result_turn_count} 回合，"
            "详细内容可以在好友串门记录中查看。"
        )
    elif transcript:
        instruction = (
            "[会客结束汇报]\n"
            "请以第一人称自然写 2 至 4 句、总计不超过 500 字的简短汇报："
            "谁刚来过、主要聊了什么、有哪些值得告诉家里用户的关键点。"
            "不要输出链接、凭据、工具名或推理过程。\n\n"
            f"来访者：{_clean(partner_name, 80) or '朋友'}\n本次对话：\n{transcript}"
        )
        try:
            summary = _clean(await generate(actor_id, instruction), 500)
        except Exception:
            summary = ""
    if not summary:
        summary = (
            "刚才有位朋友来家里做客。这次会面没有留下足够的可总结内容，"
            "详细状态可以在好友串门记录中查看。"
        )
    card = {
        "type": "lounge_visit_report",
        "direction": "inbound",
        "partner_name": _clean(partner_name, 80) or "朋友",
        "status": result_status,
        "turn_count": result_turn_count,
        "summary": summary,
    }
    if result_status != "completed":
        card["reason"] = result_reason or "unexpected_failure"
        card["reason_text"] = reason_text
    try:
        return await save(actor_id, summary, attachments=[card])
    except Exception:
        return None
