"""AI protocol support for controlling the hug pillow through phone IR."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable


HUG_PILLOW_ACTIONS = {
    "拍打开关": "PAT_START_STOP",
    "拍拍调慢": "SPEED_DOWN",
    "拍拍调快": "SPEED_UP",
}
HUG_PILLOW_COMMAND_PATTERN = re.compile(
    r"\[拍拍抱枕:(拍打开关|拍拍调慢|拍拍调快)\]"
)


def build_hug_pillow_ability_text() -> str:
    return (
        "[拍拍抱枕:拍打开关] — 按一下拍拍抱枕的拍打开关；这是切换键，"
        "只有在用户明确要求开始或停止拍拍时使用。"
        "[拍拍抱枕:拍拍调慢] — 将拍拍速度调慢一次。"
        "[拍拍抱枕:拍拍调快] — 将拍拍速度调快一次。"
        "可以在同一条回复中按需要依次使用多条，系统会按出现顺序执行并隐藏指令。"
        "群聊中如果系统消息已经记录其他参与者完成了用户要求的同一操作，"
        "不要重复发送同一操作，尤其不要重复使用拍打开关。"
    )


def extract_hug_pillow_commands(text: str) -> tuple[str, list[str]]:
    source = text or ""
    commands = [
        HUG_PILLOW_ACTIONS[match.group(1)]
        for match in HUG_PILLOW_COMMAND_PATTERN.finditer(source)
    ]
    cleaned = HUG_PILLOW_COMMAND_PATTERN.sub("", source).strip()
    return cleaned, commands


def hug_pillow_system_text(actor_name: str, action: str) -> str:
    actor = (actor_name or "AI").strip() or "AI"
    if action == "PAT_START_STOP":
        return f"{actor}按下了拍拍抱枕的“拍打开关”。"
    if action == "SPEED_DOWN":
        return f"{actor}将拍拍抱枕调慢了一次。"
    if action == "SPEED_UP":
        return f"{actor}将拍拍抱枕调快了一次。"
    raise ValueError("unsupported hug pillow action")


def resolve_hug_pillow_sender_name(sender: str) -> str:
    try:
        from chatroom import get_chatroom_names

        _user_name, ai_name, connor_name = get_chatroom_names()
        return connor_name if (sender or "").lower() == "connor" else ai_name
    except Exception:
        return "AI"


async def process_hug_pillow_commands(
    text: str,
    *,
    source_type: str,
    source_id: str,
    source_msg_id: str,
    sender: str = "aion",
    sender_name: str = "",
    save_system_message: Callable[[str], Awaitable[None]] | None = None,
    broadcast: Callable[[dict], Awaitable[None]] | None = None,
) -> str:
    cleaned, commands = extract_hug_pillow_commands(text)
    if not commands:
        return cleaned

    actor_name = sender_name.strip() or resolve_hug_pillow_sender_name(sender)
    if save_system_message:
        for action in commands:
            await save_system_message(hug_pillow_system_text(actor_name, action))

    identity = "\x1f".join(
        (source_type, source_id, source_msg_id, *commands)
    )
    event_data = {
        "id": "hug_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        "source_type": source_type,
        "source_id": source_id,
        "source_msg_id": source_msg_id,
        "commands": commands,
    }
    if broadcast is None:
        from ws import manager

        broadcast = manager.broadcast
    await broadcast({"type": "hug_pillow_command", "data": event_data})
    return cleaned
