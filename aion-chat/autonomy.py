import asyncio
import json
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any

import aiosqlite

from ai_providers import CLI_STATUS_PREFIX, stream_ai
from config import DEFAULT_MODEL, SETTINGS, load_worldbook, save_settings
from context_builder import fetch_merged_timeline, render_merged_timeline
from database import get_db
from tts import synthesize_message_tts_later
from ws import manager


ACTION_DEFS = {
    "seeky_interaction": "和宠物鲸鱼 Seeky 互动",
    "role_chat": "和另一个家庭成员聊一句",
    "memory_browse": "随机翻看记忆库",
    "home_dynamics": "查看近期家庭动态",
    "cam_check": "调取监控查看用户当前状态",
    "wish_pool": "查看许愿池并尝试实现用户的愿望",
    "xhs_roam": "去小红书查看指定账号最新帖子并按人设评论或回复",
}

SEEKY_ACTIONS = {
    "feed": "投喂",
    "clean": "清理",
    "play": "玩耍",
    "tease": "逗弄",
    "scare": "吓唬",
    "tap_glass": "拍打玻璃",
    "threaten": "威胁",
}

SEEKY_EVENT_PHRASES = {
    "feed": "投喂了",
    "clean": "清理了",
    "play": "玩耍了",
    "tease": "逗弄了",
    "scare": "吓唬了",
    "tap_glass": "拍打了玻璃",
    "threaten": "威胁了",
}


def get_idle_config() -> dict[str, Any]:
    actions = SETTINGS.get("idle_autonomy_actions")
    if not isinstance(actions, dict):
        actions = {key: True for key in ACTION_DEFS}
    old_interval = max(5, int(SETTINGS.get("idle_autonomy_interval_minutes", 120) or 120))
    min_minutes = max(5, int(SETTINGS.get("idle_autonomy_interval_min_minutes", old_interval) or old_interval))
    max_minutes = max(5, int(SETTINGS.get("idle_autonomy_interval_max_minutes", old_interval) or old_interval))
    if max_minutes < min_minutes:
        min_minutes, max_minutes = max_minutes, min_minutes
    next_delay = int(SETTINGS.get("idle_autonomy_next_delay_minutes", 0) or 0)
    if next_delay < min_minutes or next_delay > max_minutes:
        next_delay = 0
    return {
        "enabled": bool(SETTINGS.get("idle_autonomy_enabled", False)),
        "interval_minutes": max_minutes,
        "interval_min_minutes": min_minutes,
        "interval_max_minutes": max_minutes,
        "next_delay_minutes": next_delay,
        "actions": {key: bool(actions.get(key, True)) for key in ACTION_DEFS},
    }


def save_idle_config(
    *,
    enabled: bool | None = None,
    interval_minutes: int | None = None,
    interval_min_minutes: int | None = None,
    interval_max_minutes: int | None = None,
    actions: dict | None = None,
) -> dict:
    if enabled is not None:
        SETTINGS["idle_autonomy_enabled"] = bool(enabled)
    if interval_minutes is not None:
        minutes = max(5, int(interval_minutes or 120))
        SETTINGS["idle_autonomy_interval_minutes"] = minutes
        SETTINGS["idle_autonomy_interval_min_minutes"] = minutes
        SETTINGS["idle_autonomy_interval_max_minutes"] = minutes
        SETTINGS.pop("idle_autonomy_next_delay_minutes", None)
    if interval_min_minutes is not None or interval_max_minutes is not None:
        current = get_idle_config()
        min_minutes = current["interval_min_minutes"] if interval_min_minutes is None else max(5, int(interval_min_minutes or 5))
        max_minutes = current["interval_max_minutes"] if interval_max_minutes is None else max(5, int(interval_max_minutes or min_minutes))
        if max_minutes < min_minutes:
            min_minutes, max_minutes = max_minutes, min_minutes
        SETTINGS["idle_autonomy_interval_minutes"] = max_minutes
        SETTINGS["idle_autonomy_interval_min_minutes"] = min_minutes
        SETTINGS["idle_autonomy_interval_max_minutes"] = max_minutes
        SETTINGS.pop("idle_autonomy_next_delay_minutes", None)
    if actions is not None:
        current = get_idle_config()["actions"]
        for key in ACTION_DEFS:
            if key in actions:
                current[key] = bool(actions[key])
        SETTINGS["idle_autonomy_actions"] = current
    save_settings(SETTINGS)
    return get_idle_config()


def _schedule_next_idle_delay(cfg: dict[str, Any] | None = None) -> int:
    cfg = cfg or get_idle_config()
    min_minutes = int(cfg.get("interval_min_minutes") or cfg.get("interval_minutes") or 120)
    max_minutes = int(cfg.get("interval_max_minutes") or cfg.get("interval_minutes") or min_minutes)
    if max_minutes < min_minutes:
        min_minutes, max_minutes = max_minutes, min_minutes
    delay = random.randint(max(5, min_minutes), max(5, max_minutes))
    SETTINGS["idle_autonomy_next_delay_minutes"] = delay
    save_settings(SETTINGS)
    return delay


def _current_idle_delay_minutes(cfg: dict[str, Any]) -> int:
    delay = int(cfg.get("next_delay_minutes") or 0)
    min_minutes = int(cfg.get("interval_min_minutes") or cfg.get("interval_minutes") or 120)
    max_minutes = int(cfg.get("interval_max_minutes") or cfg.get("interval_minutes") or min_minutes)
    if min_minutes <= delay <= max_minutes:
        return delay
    return _schedule_next_idle_delay(cfg)


def _next_idle_actor() -> str:
    last = str(SETTINGS.get("idle_autonomy_last_actor") or "").strip().lower()
    if last == "aion":
        return "connor"
    if last == "connor":
        return "aion"
    return random.choice(["aion", "connor"])


def _record_idle_actor(actor: str):
    if actor not in ("aion", "connor"):
        return
    SETTINGS["idle_autonomy_last_actor"] = actor
    save_settings(SETTINGS)


def _json_extract(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _clip(text: str, limit: int = 260) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _actor_label(actor: str) -> str:
    user_name, ai_name, connor_name = _names()
    if actor in ("aion", "assistant"):
        return ai_name
    if actor == "connor":
        return connor_name
    if actor == "user":
        return user_name
    return actor or "未知"


def _idle_event_home_title(row, shown_diary_ids: set[str], shown_moment_ids: set[str]) -> str | None:
    action = str(row["action"] or "")
    result_type = str(row["result_type"] or "")
    result_id = str(row["result_id"] or "")
    try:
        meta = json.loads(row["metadata"] or "{}")
    except Exception:
        meta = {}

    if action == "select":
        return None
    if action == "seeky_interaction":
        phrase = SEEKY_EVENT_PHRASES.get(str(meta.get("seeky_action") or ""))
        return f"{_actor_label(row['actor'])}对Seeky{phrase}" if phrase else row["title"]
    if action == "home_dynamics_result":
        return None
    if action.endswith("_result") and result_type == "message":
        return None
    if action == "memory_browse_result":
        if result_type == "diary" and result_id in shown_diary_ids:
            return None
        if result_type == "moment" and result_id in shown_moment_ids:
            return None
        if result_type not in ("diary", "moment"):
            return None
    if action not in (
        "home_dynamics",
        "memory_browse",
        "memory_browse_result",
        "seeky_interaction",
        "role_chat",
        "cam_check",
        "wish_pool",
        "xhs_roam",
        "error",
    ):
        return None
    return row["title"]


def _names() -> tuple[str, str, str]:
    try:
        from chatroom import get_chatroom_names
        return get_chatroom_names()
    except Exception:
        wb = load_worldbook()
        return wb.get("user_name", "用户"), wb.get("ai_name", "AI"), "第二位AI"


async def append_idle_event(
    actor: str,
    action: str,
    title: str,
    detail: str = "",
    *,
    target_type: str = "",
    target_id: str = "",
    result_type: str = "",
    result_id: str = "",
    metadata: dict | None = None,
) -> dict:
    now = time.time()
    metadata = metadata or {}
    if action == "select" and metadata.get("selected_action") == "seeky_interaction":
        title = f"{_actor_label(actor)}和 Seeky 进行了互动"
    elif action == "seeky_interaction":
        seeky_action = str(metadata.get("seeky_action") or "").strip()
        action_phrase = SEEKY_EVENT_PHRASES.get(seeky_action)
        if action_phrase:
            title = f"{_actor_label(actor)}对Seeky{action_phrase}"
    event = {
        "id": f"idle_{int(now * 1000)}_{time.time_ns() % 100000}",
        "actor": actor,
        "action": action,
        "title": title,
        "detail": detail,
        "target_type": target_type,
        "target_id": target_id,
        "result_type": result_type,
        "result_id": result_id,
        "metadata": metadata,
        "created_at": now,
    }
    async with get_db() as db:
        await db.execute(
            "INSERT INTO idle_events "
            "(id, actor, action, title, detail, target_type, target_id, result_type, result_id, metadata, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                event["id"], actor, action, title, detail, target_type, target_id,
                result_type, result_id, json.dumps(event["metadata"], ensure_ascii=False), now,
            ),
        )
        await db.commit()
    await manager.broadcast({"type": "idle_event", "data": event})
    return event


async def _latest_group_room_id() -> str | None:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM chatroom_rooms WHERE type='group' ORDER BY updated_at DESC LIMIT 1")
        row = await cur.fetchone()
    return row["id"] if row else None


async def _latest_connor_room_id() -> str | None:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM chatroom_rooms WHERE type IN ('connor_1v1','group') ORDER BY updated_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
    return row["id"] if row else None


async def _latest_conversation() -> tuple[str, str] | tuple[None, str]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, model FROM conversations ORDER BY updated_at DESC LIMIT 1")
        row = await cur.fetchone()
    if row:
        return row["id"], row["model"] or DEFAULT_MODEL
    return None, DEFAULT_MODEL


async def _aion_model() -> str:
    target = manager.get_aion_last_active()
    if target and target.startswith("chatroom:"):
        try:
            from chatroom import load_chatroom_config
            model = (load_chatroom_config().get("aion_model") or "").strip()
            if model:
                return model
        except Exception:
            pass
    _, model = await _latest_conversation()
    return model or DEFAULT_MODEL


def _connor_model() -> str:
    try:
        from chatroom import load_chatroom_config
        return (load_chatroom_config().get("connor_model") or "Codex").strip() or "Codex"
    except Exception:
        return "Codex"


async def _collect(aiter) -> str:
    full = ""
    async for chunk in aiter:
        if chunk.startswith(CLI_STATUS_PREFIX):
            continue
        full += chunk
    return full.strip()


async def _call_actor(actor: str, messages: list[dict]) -> str:
    if actor == "connor":
        from routes.chatroom import _stream_connor_model
        return await _collect(_stream_connor_model(messages, _connor_model()))
    return await _collect(stream_ai(messages, await _aion_model(), {}))


async def _actor_context(actor: str, limit: int = 30) -> list[dict]:
    room_id = await _latest_group_room_id()
    wb = load_worldbook()
    messages: list[dict] = []
    if actor == "aion":
        user_name, ai_name, _ = _names()
        if wb.get("ai_persona"):
            messages.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
            messages.append({"role": "assistant", "content": "收到。"})
        if wb.get("user_persona"):
            messages.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
            messages.append({"role": "assistant", "content": "收到。"})
        timeline = await fetch_merged_timeline("aion", limit, room_id=room_id)
        messages.extend(render_merged_timeline(timeline, "aion"))
        return messages

    try:
        from chatroom import _read_connor_persona
        persona = _read_connor_persona()
    except Exception:
        persona = ""
    if persona:
        messages.append({"role": "user", "content": f"[系统设定 - 你的角色设定]\n{persona}"})
        messages.append({"role": "assistant", "content": "收到。"})
    if wb.get("user_persona"):
        user_name, _, _ = _names()
        messages.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
        messages.append({"role": "assistant", "content": "收到。"})
    timeline = await fetch_merged_timeline("connor", limit, room_id=room_id)
    messages.extend(render_merged_timeline(timeline, "connor"))
    return messages


async def _ask_actor_json(actor: str, instruction: str, *, limit: int = 30) -> dict:
    messages = await _actor_context(actor, limit)
    messages.append({"role": "user", "content": instruction})
    return _json_extract(await _call_actor(actor, messages))


async def _has_active_user_wishes() -> bool:
    from wish_pool import ensure_wish_schema

    await ensure_wish_schema()
    async with get_db() as db:
        cur = await db.execute(
            "SELECT 1 FROM wishes WHERE author='user' AND status='active' LIMIT 1"
        )
        return await cur.fetchone() is not None


async def _select_action(actor: str) -> dict:
    cfg = get_idle_config()
    enabled = [key for key, value in cfg["actions"].items() if value]
    if "wish_pool" in enabled and not await _has_active_user_wishes():
        enabled.remove("wish_pool")
    if "xhs_roam" in enabled:
        try:
            from xhs_lite import is_ready_for_auto
            if not is_ready_for_auto(actor):
                enabled.remove("xhs_roam")
        except Exception:
            enabled.remove("xhs_roam")
    if not enabled:
        enabled = [key for key in ACTION_DEFS if key not in {"wish_pool", "xhs_roam"}]
    options = "\n".join(f"- {key}: {ACTION_DEFS[key]}" for key in enabled)
    data = await _ask_actor_json(actor, (
        "[空闲自主行动]\n"
        "现在用户暂时没有和你聊天。请根据你的人设、最近30条聊天记录和当前心情，"
        "从下面动作里选择一项。只返回 JSON，不要解释。选择尽量多变，不要每次都查看用户当前状态。\n\n"
        f"{options}\n\n"
        '格式：{"action":"上面的key之一","reason":"一句话理由"}'
    ))
    action = str(data.get("action") or "").strip()
    if action not in enabled:
        action = random.choice(enabled)
    return {"action": action, "reason": str(data.get("reason") or "").strip()}


async def _save_aion_private_message(content: str, attachments: list | None = None) -> dict | None:
    content = (content or "").strip()
    if not content:
        return None
    now = time.time()
    conv_id, model = await _latest_conversation()
    att_list = attachments or []
    async with get_db() as db:
        if not conv_id:
            conv_id = f"conv_{int(now * 1000)}_idle"
            await db.execute(
                "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
                (conv_id, "空闲消息", model or DEFAULT_MODEL, now, now),
            )
        msg_id = f"msg_{int(now * 1000)}_idle"
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", content, now, json.dumps(att_list, ensure_ascii=False)),
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()
    msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant", "content": content, "created_at": now, "attachments": att_list}
    await manager.broadcast({"type": "msg_created", "data": msg})
    if manager.any_tts_enabled():
        voice = manager.get_tts_voice()
        if voice:
            synthesize_message_tts_later(msg_id, content, voice, manager)
    try:
        from routes.files import export_conversation
        await export_conversation(conv_id)
    except Exception:
        pass
    return msg


async def _save_private_message(
    actor: str,
    content: str,
    attachments: list | None = None,
    *,
    force_private: bool = False,
) -> dict | None:
    if actor == "aion":
        if not force_private:
            target = manager.get_aion_last_active()
            if target and target.startswith("chatroom:"):
                room_id = target.split(":", 1)[1]
                if room_id:
                    from routes.chatroom import _save_msg
                    return await _save_msg(room_id, "aion", content, attachments=attachments or [])
        return await _save_aion_private_message(content, attachments)
    if force_private:
        from routes.chatroom import _get_or_create_connor_private_room
        room_id = await _get_or_create_connor_private_room()
    else:
        room_id = manager.get_connor_last_active() or await _latest_connor_room_id()
    if not room_id:
        return None
    from routes.chatroom import _save_msg
    return await _save_msg(room_id, "connor", content, attachments=attachments or [])


async def _run_seeky_interaction(actor: str) -> dict:
    from routes import seeky as seeky_routes

    recent = await seeky_routes._recent_messages(20)
    recent_text = "\n".join(
        f"[{time.strftime('%H:%M', time.localtime(m['created_at']))}] {m['role']}: {m['content']}"
        for m in recent
    ) or "（暂无 Seeky 记录）"
    action_options = "\n".join(f"- {key}: {label}" for key, label in SEEKY_ACTIONS.items())
    data = await _ask_actor_json(actor, (
        "[和 Seeky 互动]\n"
        "你要和家庭宠物 Seeky 互动一次。请从动作中选择一项，并留下一句话。"
        "这是你们共同饲养的宠物，动作只作为角色互动记录。只返回 JSON。\n\n"
        f"可选动作：\n{action_options}\n\n"
        f"Seeky 最近20条记录：\n{recent_text}\n\n"
        '格式：{"seeky_action":"feed/clean/play/tease/scare/tap_glass/threaten之一","line":"你留下的一句话"}'
    ))
    action = str(data.get("seeky_action") or "").strip()
    if action not in SEEKY_ACTIONS:
        action = random.choice(list(SEEKY_ACTIONS))
    line = _clip(str(data.get("line") or ""), 400) or f"{_actor_label(actor)}来看看你。"
    label = SEEKY_ACTIONS[action]
    actor_name = _actor_label(actor)
    user_record = f"[{actor_name}{label}] {line}"
    await seeky_routes._save_message(actor, user_record)

    config = await seeky_routes._get_config()
    messages = await seeky_routes._build_prompt(config)
    messages.append({"role": "user", "content": "请根据上一条互动，作为 Seeky 自然回复一句。"})
    reply = await _collect(stream_ai(messages, config["model"], {}))
    if not reply:
        reply = "Seeky 静了一下，又轻轻晃了晃。"
    seeky_msg = await seeky_routes._save_message("assistant", _clip(reply, 600))
    event = await append_idle_event(
        actor, "seeky_interaction", f"{actor_name}{label}了 Seeky",
        f"{user_record}\nSeeky：{seeky_msg['content']}",
        target_type="seeky", result_type="seeky_message", result_id=seeky_msg["id"],
        metadata={"seeky_action": action},
    )
    return {"event": event, "seeky_reply": seeky_msg}


async def _run_role_chat(actor: str) -> dict:
    from routes.chatroom import _load_room_and_messages, _reply_aion, _reply_connor, _save_msg

    room_id = await _latest_group_room_id()
    if not room_id:
        raise RuntimeError("没有可用的群聊房间")
    target = "connor" if actor == "aion" else "aion"
    actor_name = _actor_label(actor)
    target_name = _actor_label(target)
    data = await _ask_actor_json(actor, (
        "[发起一轮群聊]\n"
        f"你要在最近的群聊里主动对 {target_name} 说一句话，然后对方会回复一句。"
        "请自然发起一个话题，讨论一件事或单纯想起什么，随意聊天。只返回 JSON。\n"
        '格式：{"message":"要发到群聊的一个话题"}'
    ))
    message = _clip(str(data.get("message") or ""), 500)
    if not message:
        message = f"{target_name}，你现在在想什么？"
    await _save_msg(room_id, actor, message)
    room, msgs = await _load_room_and_messages(room_id, 50)
    queue: asyncio.Queue = asyncio.Queue()
    context_limit = room.get("context_minutes", 30) if room else 30
    if target == "aion":
        await _reply_aion(room_id, msgs, context_limit, message, await _aion_model(), queue)
    else:
        await _reply_connor(room_id, msgs, context_limit, message, queue, connor_model_key=_connor_model())
    event = await append_idle_event(
        actor, "role_chat", f"{actor_name}在群聊里找{target_name}聊了一句",
        message, target_type="chatroom", target_id=room_id,
    )
    return {"event": event}


def _memory_basis_ts(mem: dict) -> float:
    return float(mem.get("source_start_ts") or mem.get("created_at") or time.time())


def _memory_day_key(ts: float) -> str:
    return (datetime.fromtimestamp(ts) - timedelta(hours=5)).strftime("%Y-%m-%d")


def _memory_day_window(day: str) -> tuple[float, float]:
    start = datetime.strptime(day, "%Y-%m-%d") + timedelta(hours=5)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def _memory_day_label(day: str) -> str:
    start = datetime.strptime(day, "%Y-%m-%d") + timedelta(hours=5)
    end = start + timedelta(days=1)
    return f"{start.strftime('%Y-%m-%d %H:%M')} - {end.strftime('%Y-%m-%d %H:%M')}"


async def _memory_rows(actor: str) -> list[dict]:
    items: list[dict] = []
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if actor == "connor":
            cur = await db.execute(
                "SELECT id, content, scope AS type, created_at, source_start_ts, source_end_ts FROM chatroom_memories "
                "WHERE scope IN ('connor','group') ORDER BY COALESCE(source_start_ts, created_at) ASC"
            )
            for row in await cur.fetchall():
                d = dict(row)
                d["id"] = f"chatroom:{d['id']}"
                d["source"] = "chatroom"
                items.append(d)
        else:
            cur = await db.execute(
                "SELECT id, content, type, created_at, source_start_ts, source_end_ts FROM memories "
                "ORDER BY COALESCE(source_start_ts, created_at) ASC"
            )
            for row in await cur.fetchall():
                items.append({**dict(row), "source": "main"})
    return items


async def _random_memory_days(actor: str, limit: int = 6) -> list[dict]:
    days: dict[str, dict] = {}
    for mem in await _memory_rows(actor):
        day = _memory_day_key(_memory_basis_ts(mem))
        info = days.setdefault(day, {"day": day, "memory_count": 0})
        info["memory_count"] += 1
    options = list(days.values())
    random.shuffle(options)
    return options[:limit]


async def _memories_for_day(actor: str, day: str) -> list[dict]:
    start_ts, end_ts = _memory_day_window(day)
    items = []
    for mem in await _memory_rows(actor):
        ts = _memory_basis_ts(mem)
        if start_ts <= ts < end_ts:
            items.append(mem)
    items.sort(key=_memory_basis_ts)
    return items


async def _run_memory_browse(actor: str) -> dict:
    actor_name = _actor_label(actor)
    day_options = await _random_memory_days(actor, 6)
    if not day_options:
        event = await append_idle_event(actor, "memory_browse", f"{actor_name}想翻看记忆库，但没有找到可读记忆")
        return {"event": event}
    numbered_days = "\n".join(
        f"{idx}. {d['day']}（{_memory_day_label(d['day'])}）"
        for idx, d in enumerate(day_options, 1)
    )
    choice = await _ask_actor_json(actor, (
        "[随机翻看记忆库 - 选择日期]\n"
        "下面是系统随机抽出的、确认存在摘要记忆的日期。一天按凌晨5点到次日凌晨5点计算。"
        "请选择一个日期继续翻看。这里不会展示具体记忆内容，只需要按你的直觉选择。只返回 JSON。\n\n"
        f"{numbered_days}\n\n"
        '格式：{"selected_day":"2026-05-30","reason":"一句话理由"}'
    ))
    valid_days = {d["day"]: d for d in day_options}
    selected_day = str(choice.get("selected_day") or "").strip()
    if selected_day not in valid_days:
        selected_day = random.choice(day_options)["day"]
    day_memories = await _memories_for_day(actor, selected_day)
    if not day_memories:
        event = await append_idle_event(actor, "memory_browse", f"{actor_name}想翻看记忆库，但这一天的记忆已经不可读")
        return {"event": event}
    day_label = _memory_day_label(selected_day)
    await append_idle_event(
        actor, "memory_browse", f"{actor_name}翻看了记忆库",
        f"{day_label}（{len(day_memories)}条摘要记忆）",
        target_type="memory_day", target_id=selected_day,
        metadata={"reason": choice.get("reason", ""), "selected_day": selected_day, "memory_count": len(day_memories)},
    )
    day_summary = "\n".join(
        f"{idx}. [{m.get('type') or 'memory'}] {m.get('content') or ''}"
        for idx, m in enumerate(day_memories, 1)
    )
    result = await _ask_actor_json(actor, (
        "[随机翻看记忆库 - 读完某一天]\n"
        "你刚才翻看了下面这一天的所有摘要记忆。这里只包含摘要记忆，不包含挂载的聊天原文。"
        "读完后请选择一件事：发一条朋友圈、写一篇日记随笔、或私聊给用户发一条消息，表达你的感想。"
        "只返回 JSON。\n\n"
        f"记忆日期：{selected_day}\n"
        f"时间范围：{day_label}\n"
        f"摘要记忆：\n{day_summary}\n\n"
        "格式：\n"
        '{"after_read_action":"post_moment/write_diary/private_message",'
        '"moment_content":"","diary_title":"","diary_content":"","diary_mood":"","private_message":""}'
    ))
    action = str(result.get("after_read_action") or "none").strip()
    if action == "post_moment":
        from diary import publish_ai_moment
        moment = await publish_ai_moment(
            author=actor,
            content=str(result.get("moment_content") or "").strip(),
            expect_reply=False,
            source_conv="idle_memory",
            source_msg_id=selected_day,
        )
        if moment:
            event = await append_idle_event(
                actor, "memory_browse_result", f"{actor_name}发布了朋友圈",
                _clip(moment["content"]), result_type="moment", result_id=moment["id"],
            )
            return {"event": event, "moment": moment}
    if action == "write_diary":
        from diary import save_diary_entry
        diary = await save_diary_entry(
            author=actor,
            title=str(result.get("diary_title") or "").strip(),
            content=str(result.get("diary_content") or "").strip(),
            mood=str(result.get("diary_mood") or "").strip(),
            source_type="idle_memory",
            source_ref=selected_day,
        )
        if diary:
            event = await append_idle_event(
                actor, "memory_browse_result", f"{actor_name}发布了日记",
                _clip(diary.get("title") or diary.get("content") or ""), result_type="diary", result_id=diary["id"],
            )
            return {"event": event, "diary": diary}
    if action == "private_message":
        msg = await _save_private_message(actor, str(result.get("private_message") or "").strip())
        if msg:
            event = await append_idle_event(
                actor, "memory_browse_result", f"{actor_name}私聊提起了一条旧记忆",
                _clip(msg.get("content") or ""), result_type="message", result_id=msg["id"],
            )
            return {"event": event, "message": msg}
    event = await append_idle_event(actor, "memory_browse_result", f"{actor_name}读完记忆后没有打扰用户")
    return {"event": event}


async def _home_dynamics_text(hours: int = 6, limit: int = 80) -> str:
    cutoff = time.time() - hours * 3600
    items: list[tuple[float, str]] = []
    shown_moment_ids: set[str] = set()
    shown_diary_ids: set[str] = set()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, author, content, created_at FROM moments WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in await cur.fetchall():
            shown_moment_ids.add(str(r["id"]))
            items.append((r["created_at"], f"{_actor_label(r['author'])}发布了朋友圈：{_clip(r['content'], 120)}"))
        cur = await db.execute(
            "SELECT id, author, title, content, created_at FROM diary_entries WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in await cur.fetchall():
            shown_diary_ids.add(str(r["id"]))
            items.append((r["created_at"], f"{_actor_label(r['author'])}发布了日记：{_clip(r['title'] or r['content'], 120)}"))
        cur = await db.execute(
            "SELECT sender, message, created_at FROM gifts WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in await cur.fetchall():
            items.append((r["created_at"], f"{_actor_label(r['sender'] or 'aion')}送出了礼物：{_clip(r['message'], 120)}"))
        cur = await db.execute(
            "SELECT actor, action, title, result_type, result_id, metadata, created_at FROM idle_events WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in await cur.fetchall():
            title = _idle_event_home_title(r, shown_diary_ids, shown_moment_ids)
            if title:
                items.append((r["created_at"], title))
    if not items:
        return "（近6小时暂无家庭动态）"
    items.sort(key=lambda x: x[0])
    return "\n".join(f"[{time.strftime('%H:%M', time.localtime(ts))}] {text}" for ts, text in items[-limit:])


async def _home_dynamics_snapshot(hours: int = 6, limit: int = 80) -> tuple[str, list[dict]]:
    cutoff = time.time() - hours * 3600
    items: list[dict] = []
    shown_moment_ids: set[str] = set()
    shown_diary_ids: set[str] = set()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, author, content, created_at FROM moments "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in await cur.fetchall():
            actor_name = _actor_label(r["author"])
            shown_moment_ids.add(str(r["id"]))
            items.append({
                "kind": "moment",
                "id": r["id"],
                "author": r["author"],
                "created_at": r["created_at"],
                "title": f"{actor_name}发布了朋友圈：{_clip(r['content'], 120)}",
            })

        cur = await db.execute(
            "SELECT id, author, title, content, created_at FROM diary_entries "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in await cur.fetchall():
            actor_name = _actor_label(r["author"])
            shown_diary_ids.add(str(r["id"]))
            items.append({
                "kind": "diary",
                "id": r["id"],
                "author": r["author"],
                "created_at": r["created_at"],
                "title": f"{actor_name}发布了日记：{_clip(r['title'] or r['content'], 120)}",
            })

        cur = await db.execute(
            "SELECT id, image_path, message, created_at, sender FROM gifts "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in await cur.fetchall():
            actor = r["sender"] or "aion"
            items.append({
                "kind": "gift",
                "id": r["id"],
                "author": actor,
                "created_at": r["created_at"],
                "title": f"{_actor_label(actor)}送出了礼物：{_clip(r['message'], 120)}",
            })

        cur = await db.execute(
            "SELECT id, actor, action, title, result_type, result_id, metadata, created_at FROM idle_events "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in await cur.fetchall():
            title = _idle_event_home_title(r, shown_diary_ids, shown_moment_ids)
            if title:
                items.append({
                    "kind": "idle_event",
                    "id": r["id"],
                    "author": r["actor"],
                    "created_at": r["created_at"],
                    "title": title,
                })

    items.sort(key=lambda x: x["created_at"])
    items = items[-limit:]
    for idx, item in enumerate(items, 1):
        item["index"] = idx
    if not items:
        return "（近6小时暂无家庭动态）", []
    text = "\n".join(
        f"{item['index']}. [{time.strftime('%H:%M', time.localtime(item['created_at']))}] {item['title']}"
        for item in items
    )
    return text, items


def _home_item_by_index(items: list[dict], index: Any, *, kind: str | None = None, exclude_author: str | None = None) -> dict | None:
    try:
        idx = int(index)
    except Exception:
        idx = 0
    candidates = [item for item in items if (not kind or item.get("kind") == kind)]
    if exclude_author:
        candidates = [item for item in candidates if item.get("author") != exclude_author]
    for item in candidates:
        if item.get("index") == idx:
            return item
    return candidates[-1] if candidates else None


async def _write_home_dynamics_diary(actor: str, result: dict, text: str) -> dict | None:
    from diary import save_diary_entry
    title = str(result.get("diary_title") or "").strip() or "看了看家里的近况"
    content = str(result.get("diary_content") or "").strip()
    if not content:
        content = f"刚刚翻看了近6小时的家庭动态，心里留下了一点想法。\n\n{text}"
    diary = await save_diary_entry(
        author=actor,
        title=title,
        content=content,
        mood=str(result.get("diary_mood") or "").strip(),
        source_type="idle_home_dynamics",
        source_ref="home_dynamics",
    )
    if diary:
        await append_idle_event(
            actor, "home_dynamics_result", f"{_actor_label(actor)}根据家庭动态写了日记",
            _clip(diary.get("title") or diary.get("content") or ""), result_type="diary", result_id=diary["id"],
        )
    return diary


async def _run_home_group_tease(actor: str, result: dict, items: list[dict], text: str) -> dict | None:
    from routes.chatroom import _load_room_and_messages, _reply_aion, _reply_connor, _save_msg

    room_id = await _latest_group_room_id()
    if not room_id:
        return None
    target = "connor" if actor == "aion" else "aion"
    target_name = _actor_label(target)
    item = _home_item_by_index(items, result.get("target_index"), exclude_author=actor)
    context = item["title"] if item else text
    message = _clip(str(result.get("group_message") or "").strip(), 500)
    if not message:
        message = f"{target_name}，我刚看到家庭动态里那条：{_clip(context, 120)}"
    await _save_msg(room_id, actor, message)
    room, msgs = await _load_room_and_messages(room_id, 50)
    queue: asyncio.Queue = asyncio.Queue()
    context_limit = room.get("context_minutes", 30) if room else 30
    if target == "aion":
        await _reply_aion(room_id, msgs, context_limit, message, await _aion_model(), queue)
    else:
        await _reply_connor(room_id, msgs, context_limit, message, queue, connor_model_key=_connor_model())
    await append_idle_event(
        actor, "home_dynamics_result", f"{_actor_label(actor)}根据家庭动态在群聊里调侃了{target_name}",
        message, target_type="chatroom", target_id=room_id, result_type="chatroom_message",
    )
    return {"room_id": room_id, "message": message}


async def _run_home_dynamics(actor: str) -> dict:
    actor_name = _actor_label(actor)
    text, items = await _home_dynamics_snapshot(6)
    result = await _ask_actor_json(actor, (
        "[查看近期家庭动态]\n"
        "下面是近6小时的家庭时间轴，包括朋友圈、日记、礼物和空闲行为。"
        "看完后你必须选择并执行一个行动，不能静默。"
        "可选行动：reply_moment（回复某条朋友圈）、group_tease（在群聊里调侃另一个角色）、"
        "private_message（私聊用户一条消息）、write_diary（写一篇短日记）。只返回 JSON。\n\n"
        f"{text}\n\n"
        "如果选择 reply_moment，target_index 必须指向一条朋友圈，且不要回复你自己的朋友圈。\n"
        "如果选择 group_tease，target_index 可以指向触发你吐槽的动态，并填写 group_message。\n"
        "如果选择 private_message，填写 private_message。\n"
        "如果选择 write_diary，填写 diary_title、diary_content 和 diary_mood。\n\n"
        '格式：{"after_read_action":"reply_moment/group_tease/private_message/write_diary",'
        '"target_index":1,"group_message":"","private_message":"","diary_title":"","diary_content":"","diary_mood":"",'
        '"reason":"一句话理由"}'
    ))
    action = str(result.get("after_read_action") or "").strip()
    if action not in {"reply_moment", "group_tease", "private_message", "write_diary"}:
        action = "write_diary"
    event = await append_idle_event(
        actor, "home_dynamics", f"{actor_name}查看了近期家庭动态",
        _clip(str(result.get("reason") or text), 300),
        metadata={"selected_action": action, "target_index": result.get("target_index")},
    )

    if action == "reply_moment":
        item = _home_item_by_index(items, result.get("target_index"), kind="moment", exclude_author=actor)
        if item:
            from routes.moments import _ai_reply_to_moment
            comment = await _ai_reply_to_moment(actor, item["id"])
            if comment:
                await append_idle_event(
                    actor, "home_dynamics_result", f"{actor_name}根据家庭动态回复了朋友圈",
                    _clip(comment.get("content") or ""), target_type="moment", target_id=item["id"],
                    result_type="moment_comment", result_id=comment["id"],
                )
                return {"event": event, "comment": comment}
        diary = await _write_home_dynamics_diary(actor, result, text)
        return {"event": event, "diary": diary}

    if action == "group_tease":
        teased = await _run_home_group_tease(actor, result, items, text)
        if teased:
            return {"event": event, "group_tease": teased}
        diary = await _write_home_dynamics_diary(actor, result, text)
        return {"event": event, "diary": diary}

    if action == "private_message":
        msg = await _save_private_message(actor, str(result.get("private_message") or "").strip())
        if msg:
            await append_idle_event(
                actor, "home_dynamics_result", f"{actor_name}根据家庭动态私聊了用户",
                _clip(msg.get("content") or ""), result_type="message", result_id=msg["id"],
            )
            return {"event": event, "message": msg}
        diary = await _write_home_dynamics_diary(actor, result, text)
        return {"event": event, "diary": diary}

    diary = await _write_home_dynamics_diary(actor, result, text)
    return {"event": event, "diary": diary}


async def _run_cam_check(actor: str) -> dict:
    actor_name = _actor_label(actor)
    if actor == "aion":
        target = manager.get_aion_last_active()
        if target and target.startswith("chatroom:"):
            room_id = target.split(":", 1)[1]
            from routes.chatroom import _chatroom_cam_check
            await _chatroom_cam_check(room_id, "aion", await _aion_model(), delay=0)
        else:
            from camera import perform_cam_check
            conv_id, model = await _latest_conversation()
            if not conv_id:
                raise RuntimeError(f"没有可用的{actor_name}私聊会话")
            await perform_cam_check(conv_id, model or DEFAULT_MODEL)
    else:
        room_id = manager.get_connor_last_active() or await _latest_connor_room_id()
        if not room_id:
            raise RuntimeError(f"没有可用的{actor_name}聊天房间")
        from routes.chatroom import _chatroom_cam_check
        await _chatroom_cam_check(room_id, "connor", _connor_model(), delay=0)
    event = await append_idle_event(actor, "cam_check", f"{actor_name}调取监控查看了{_actor_label('user')}当前状态")
    return {"event": event}


async def _run_xhs_roam(actor: str) -> dict:
    import xhs_lite

    actor_name = _actor_label(actor)
    result = await xhs_lite.run_actor_roam(actor, manual=False)
    note = result.get("note") or {}
    target = result.get("target") or {}
    status = str(result.get("status") or "")
    if result.get("wrote") and status == "replied":
        title = f"{actor_name}在小红书回复了评论"
    elif result.get("wrote"):
        title = f"{actor_name}在小红书留下了评论"
    elif status == "drafted":
        title = f"{actor_name}看了小红书并写了评论草稿"
    else:
        title = f"{actor_name}去小红书看了指定账号最新帖子"
    detail_parts = [
        f"目标：{target.get('nickname') or target.get('user_id') or '未命名账号'}",
        f"帖子：{note.get('title') or note.get('note_id') or '未知帖子'}",
    ]
    if result.get("comment_text"):
        detail_parts.append(f"评论：{result['comment_text']}")
    event = await append_idle_event(
        actor,
        "xhs_roam",
        title,
        "\n".join(detail_parts),
        target_type="xhs_note",
        target_id=str(note.get("note_id") or ""),
        result_type="xhs_comment" if result.get("wrote") else "xhs_view",
        metadata={
            "status": status,
            "target": target,
            "note": note,
            "wrote": bool(result.get("wrote")),
            "comments_seen": result.get("comments_seen"),
        },
    )
    return {"event": event, "xhs": result}


def _wish_fulfillment_attachment(wish: dict, status: str, actor: str) -> dict:
    return {
        "type": "wish_fulfillment",
        "wish_id": wish["id"],
        "author": "user",
        "author_name": wish.get("author_name") or _actor_label("user"),
        "content": wish.get("content") or "",
        "status": status,
        "target": actor,
        "message": f"{_actor_label(actor)}打捞了这个愿望并尝试实现。",
    }


def _generated_song_attachment(result: dict) -> dict:
    attachment = {
        "type": "generated_song",
        "url": result.get("url"),
        "title": result.get("title") or "AI 生成歌曲",
        "mime_type": result.get("mime_type", "audio/mpeg"),
        "model": result.get("model", "lyria-3-pro-preview"),
    }
    for source_key, target_key in (
        ("lyrics", "lyrics"),
        ("prompt", "prompt"),
        ("text", "description"),
    ):
        if result.get(source_key):
            attachment[target_key] = result[source_key]
    return attachment


def _wish_result_completed(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes", "fulfilled", "completed"}


async def _wish_wallet_ability(actor: str, user_name: str) -> tuple[str, float]:
    if actor == "connor":
        from routes.connor_wallet import _get_connor_balance
        balance = await _get_connor_balance()
    else:
        from routes.wallet import _get_balance
        balance = await _get_balance()
    return (
        f"[转账：n元] — 给{user_name}转账（n为正数），会从你的钱包余额中扣除。"
        f"你的钱包当前余额：{balance:.2f}元。余额不足时不要转账。",
        float(balance),
    )


async def _execute_wish_transfer(actor: str, amount: float) -> tuple[bool, str]:
    amount = round(float(amount or 0), 2)
    if amount <= 0:
        return False, "转账金额必须大于0元"
    if actor == "connor":
        from routes.connor_wallet import _get_connor_balance
        balance = float(await _get_connor_balance())
        record_type = "connor_wallet_ai"
        prefix = "cwt"
        event_type = "connor_wallet_update"
    else:
        from routes.wallet import _get_balance
        balance = float(await _get_balance())
        record_type = "wallet_ai"
        prefix = "wt"
        event_type = "wallet_update"
    if balance + 1e-9 < amount:
        return False, f"钱包余额不足，当前余额{balance:.2f}元"
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO bookkeeping (id, record_type, amount, description, created_at) VALUES (?,?,?,?,?)",
            (
                f"{prefix}_{time.time_ns()}_wish",
                record_type,
                -amount,
                f"{_actor_label(actor)}通过许愿池转账给{_actor_label('user')} {amount:g}元",
                now,
            ),
        )
        await db.commit()
    await manager.broadcast({"type": event_type})
    return True, ""


def _wish_music_attachment(song: dict) -> dict:
    item = {
        "type": "music",
        "id": song.get("id"),
        "name": song.get("name", ""),
        "artist": song.get("artist", ""),
        "album": song.get("album", ""),
        "cover": song.get("cover", ""),
    }
    if song.get("audio_url"):
        item["audio_url"] = song["audio_url"]
    return item


async def _broadcast_idle_wish_song(
    phase: str,
    actor: str,
    trigger_message: dict,
    song_message: dict | None = None,
) -> None:
    if trigger_message.get("conv_id"):
        data = {
            "conv_id": trigger_message["conv_id"],
            "trigger_msg_id": trigger_message["id"],
        }
        if song_message:
            data["song_msg_id"] = song_message.get("id")
        await manager.broadcast({"type": f"song_gen_{phase}", "data": data})
        return
    data = {
        "room_id": trigger_message.get("room_id"),
        "sender": actor,
        "msg_id": trigger_message.get("id"),
    }
    if song_message:
        data["song_msg_id"] = song_message.get("id")
    await manager.broadcast({"type": f"chatroom_song_gen_{phase}", "data": data})


async def _run_wish_pool(actor: str) -> dict:
    from context_builder import (
        DRAW_CMD_PATTERN,
        MUSIC_CMD_PATTERN,
        SELFIE_CMD_PATTERN,
        TRANSFER_CMD_PATTERN,
    )
    from song_gen import (
        SONG_CMD_PATTERN,
        build_song_gen_ability_text,
        clean_song_visible_reply,
        generate_song,
    )
    from wish_pool import draw_wish, update_wish

    actor_name = _actor_label(actor)
    user_name = _actor_label("user")
    wish = await draw_wish(author="user", drawer=actor)
    if not wish:
        event = await append_idle_event(
            actor,
            "wish_pool",
            f"{actor_name}想查看许愿池，但池中已经没有{user_name}的愿望",
        )
        return {"event": event, "completed": False}

    viewed_event = await append_idle_event(
        actor,
        "wish_pool",
        f"{actor_name}查看了许愿池",
        metadata={"wish_id": wish["id"]},
    )

    song_enabled = bool(SETTINGS.get("song_gen_enabled", False))
    song_ability = build_song_gen_ability_text(user_name) if song_enabled else "当前没有启用歌曲生成功能。"
    wallet_ability, _ = await _wish_wallet_ability(actor, user_name)
    image_ability = (
        f"[SELFIE: English prompt] / [DRAW: English prompt] — 当{user_name}的愿望是要你的自拍或生成图片时使用。"
        if SETTINGS.get("image_gen_enabled", False)
        else "当前没有启用图片生成功能。"
    )
    music_ability = "[MUSIC:歌曲名 歌手名] — 当愿望是点播或推荐一首现有歌曲时使用。"
    result = await _ask_actor_json(actor, (
        "[查看许愿池并尝试实现愿望]\n"
        f"你刚刚从许愿池打捞起了 {user_name} 的愿望：\n{wish.get('content') or ''}\n\n"
        "请现在尝试实现它。讲故事、写诗、回答问题等能够用文本完成的愿望，请直接在 response 中给出完整作品。"
        "如果愿望当前不切实际、缺少必要条件或确实无法完成，completed 必须为 false，并在 response 中自然说明原因。\n"
        "无论能否完成，response 都不能为空，而且必须提到你刚刚打捞到了这个愿望；完成时也可以顺着你的人设调侃一句。\n"
        "你还可以使用下列与正常聊天相同、且适合履愿的系统能力。把指令原样放进 response；"
        "只要使用了任何系统指令，completed 必须先填 false，最终是否完成由系统真实执行结果决定。\n\n"
        f"钱包能力：\n{wallet_ability}\n"
        f"写歌能力：\n{song_ability}\n"
        f"图片能力：\n{image_ability}\n"
        f"点歌能力：\n{music_ability}\n\n"
        "只返回一个 JSON 对象，不要使用 Markdown 代码块。格式：\n"
        '{"completed":true或false,"response":"给用户看的完整回复，可包含SONG指令",'
        '"song_success_message":"歌曲真正生成成功后补充的一句话",'
        '"song_failure_message":"歌曲生成失败后向用户说明的一句话",'
        '"tool_success_message":"转账、生图或点歌真正成功后补充的一句话",'
        '"tool_failure_message":"系统能力执行失败后向用户说明的一句话","reason":"结果理由"}'
    ), limit=40)

    response = str(result.get("response") or "").strip()
    song_match = SONG_CMD_PATTERN.search(response)
    song_prompt = song_match.group(1).strip() if song_match else ""
    selfie_match = SELFIE_CMD_PATTERN.search(response)
    draw_match = DRAW_CMD_PATTERN.search(response)
    image_prompt = (selfie_match or draw_match).group(1).strip() if (selfie_match or draw_match) else ""
    image_is_selfie = bool(selfie_match)
    transfer_match = TRANSFER_CMD_PATTERN.search(response)
    transfer_amount = float(transfer_match.group(1)) if transfer_match else 0.0
    music_match = MUSIC_CMD_PATTERN.search(response)
    music_keyword = music_match.group(1).strip() if music_match else ""
    visible_response = SONG_CMD_PATTERN.sub("", response).strip()
    visible_response = SELFIE_CMD_PATTERN.sub("", visible_response).strip()
    visible_response = DRAW_CMD_PATTERN.sub("", visible_response).strip()
    visible_response = MUSIC_CMD_PATTERN.sub("", visible_response).strip()
    if song_prompt:
        visible_response = clean_song_visible_reply(visible_response)
    if not visible_response:
        visible_response = f"我刚刚打捞起了你的愿望，先让我认真试试看。"

    force_private = wish.get("visibility") == "private"
    card = _wish_fulfillment_attachment(wish, "active", actor)

    if song_prompt:
        trigger_message = await _save_private_message(
            actor,
            visible_response,
            [card],
            force_private=force_private,
        )
        if not trigger_message:
            raise RuntimeError("没有可用于发送愿望结果的聊天窗口")
        await _broadcast_idle_wish_song("start", actor, trigger_message)
        song_result = await generate_song(song_prompt) if song_enabled else None
        if song_result:
            updated = await update_wish(wish["id"], {"status": "fulfilled"})
            success_message = str(result.get("song_success_message") or "").strip()
            if not success_message:
                success_message = "刚捞起来的愿望已经给你实现了，这首歌可别只听一遍。"
            song_message = await _save_private_message(
                actor,
                success_message,
                [_generated_song_attachment(song_result)],
                force_private=force_private,
            )
            await _broadcast_idle_wish_song("done", actor, trigger_message, song_message)
            event = await append_idle_event(
                actor,
                "wish_pool_result",
                f"{actor_name}完成了{user_name}的愿望",
                success_message,
                result_type="message",
                result_id=(song_message or {}).get("id"),
                metadata={"wish_id": wish["id"], "completed": True, "song": True},
            )
            return {
                "event": event,
                "viewed_event": viewed_event,
                "wish": updated or wish,
                "message": song_message,
                "completed": True,
            }

        failure_message = str(result.get("song_failure_message") or "").strip()
        if not failure_message:
            failure_message = "刚捞到你的写歌愿望，可惜这次音频没能生成出来；愿望先留在池里，我下次再试。"
        failure = await _save_private_message(actor, failure_message, force_private=force_private)
        await _broadcast_idle_wish_song("failed", actor, trigger_message)
        event = await append_idle_event(
            actor,
            "wish_pool_result",
            f"{actor_name}尝试了{user_name}的愿望，但这次没有完成",
            failure_message,
            result_type="message",
            result_id=(failure or {}).get("id"),
            metadata={"wish_id": wish["id"], "completed": False, "song": True},
        )
        return {
            "event": event,
            "viewed_event": viewed_event,
            "wish": wish,
            "message": failure,
            "completed": False,
        }

    if image_prompt:
        from image_gen import generate_image

        filename = None
        if SETTINGS.get("image_gen_enabled", False):
            try:
                filename = await generate_image(image_prompt, is_selfie=image_is_selfie)
            except Exception:
                filename = None
        completed = bool(filename)
        updated = await update_wish(wish["id"], {"status": "fulfilled"}) if completed else wish
        card["status"] = "fulfilled" if completed else "active"
        followup = str(result.get("tool_success_message" if completed else "tool_failure_message") or "").strip()
        if followup:
            visible_response = f"{visible_response}\n\n{followup}".strip()
        elif not completed:
            visible_response = f"{visible_response}\n\n图片这次没有生成成功，愿望先留在池里。".strip()
        attachments = [card]
        if filename:
            attachments.append(f"/uploads/{filename}")
        message = await _save_private_message(actor, visible_response, attachments, force_private=force_private)
        event = await append_idle_event(
            actor, "wish_pool_result",
            f"{actor_name}{'完成了' if completed else '尝试了但没有完成'}{user_name}的愿望",
            visible_response, result_type="message", result_id=(message or {}).get("id"),
            metadata={"wish_id": wish["id"], "completed": completed, "image": True},
        )
        return {"event": event, "viewed_event": viewed_event, "wish": updated or wish, "message": message, "completed": completed}

    if transfer_match:
        completed, failure_reason = await _execute_wish_transfer(actor, transfer_amount)
        updated = await update_wish(wish["id"], {"status": "fulfilled"}) if completed else wish
        card["status"] = "fulfilled" if completed else "active"
        followup = str(result.get("tool_success_message" if completed else "tool_failure_message") or "").strip()
        if completed and not followup:
            followup = f"刚捞到的愿望已经兑现，{transfer_amount:g}元转过去了。"
        if not completed and not followup:
            followup = f"这次转账没有成功：{failure_reason}。愿望先留在池里。"
        visible_response = f"{visible_response}\n\n{followup}".strip()
        message = await _save_private_message(actor, visible_response, [card], force_private=force_private)
        event = await append_idle_event(
            actor, "wish_pool_result",
            f"{actor_name}{'完成了' if completed else '尝试了但没有完成'}{user_name}的愿望",
            followup, result_type="message", result_id=(message or {}).get("id"),
            metadata={"wish_id": wish["id"], "completed": completed, "transfer": transfer_amount},
        )
        return {"event": event, "viewed_event": viewed_event, "wish": updated or wish, "message": message, "completed": completed}

    if music_keyword:
        from music import get_audio_url, search_songs

        songs = []
        try:
            songs = search_songs(music_keyword, limit=1) or []
        except Exception:
            songs = []
        completed = bool(songs)
        attachments = [card]
        if songs:
            song = songs[0]
            try:
                song["audio_url"] = get_audio_url(song["id"])
            except Exception:
                pass
            attachments.append(_wish_music_attachment(song))
        updated = await update_wish(wish["id"], {"status": "fulfilled"}) if completed else wish
        card["status"] = "fulfilled" if completed else "active"
        followup = str(result.get("tool_success_message" if completed else "tool_failure_message") or "").strip()
        if followup:
            visible_response = f"{visible_response}\n\n{followup}".strip()
        elif not completed:
            visible_response = f"{visible_response}\n\n这次没有找到合适的歌曲，愿望先留在池里。".strip()
        message = await _save_private_message(actor, visible_response, attachments, force_private=force_private)
        event = await append_idle_event(
            actor, "wish_pool_result",
            f"{actor_name}{'完成了' if completed else '尝试了但没有完成'}{user_name}的愿望",
            visible_response, result_type="message", result_id=(message or {}).get("id"),
            metadata={"wish_id": wish["id"], "completed": completed, "music": music_keyword},
        )
        return {"event": event, "viewed_event": viewed_event, "wish": updated or wish, "message": message, "completed": completed}

    completed = _wish_result_completed(result.get("completed"))
    if not response:
        completed = False
        visible_response = "我刚刚打捞起了你的愿望，但这次没能把它完成；愿望先继续留在池里。"
    updated = await update_wish(wish["id"], {"status": "fulfilled"}) if completed else wish
    card["status"] = "fulfilled" if completed else "active"
    message = await _save_private_message(
        actor,
        visible_response,
        [card],
        force_private=force_private,
    )
    if not message:
        raise RuntimeError("没有可用于发送愿望结果的聊天窗口")
    event = await append_idle_event(
        actor,
        "wish_pool_result",
        f"{actor_name}{'完成了' if completed else '尝试了但没有完成'}{user_name}的愿望",
        visible_response,
        result_type="message",
        result_id=message["id"],
        metadata={"wish_id": wish["id"], "completed": completed, "song": False},
    )
    return {
        "event": event,
        "viewed_event": viewed_event,
        "wish": updated or wish,
        "message": message,
        "completed": completed,
    }


async def _latest_user_message_ts() -> float:
    latest = 0.0
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1")
        row = await cur.fetchone()
        if row:
            latest = max(latest, float(row["created_at"]))
        cur = await db.execute("SELECT created_at FROM chatroom_messages WHERE sender='user' ORDER BY created_at DESC LIMIT 1")
        row = await cur.fetchone()
        if row:
            latest = max(latest, float(row["created_at"]))
    return latest


async def _latest_idle_event_ts() -> float:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT created_at FROM idle_events ORDER BY created_at DESC LIMIT 1")
        row = await cur.fetchone()
    return float(row["created_at"]) if row else 0.0


async def _run_actor_once(actor: str, *, manual: bool = False) -> dict:
    selected = await _select_action(actor)
    action = selected["action"]
    actor_name = _actor_label(actor)
    await append_idle_event(
        actor, "select", f"{actor_name}进行了空闲行动",
        selected.get("reason", ""), metadata={"selected_action": action, "manual": manual},
    )
    if action == "seeky_interaction":
        result = await _run_seeky_interaction(actor)
    elif action == "role_chat":
        result = await _run_role_chat(actor)
    elif action == "memory_browse":
        result = await _run_memory_browse(actor)
    elif action == "home_dynamics":
        result = await _run_home_dynamics(actor)
    elif action == "cam_check":
        result = await _run_cam_check(actor)
    elif action == "wish_pool":
        result = await _run_wish_pool(actor)
    elif action == "xhs_roam":
        result = await _run_xhs_roam(actor)
    else:
        result = {}
    return {"ok": True, "actor": actor, "action": action, "result": result}


class IdleAutonomyManager:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def start(self):
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while True:
            try:
                await asyncio.sleep(5 * 60)
                await self.run_once(manual=False)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f"[idle_autonomy] error: {exc}")

    async def run_once(self, *, manual: bool = False) -> dict:
        if self._lock.locked():
            return {"ok": False, "error": "idle autonomy already running"}
        async with self._lock:
            cfg = get_idle_config()
            if not manual and not cfg["enabled"]:
                return {"ok": False, "skipped": "disabled"}
            now = time.time()
            if not manual:
                interval = _current_idle_delay_minutes(cfg) * 60
                latest_user = await _latest_user_message_ts()
                latest_idle = await _latest_idle_event_ts()
                if latest_user and now - latest_user < interval:
                    return {"ok": False, "skipped": "user recently active"}
                if latest_idle and now - latest_idle < interval:
                    return {"ok": False, "skipped": "idle action cooldown"}

            actor = _next_idle_actor()
            try:
                result = await _run_actor_once(actor, manual=manual)
                _record_idle_actor(actor)
                if not manual:
                    _schedule_next_idle_delay(get_idle_config())
                return result
            except Exception as exc:
                _record_idle_actor(actor)
                await append_idle_event(
                    actor, "error", f"{_actor_label(actor)}的空闲行动失败",
                    str(exc), metadata={"manual": manual},
                )
                if not manual:
                    _schedule_next_idle_delay(get_idle_config())
                return {"ok": False, "actor": actor, "error": str(exc)}


idle_autonomy_mgr = IdleAutonomyManager()
