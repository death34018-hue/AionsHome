"""
日程 / 闹铃管理器
- 后台线程每 30 秒扫描一次到期的闹铃
- 触发时组装 Prompt（世界书 + 记忆 + 上下文）调用 Core，与监控唤醒一致
- 所有日程持久化在 SQLite schedules 表，重启后自动恢复
"""

import asyncio, json, time, threading, logging, re
from datetime import datetime

import aiosqlite

from config import DB_PATH, DEFAULT_MODEL, load_worldbook, SETTINGS
from database import get_db
from ws import manager
from band_commands import process_band_vibration, with_band_vibration_attachment
from hug_pillow_commands import process_hug_pillow_commands
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from memory import recall_memories, format_recalled_memories_for_prompt
from music import search_songs, get_audio_url
from routes.music import MUSIC_CMD_PATTERN
from schedule_history import finish_schedule
from stream_safety import (
    CHAT_STREAM_POLICY,
    StreamSafetyResult,
    consume_safe_stream,
)
from tts import TTSStreamer
from web_search import WebCommandStreamFilter
from capabilities import build_band_note_ability_text
from wechat_bridge import (
    dispatch_wechat_message,
    process_wechat_outbound_commands,
    record_wechat_route,
)

log = logging.getLogger("schedule")
BACKGROUND_CLI_META = {"antigravity_print_timeout": "90s"}


async def _consume_background_stream(
    source,
    command_filter: WebCommandStreamFilter,
    tts_streamer: TTSStreamer | None,
) -> StreamSafetyResult:
    async def on_commit(chunk: str) -> None:
        if not tts_streamer:
            return
        visible = command_filter.feed(chunk)
        if visible:
            await tts_streamer.feed_async(visible)

    result = await consume_safe_stream(source, CHAT_STREAM_POLICY, on_commit)
    if tts_streamer:
        visible_tail = command_filter.flush()
        if visible_tail:
            await tts_streamer.feed_async(visible_tail)
    return result


def _new_background_meta() -> dict:
    return dict(BACKGROUND_CLI_META)


async def _broadcast_trigger_debug(
    *,
    msg_id: str,
    model_key: str,
    usage_meta: dict | None,
    prompt_messages: list | None,
    recalled_memories: list | None = None,
    has_error: bool = False,
    error_text: str | None = None,
) -> None:
    prompt_messages = prompt_messages or []
    debug_data = {
        "type": "debug",
        "model": model_key,
        "msg_id": msg_id,
        "recalled_memories": recalled_memories or [],
        "prompt_messages": prompt_messages,
        "prompt_count": len(prompt_messages),
        "usage": usage_meta if usage_meta else None,
        "has_error": has_error,
        "error_text": error_text if has_error else None,
    }
    await manager.broadcast({"type": "debug", "data": debug_data})


def _tts_voice_for_target(is_chatroom: bool, sender: str) -> str:
    if is_chatroom:
        try:
            from chatroom import load_chatroom_config
            cfg = load_chatroom_config()
        except Exception:
            return ""
        if not cfg.get("tts_enabled"):
            return ""
        key = "tts_aion_voice" if sender == "aion" else "tts_connor_voice"
        return (cfg.get(key) or "").strip()
    if not manager.any_tts_enabled():
        return ""
    return (manager.get_tts_voice() or "").strip()

# ── 文本指令正则 ──────────────────────────────────
_SCHEDULE_OPEN = r"[\[［]\s*"
_SCHEDULE_CLOSE = r"\s*[\]］]"
_SCHEDULE_COLON = r"\s*[:：]\s*"
_SCHEDULE_PIPE = r"\s*[|｜]\s*"
_SCHEDULE_FLAGS = re.IGNORECASE

ALARM_CMD = re.compile(
    rf"{_SCHEDULE_OPEN}ALARM{_SCHEDULE_COLON}(.+?){_SCHEDULE_PIPE}(.+?){_SCHEDULE_CLOSE}",
    _SCHEDULE_FLAGS,
)
REMINDER_CMD = re.compile(
    rf"{_SCHEDULE_OPEN}REMINDER{_SCHEDULE_COLON}(.+?){_SCHEDULE_PIPE}(.+?){_SCHEDULE_CLOSE}",
    _SCHEDULE_FLAGS,
)
MONITOR_CMD = re.compile(
    rf"{_SCHEDULE_OPEN}MONITOR{_SCHEDULE_COLON}(.+?){_SCHEDULE_PIPE}(.+?){_SCHEDULE_CLOSE}",
    _SCHEDULE_FLAGS,
)
SCHEDULE_DEL_CMD = re.compile(
    rf"{_SCHEDULE_OPEN}SCHEDULE_DEL{_SCHEDULE_COLON}(.+?){_SCHEDULE_CLOSE}",
    _SCHEDULE_FLAGS,
)
SCHEDULE_LIST_CMD = re.compile(
    rf"{_SCHEDULE_OPEN}SCHEDULE_LIST{_SCHEDULE_CLOSE}",
    _SCHEDULE_FLAGS,
)


def get_schedule_origin_name(origin: str | None) -> str:
    """Return the configured display name for the schedule creator."""
    wb = load_worldbook()
    origin = (origin or "aion").strip().lower()
    if origin == "connor":
        try:
            from chatroom import load_chatroom_config
            cfg = load_chatroom_config()
            return (cfg.get("connor_name") or "AI").strip() or "AI"
        except Exception:
            return "AI"
    if origin == "user":
        return (wb.get("user_name") or "用户").strip() or "用户"
    return (wb.get("ai_name") or "AI").strip() or "AI"


def _parse_dt(raw: str) -> str | None:
    """尝试把 AI 输出的时间字符串解析为 ISO 格式，失败返回 None"""
    raw = raw.strip()
    # ISO 格式的 T 分隔符统一替换为空格
    raw = raw.replace("T", " ")
    # 带时间的格式
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%m-%d %H:%M",
        "%m-%d %H:%M",
        "%m/%d %H:%M",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    # 纯日期格式（REMINDER 可能不带时间）→ 默认 09:00
    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m-%d",
        "%m/%d",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            dt = dt.replace(hour=9, minute=0)
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return None


def _is_schedule_time_stale(dt_text: str, *, grace_seconds: int = 90) -> bool:
    try:
        dt = datetime.strptime(dt_text.replace("T", " "), "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    return dt.timestamp() < time.time() - grace_seconds


# ── ScheduleManager ───────────────────────────────
class ScheduleManager:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        log.info("ScheduleManager started")

    def stop(self):
        self._running = False

    # ── 后台轮询 ──────────────────────────────────
    def _check_loop(self):
        while self._running:
            tick_future = asyncio.run_coroutine_threadsafe(self._tick(), self._loop)
            try:
                tick_future.result(timeout=CHAT_STREAM_POLICY.total_timeout + 30)
            except Exception as error:
                tick_future.cancel()
                log.exception(
                    "schedule tick error (%s)",
                    type(error).__name__,
                )
            # 每 30 秒检查一次
            for _ in range(60):          # 30s = 60 × 0.5s
                if not self._running:
                    return
                time.sleep(0.5)

    async def _tick(self):
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M")
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM schedules WHERE status='active' AND type='alarm' AND trigger_at <= ?",
                (now_iso,),
            )
            due_alarms = [dict(r) for r in await cur.fetchall()]
            cur = await db.execute(
                "SELECT * FROM schedules WHERE status='active' AND type='monitor' AND trigger_at <= ?",
                (now_iso,),
            )
            due_monitors = [dict(r) for r in await cur.fetchall()]
        for item in due_alarms:
            await self._fire_alarm(item)
        for item in due_monitors:
            await self._fire_monitor(item)

    def _resolve_target(self, item: dict) -> dict:
        """根据日程来源和用户最后活跃窗口，确定响应目标。
        返回:
          {"type": "private", "conv_id": ...}
          或 {"type": "chatroom", "room_id": ...}
        """
        origin = item.get("origin", "aion")
        if origin == "connor":
            room_id = manager.get_connor_last_active()
            if room_id:
                return {"type": "chatroom", "room_id": room_id}
            # Connor 侧没有活跃记录，回退到创建时的 room_id
            origin_room = item.get("origin_room_id", "")
            if origin_room:
                return {"type": "chatroom", "room_id": origin_room}
            return {"type": "private"}

        else:  # aion
            last = manager.get_aion_last_active()
            if last and last.startswith("chatroom:"):
                return {"type": "chatroom", "room_id": last.split(":", 1)[1]}
            # Aion 侧没有活跃记录，回退到创建时的 room_id
            origin_room = item.get("origin_room_id", "")
            if origin_room:
                return {"type": "chatroom", "room_id": origin_room}
            return {"type": "private"}

    async def fire_app_supervision_checkpoint(
        self,
        group: dict,
        *,
        event_id: str,
        checkpoint_minutes: int,
    ) -> None:
        display_name = str(group.get("displayName") or group.get("groupId") or "应用")
        role_id = "connor" if group.get("roleId") == "connor" else "aion"
        role_name = get_schedule_origin_name(role_id)
        minutes = int(checkpoint_minutes)
        capture_requested_at = time.time()
        await manager.broadcast({
            "type": "cam_check",
            "data": {
                "capture_only": True,
                "reason": "app_supervision_checkpoint",
                "event_id": event_id,
            },
        })
        from phone_screen import wait_for_phone_screen_after, freeze_phone_screen
        phone_path = await wait_for_phone_screen_after(capture_requested_at)
        phone_attachment = (
            freeze_phone_screen(phone_path, event_id=event_id)
            if phone_path is not None else ""
        )

        alarm_item = {
            "id": event_id,
            "content": f"{display_name}本轮累计达到 {minutes} 分钟检查点，请结合最近对话和当前应用状态自然回复，并自行决定是否干预。",
            "trigger_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "origin": role_id,
            "origin_room_id": "",
            "_app_supervision_checkpoint": True,
            "_app_supervision_display": f"【{display_name}本轮累计达到{minutes}分钟检查，唤醒{role_name}进行判定。】",
        }
        if phone_attachment:
            alarm_item["_app_supervision_phone_attachment"] = phone_attachment
        await self._fire_alarm(alarm_item)

    async def _save_to_private(
        self,
        conv_id: str,
        sys_content: str,
        ai_text: str,
        ai_msg_id: str,
        att_json: str,
        music_atts: list,
        reasoning_content: str = "",
        system_atts: list | None = None,
    ):
        """将系统消息和 AI 回复保存到 Aion 私聊"""
        now = time.time()
        sys_msg_id = f"msg_{int(now*1000)}_st"
        system_atts = list(system_atts or [])
        system_att_json = (
            json.dumps(system_atts, ensure_ascii=False) if system_atts else "[]"
        )
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (
                    sys_msg_id,
                    conv_id,
                    "system",
                    sys_content,
                    now,
                    system_att_json,
                ),
            )
            await db.commit()
        sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
                   "content": sys_content, "created_at": now,
                   "attachments": system_atts}
        await manager.broadcast({"type": "msg_created", "data": sys_msg})

        now2 = time.time()
        music_atts = await with_band_vibration_attachment(ai_msg_id, music_atts)
        att_json = json.dumps(music_atts, ensure_ascii=False) if music_atts else "[]"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments, reasoning_content) VALUES (?,?,?,?,?,?,?)",
                (ai_msg_id, conv_id, "assistant", ai_text, now2, att_json, reasoning_content),
            )
            await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
            await db.commit()
        ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant",
                  "content": ai_text, "created_at": now2, "attachments": music_atts,
                  "reasoning_content": reasoning_content}
        await manager.broadcast({"type": "msg_created", "data": ai_msg})

        from routes.files import export_conversation
        await export_conversation(conv_id)

    async def _save_to_chatroom(
        self,
        room_id: str,
        sender: str,
        sys_content: str,
        ai_text: str,
        ai_msg_id: str,
        att_json: str,
        music_atts: list,
        reasoning_content: str = "",
        system_atts: list | None = None,
    ):
        """将系统消息和 AI 回复保存到聊天室（群聊/Connor 私聊）"""
        now = time.time()
        sys_msg_id = f"cm_{int(now*1000)}_sys"
        system_atts = list(system_atts or [])
        system_att_json = (
            json.dumps(system_atts, ensure_ascii=False) if system_atts else "[]"
        )
        async with get_db() as db:
            await db.execute(
                "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (
                    sys_msg_id,
                    room_id,
                    "system",
                    sys_content,
                    now,
                    system_att_json,
                ),
            )
            await db.commit()
        sys_msg = {"id": sys_msg_id, "room_id": room_id, "sender": "system",
                   "content": sys_content, "created_at": now,
                   "attachments": system_atts}
        await manager.broadcast({"type": "chatroom_msg_created", "data": sys_msg})

        now2 = time.time()
        music_atts = await with_band_vibration_attachment(ai_msg_id, music_atts)
        att_json = json.dumps(music_atts, ensure_ascii=False) if music_atts else "[]"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments, reasoning_content) VALUES (?,?,?,?,?,?,?)",
                (ai_msg_id, room_id, sender, ai_text, now2, att_json, reasoning_content),
            )
            await db.execute("UPDATE chatroom_rooms SET updated_at=? WHERE id=?", (now2, room_id))
            await db.commit()
        ai_msg = {"id": ai_msg_id, "room_id": room_id, "sender": sender,
                  "content": ai_text, "created_at": now2, "attachments": music_atts,
                  "reasoning_content": reasoning_content}
        await manager.broadcast({"type": "chatroom_msg_created", "data": ai_msg})

    # ── 触发闹铃 ─────────────────────────────────
    async def _fire_alarm(self, item: dict):
        sid = item["id"]
        content = item["content"]
        trigger_at = item["trigger_at"]
        origin = item.get("origin", "aion")
        is_app_checkpoint = bool(item.get("_app_supervision_checkpoint"))
        checkpoint_display = str(item.get("_app_supervision_display") or "").strip()
        checkpoint_phone_attachment = str(
            item.get("_app_supervision_phone_attachment") or ""
        ).strip()
        origin_name = get_schedule_origin_name(origin)
        log.info("firing alarm %s: %s @%s (origin=%s)", sid, content, trigger_at, origin)

        # 标记为已触发
        if not is_app_checkpoint:
            async with aiosqlite.connect(DB_PATH) as db:
                changed = await finish_schedule(db, sid, "triggered")
                await db.commit()
            if not changed:
                return

        # 广播给前端弹窗
        if not is_app_checkpoint:
            await manager.broadcast({
                "type": "schedule_alarm",
                "data": {"id": sid, "content": content, "trigger_at": trigger_at, "origin": origin, "origin_name": origin_name},
            })
            await manager.broadcast({"type": "schedule_changed"})

        # ── 确定响应目标 ──
        target = self._resolve_target(item)
        is_chatroom = target["type"] == "chatroom"
        sender = "connor" if origin == "connor" else "aion"

        # ── 组装 Prompt 调用 Core（与 camera._call_core 一致） ──
        wb = load_worldbook()
        user_name = wb.get("user_name", "你")
        ai_name = wb.get("ai_name", "AI")
        heart_rate_block = ""
        try:
            from health_context import build_heart_rate_prompt_block
            heart_rate_block = await build_heart_rate_prompt_block(user_name)
        except Exception:
            heart_rate_block = ""

        # 获取 conv_id（始终需要，用于 Aion 的上下文获取和保存）
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1")
            conv = await cur.fetchone()
            if not conv:
                return
            conv_id = conv["id"]
            model_key = conv["model"] or DEFAULT_MODEL

        # 聊天室来源的闹铃，优先用聊天室配置的 Aion 模型
        if is_chatroom and origin != "connor":
            from chatroom import load_chatroom_config as _lcc
            _cr_model = _lcc().get("aion_model", "").strip()
            if _cr_model:
                model_key = _cr_model

        # 根据来源构建不同的上下文
        debug_recalled = []
        if origin == "connor" and is_chatroom:
            # Connor 来源 → 用 Connor 的上下文
            from chatroom import build_connor_group_context, build_connor_1v1_context
            room_id = target["room_id"]
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM chatroom_rooms WHERE id=?", (room_id,))
                room = await cur.fetchone()
            if not room:
                return
            room = dict(room)
            room_type = room.get("type", "group")

            if room_type == "connor_1v1":
                messages_ctx, _ = await build_connor_1v1_context(room_id, [], query_text=content)
            else:
                messages_ctx, _ = await build_connor_group_context(room_id, [], query_text=content)

            now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
            trigger_prompt = (
                f"{'[应用使用检查点触发]' if is_app_checkpoint else '[日程闹铃触发]'}\n"
                f"日程内容：{trigger_at} — {content}\n"
                f"现在时间已经到了（当前 {now_str}），请提醒【{user_name}】。"
                f"{heart_rate_block}"
            )
            if checkpoint_phone_attachment:
                trigger_prompt += "\n系统附带了本次检查点刚刚截取的手机屏幕，请结合画面判断。"
            elif is_app_checkpoint:
                trigger_prompt += "\n本次手机屏幕未能在等待时间内取得，请根据应用状态和对话继续判断。"
            messages_ctx.append({"role": "user", "content": trigger_prompt})
            if checkpoint_phone_attachment:
                messages_ctx[-1]["attachments"] = [checkpoint_phone_attachment]
            messages = messages_ctx
        else:
            # Aion 来源 → 沿用原有逻辑
            from context_builder import fetch_merged_timeline, render_merged_timeline
            merged = await fetch_merged_timeline("aion", 20, conv_id=conv_id)
            history = render_merged_timeline(merged, "aion")

            prefix = []
            if wb.get("ai_persona"):
                prefix.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
                prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
            if wb.get("user_persona"):
                prefix.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
                prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

            now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
            if prefix:
                prefix[-1]["content"] += f"\n系统当前的准确时间是 {now_str}"

            abilities = []
            abilities.append("[MUSIC:歌曲名 歌手名] — 点歌/推荐音乐。系统自动展示播放卡片并自动播放，不要在指令外重复歌曲信息。可同时用多个。")
            abilities.append("[ALARM:YYYY-MM-DDTHH:MM|内容] — 设置闹铃，到时间系统会主动提醒用户。日期时间用ISO格式。")
            abilities.append("[REMINDER:YYYY-MM-DD|内容] — 设置日程提醒（不闹铃），你在合适时机自然提起即可。")
            abilities.append(f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时监督。到时间后系统自动截取摄像头画面发送给你，你可以查看{user_name}的状态。")
            abilities.append("[SCHEDULE_DEL:日程id] — 删除指定日程/闹铃/定时监控。")
            passive_band_ability = build_band_note_ability_text(user_name, passive=True)
            if passive_band_ability:
                abilities.append(passive_band_ability)
            ability_block = "[系统能力] 你可以在回复中根据对话氛围，善用以下指令：\n" + "\n".join(f"{i+1}. {a}" for i, a in enumerate(abilities))

            active_schedules = await get_active_schedules()
            schedule_text = build_schedule_prompt(active_schedules)
            ability_block += f"\n\n【当前日程列表】\n{schedule_text}"

            cap_idx = len(prefix) if prefix else 0
            history.insert(cap_idx, {"role": "user", "content": ability_block})
            history.insert(cap_idx + 1, {"role": "assistant", "content": "好的，需要时我会使用这些指令。"})

            trigger_prompt = (
                f"{'[应用使用检查点触发]' if is_app_checkpoint else '[日程闹铃触发]'}\n"
                f"日程内容：{trigger_at} — {content}\n"
                f"现在时间已经到了（当前 {now_str}），请提醒【{user_name}】。"
                f"{heart_rate_block}"
            )
            if checkpoint_phone_attachment:
                trigger_prompt += "\n系统附带了本次检查点刚刚截取的手机屏幕，请结合画面判断。"
            elif is_app_checkpoint:
                trigger_prompt += "\n本次手机屏幕未能在等待时间内取得，请根据应用状态和对话继续判断。"

            recalled, _ = await recall_memories(trigger_prompt[:300])
            debug_recalled = recalled
            mem_inject = []
            if recalled:
                mem_lines = format_recalled_memories_for_prompt(recalled)
                mem_inject = [
                    {"role": "user", "content": f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"},
                    {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"},
                ]

            trigger_message = {"role": "user", "content": trigger_prompt}
            if checkpoint_phone_attachment:
                trigger_message["attachments"] = [checkpoint_phone_attachment]
            messages = prefix + mem_inject + history + [trigger_message]

        from app_supervision_ai import inject_app_supervision_context
        inject_app_supervision_context(messages)

        # 预生成 ai_msg_id（TTS 分段文件命名需要）
        ai_msg_id = f"msg_{int(time.time()*1000)}_sa"
        usage_meta = _new_background_meta()
        debug_model_key = model_key
        has_error = False
        error_text = None
        alarm_command_filter = WebCommandStreamFilter()

        # Connor 来源时根据配置的模型调用
        if origin == "connor":
            from chatroom import stream_connor_cli, load_chatroom_config as _lcc_cr
            from ai_providers import CLI_STATUS_PREFIX as _CSP

            _connor_model = (_lcc_cr().get("connor_model") or "Codex").strip() or "Codex"
            debug_model_key = _connor_model

            alarm_tts = None
            tts_voice = _tts_voice_for_target(is_chatroom, "connor")
            if tts_voice:
                alarm_tts = TTSStreamer(ai_msg_id, tts_voice, manager)

            if _connor_model == "Codex":
                async def content_stream():
                    async for chunk in stream_connor_cli(messages=messages, meta=usage_meta):
                        if chunk.startswith(_CSP):
                            continue
                        yield chunk
            else:
                _temp = SETTINGS.get("temperature")

                async def content_stream():
                    async for chunk in stream_ai(messages, _connor_model, meta=usage_meta, temperature=_temp):
                        if chunk.startswith(CLI_STATUS_PREFIX):
                            continue
                        yield chunk
        else:
            # Aion 来源用常规 stream_ai
            alarm_tts = None
            tts_voice = _tts_voice_for_target(is_chatroom, "aion")
            if tts_voice:
                alarm_tts = TTSStreamer(ai_msg_id, tts_voice, manager)

            _temp = SETTINGS.get("temperature")

            async def content_stream():
                async for chunk in stream_ai(messages, model_key, meta=usage_meta, temperature=_temp):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        continue
                    yield chunk

        stream_result = await _consume_background_stream(
            content_stream(),
            alarm_command_filter,
            alarm_tts,
        )
        full_text = stream_result.committed_text
        has_error = stream_result.stop_reason is not None
        error_text = stream_result.diagnostic_error or stream_result.stop_reason
        safety_notice = stream_result.notice

        if not full_text.strip() and not safety_notice:
            return

        # 检测 [MUSIC:xxx] 指令
        music_matches = MUSIC_CMD_PATTERN.findall(full_text)
        music_cards = []
        if music_matches:
            for keyword in music_matches:
                keyword = keyword.strip()
                try:
                    results = search_songs(keyword, limit=5)
                    if results:
                        song = results[0]
                        song["audio_url"] = get_audio_url(song["id"])
                        song["candidates"] = results[1:4]
                        music_cards.append(song)
                except Exception:
                    pass
            full_text = MUSIC_CMD_PATTERN.sub("", full_text).strip()

        # 处理回复中可能包含的日程指令
        if is_chatroom:
            full_text = await process_schedule_commands(full_text, None, origin=origin, origin_room_id=target["room_id"], after_msg_id=ai_msg_id)
        else:
            full_text = await process_schedule_commands(full_text, conv_id, after_msg_id=ai_msg_id)
        full_text = await _process_background_reply_commands(
            full_text,
            target=target,
            conv_id=conv_id,
            sender=sender,
            ai_msg_id=ai_msg_id,
        )
        if safety_notice:
            full_text = f"{full_text}\n\n[{safety_notice}]".strip()

        music_atts = [{"type": "music", "name": s["name"], "artist": s["artist"], "id": s["id"]} for s in music_cards] if music_cards else []
        att_json = json.dumps(music_atts, ensure_ascii=False) if music_atts else "[]"
        reasoning_content = (usage_meta.get("reasoning_content") or "").strip()

        # ── 保存到目标窗口 ──
        sys_content = (
            checkpoint_display or f"应用使用检查点触发：{content}"
            if is_app_checkpoint else f"⏰ 日程闹铃触发：{content}"
        )
        if is_chatroom:
            await self._save_to_chatroom(target["room_id"], sender, sys_content, full_text, ai_msg_id, att_json, music_atts, reasoning_content)
        else:
            await self._save_to_private(conv_id, sys_content, full_text, ai_msg_id, att_json, music_atts, reasoning_content)
        await _broadcast_trigger_debug(
            msg_id=ai_msg_id,
            model_key=debug_model_key,
            usage_meta=usage_meta,
            prompt_messages=messages,
            recalled_memories=debug_recalled,
            has_error=has_error,
            error_text=error_text,
        )

        # 刷新 TTS 剩余文本
        if alarm_tts:
            try:
                await alarm_tts.flush()
            except Exception:
                pass

        # 推送音乐卡片（带 autoplay 标记，前端自动播放）
        if music_cards:
            music_data = {'type': 'music', 'msg_id': ai_msg_id, 'cards': music_cards, 'autoplay': True}
            await manager.broadcast({"type": "music", "data": music_data})

    # ── 触发定时监控 ─────────────────────────────
    async def _fire_monitor(self, item: dict):
        sid = item["id"]
        content = item["content"]
        trigger_at = item["trigger_at"]
        origin = item.get("origin", "aion")
        origin_name = get_schedule_origin_name(origin)
        log.info("firing monitor %s: %s @%s (origin=%s)", sid, content, trigger_at, origin)

        # 标记为已触发
        async with aiosqlite.connect(DB_PATH) as db:
            changed = await finish_schedule(db, sid, "triggered")
            await db.commit()
        if not changed:
            return
        await manager.broadcast({"type": "schedule_changed"})

        # ── 确定响应目标 ──
        target = self._resolve_target(item)
        is_chatroom = target["type"] == "chatroom"
        sender = "connor" if origin == "connor" else "aion"

        # 尝试截图。手机源必须等待本轮 request_id 对应的新照片。
        from camera import (
            acquire_monitor_image,
            build_monitor_alert_data,
            cam,
            save_monitor_camera_snapshot,
        )
        fname = None
        camera_jpeg = None
        await manager.broadcast({
            "type": "monitor_alert",
            "data": build_monitor_alert_data(
                content,
                origin=origin,
                origin_name=origin_name,
            ),
        })
        no_image_context = ""
        if cam.cfg.get("active_source", "local") == "phone":
            image_result = await acquire_monitor_image("scheduled_monitor")
            jpg_bytes = image_result.jpeg
            camera_jpeg = image_result.camera_jpeg
            no_image_context = image_result.no_image_context
        else:
            # 本地/ESP32 路径仍合成一次本轮新鲜的手机屏幕截图。
            phone_screen_requested_at = time.time()
            from phone_screen import wait_for_phone_screen_after
            await wait_for_phone_screen_after(phone_screen_requested_at)
            jpg_bytes, camera_jpeg = cam.get_capture_jpegs(
                phone_screen_after=phone_screen_requested_at,
            )
            if not jpg_bytes:
                jpg_bytes = cam.get_screen_only_jpeg(
                    phone_screen_after=phone_screen_requested_at,
                )
        if jpg_bytes:
            from config import UPLOADS_DIR, SCREENSHOTS_DIR
            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = f"monitor_{ts}.jpg"
            fpath = UPLOADS_DIR / fname
            fpath.write_bytes(jpg_bytes)

            # 同时保存到 screenshots 目录
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            (SCREENSHOTS_DIR / fname).write_bytes(jpg_bytes)

        # 获取最新对话
        wb = load_worldbook()
        user_name = wb.get("user_name", "你")
        ai_name = wb.get("ai_name", "AI")

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1")
            conv = await cur.fetchone()
            if not conv:
                return
            conv_id = conv["id"]
            model_key = conv["model"] or DEFAULT_MODEL

        # 聊天室来源的监控，优先用聊天室配置的 Aion 模型
        if is_chatroom and origin != "connor":
            from chatroom import load_chatroom_config as _lcc
            _cr_model = _lcc().get("aion_model", "").strip()
            if _cr_model:
                model_key = _cr_model

        now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")

        # 获取最近 1 小时的设备活动摘要（6 条）
        activity_summary_text = ""
        user_dynamics_text = ""
        try:
            from activity import get_activity_summary_for_prompt, get_user_dynamics_for_prompt
            activity_summary_text = get_activity_summary_for_prompt(6)
            user_dynamics_text = get_user_dynamics_for_prompt(hours=1)
        except Exception:
            pass
        user_dynamics_block = (
            f"\n以下是{user_name}过去一小时的用户关键动态：\n{user_dynamics_text}\n"
            if user_dynamics_text else ""
        )
        heart_rate_block = ""
        try:
            from health_context import build_heart_rate_prompt_block
            heart_rate_block = await build_heart_rate_prompt_block(user_name)
        except Exception:
            heart_rate_block = ""

        # 触发提示词（两种来源共用）
        trigger_prompt = (
            f"[定时监控触发]\n"
            f"你之前设置了在 {trigger_at.replace('T', ' ')} 查看【{user_name}】的状态。\n"
            f"监控目的：{content}\n"
        )
        if fname:
            trigger_prompt += f"这是系统在当前时间（{now_str}）自动从摄像头截取的实时画面。\n"
        else:
            trigger_prompt += f"当前时间是{now_str}，摄像头未开启，无法获取画面。\n"
        if no_image_context:
            trigger_prompt += no_image_context + "\n"
        if activity_summary_text:
            trigger_prompt += (
                f"\n以下是{user_name}过去一小时的设备使用动态（手机/电脑应用使用情况，每10分钟一条摘要）：\n"
                f"{activity_summary_text}\n"
            )
        trigger_prompt += user_dynamics_block
        trigger_prompt += heart_rate_block
        if fname:
            trigger_prompt += f"\n请根据画面内容、设备活动动态、心率摘要和之前的对话上下文，自然地回应。"
        else:
            trigger_prompt += f"\n请根据设备活动动态、心率摘要和之前的对话上下文，自然地回应。"

        # 根据来源构建不同的上下文
        if origin == "connor" and is_chatroom:
            from chatroom import build_connor_group_context, build_connor_1v1_context
            room_id = target["room_id"]
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM chatroom_rooms WHERE id=?", (room_id,))
                room = await cur.fetchone()
            if not room:
                return
            room = dict(room)
            room_type = room.get("type", "group")

            if room_type == "connor_1v1":
                messages_ctx, _ = await build_connor_1v1_context(room_id, [], query_text=content)
            else:
                messages_ctx, _ = await build_connor_group_context(room_id, [], query_text=content)

            trigger_msg = {"role": "user", "content": trigger_prompt}
            if fname:
                trigger_msg["attachments"] = [f"/uploads/{fname}"]
            messages_ctx.append(trigger_msg)
            messages = messages_ctx
        else:
            # Aion 来源 → 沿用原有逻辑
            from context_builder import fetch_merged_timeline, render_merged_timeline
            merged = await fetch_merged_timeline("aion", 20, conv_id=conv_id)
            history = render_merged_timeline(merged, "aion")

            prefix = []
            if wb.get("ai_persona"):
                prefix.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
                prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
            if wb.get("user_persona"):
                prefix.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
                prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

            if prefix:
                prefix[-1]["content"] += f"\n系统当前的准确时间是 {now_str}"

            abilities = []
            abilities.append("[MUSIC:歌曲名 歌手名] — 点歌/推荐音乐。系统自动展示播放卡片并自动播放，不要在指令外重复歌曲信息。可同时用多个。")
            abilities.append("[ALARM:YYYY-MM-DDTHH:MM|内容] — 设置闹铃，到时间系统会主动提醒用户。日期时间用ISO格式。")
            abilities.append("[REMINDER:YYYY-MM-DD|内容] — 设置日程提醒（不闹铃），你在合适时机自然提起即可。")
            abilities.append(f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时监督。到时间后系统自动截取摄像头画面发送给你，你可以查看{user_name}的状态。")
            abilities.append("[SCHEDULE_DEL:日程id] — 删除指定日程/闹铃/定时监控。")
            passive_band_ability = build_band_note_ability_text(user_name, passive=True)
            if passive_band_ability:
                abilities.append(passive_band_ability)
            ability_block = "[系统能力] 你可以在回复中根据对话氛围，善用以下指令：\n" + "\n".join(f"{i+1}. {a}" for i, a in enumerate(abilities))

            active_schedules = await get_active_schedules()
            schedule_text = build_schedule_prompt(active_schedules)
            ability_block += f"\n\n【当前日程列表】\n{schedule_text}"

            cap_idx = len(prefix) if prefix else 0
            history.insert(cap_idx, {"role": "user", "content": ability_block})
            history.insert(cap_idx + 1, {"role": "assistant", "content": "好的，需要时我会使用这些指令。"})

            trigger_msg = {"role": "user", "content": trigger_prompt}
            if fname:
                trigger_msg["attachments"] = [f"/uploads/{fname}"]
            messages = prefix + history + [trigger_msg]

        from app_supervision_ai import inject_app_supervision_context
        inject_app_supervision_context(messages)

        # 预生成 ai_msg_id（TTS 分段文件命名需要）
        ai_msg_id = f"msg_{int(time.time()*1000)}_sm"
        usage_meta = _new_background_meta()
        debug_model_key = model_key
        has_error = False
        error_text = None
        monitor_command_filter = WebCommandStreamFilter()

        # Connor 来源时根据配置的模型调用
        if origin == "connor":
            from chatroom import stream_connor_cli, load_chatroom_config as _lcc_cr
            from ai_providers import CLI_STATUS_PREFIX as _CSP

            _connor_model = (_lcc_cr().get("connor_model") or "Codex").strip() or "Codex"
            debug_model_key = _connor_model

            monitor_tts = None
            tts_voice = _tts_voice_for_target(is_chatroom, "connor")
            if tts_voice:
                monitor_tts = TTSStreamer(ai_msg_id, tts_voice, manager)

            if _connor_model == "Codex":
                async def content_stream():
                    async for chunk in stream_connor_cli(messages=messages, meta=usage_meta):
                        if chunk.startswith(_CSP):
                            continue
                        yield chunk
            else:
                _temp = SETTINGS.get("temperature")

                async def content_stream():
                    async for chunk in stream_ai(messages, _connor_model, meta=usage_meta, temperature=_temp):
                        if chunk.startswith(CLI_STATUS_PREFIX):
                            continue
                        yield chunk
        else:
            monitor_tts = None
            tts_voice = _tts_voice_for_target(is_chatroom, "aion")
            if tts_voice:
                monitor_tts = TTSStreamer(ai_msg_id, tts_voice, manager)

            _temp = SETTINGS.get("temperature")

            async def content_stream():
                async for chunk in stream_ai(messages, model_key, meta=usage_meta, temperature=_temp):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        continue
                    yield chunk

        stream_result = await _consume_background_stream(
            content_stream(),
            monitor_command_filter,
            monitor_tts,
        )
        full_text = stream_result.committed_text
        has_error = stream_result.stop_reason is not None
        error_text = stream_result.diagnostic_error or stream_result.stop_reason
        safety_notice = stream_result.notice

        if not full_text.strip() and not safety_notice:
            return

        # 检测 [MUSIC:xxx] 指令
        music_matches = MUSIC_CMD_PATTERN.findall(full_text)
        music_cards = []
        if music_matches:
            for keyword in music_matches:
                keyword = keyword.strip()
                try:
                    results = search_songs(keyword, limit=5)
                    if results:
                        song = results[0]
                        song["audio_url"] = get_audio_url(song["id"])
                        song["candidates"] = results[1:4]
                        music_cards.append(song)
                except Exception:
                    pass
            full_text = MUSIC_CMD_PATTERN.sub("", full_text).strip()

        # 处理回复中可能包含的日程指令
        if is_chatroom:
            full_text = await process_schedule_commands(full_text, None, origin=origin, origin_room_id=target["room_id"], after_msg_id=ai_msg_id)
        else:
            full_text = await process_schedule_commands(full_text, conv_id, after_msg_id=ai_msg_id)
        full_text = await _process_background_reply_commands(
            full_text,
            target=target,
            conv_id=conv_id,
            sender=sender,
            ai_msg_id=ai_msg_id,
        )
        if safety_notice:
            full_text = f"{full_text}\n\n[{safety_notice}]".strip()

        music_atts = [{"type": "music", "name": s["name"], "artist": s["artist"], "id": s["id"]} for s in music_cards] if music_cards else []
        att_json = json.dumps(music_atts, ensure_ascii=False) if music_atts else "[]"
        reasoning_content = (usage_meta.get("reasoning_content") or "").strip()
        snapshot_attachment = save_monitor_camera_snapshot(
            camera_jpeg,
            "scheduled_monitor",
        )
        system_atts = [snapshot_attachment] if snapshot_attachment else []

        # ── 保存到目标窗口 ──
        if origin == "connor":
            from chatroom import load_chatroom_config
            _cname = load_chatroom_config().get("connor_name", "AI")
            sys_content = f"{_cname}查看了监控"
        else:
            sys_content = f"{ai_name}查看了监控"
        if is_chatroom:
            await self._save_to_chatroom(
                target["room_id"],
                sender,
                sys_content,
                full_text,
                ai_msg_id,
                att_json,
                music_atts,
                reasoning_content,
                system_atts=system_atts,
            )
        else:
            await self._save_to_private(
                conv_id,
                sys_content,
                full_text,
                ai_msg_id,
                att_json,
                music_atts,
                reasoning_content,
                system_atts=system_atts,
            )
        await _broadcast_trigger_debug(
            msg_id=ai_msg_id,
            model_key=debug_model_key,
            usage_meta=usage_meta,
            prompt_messages=messages,
            recalled_memories=[],
            has_error=has_error,
            error_text=error_text,
        )

        # 刷新 TTS 剩余文本
        if monitor_tts:
            try:
                await monitor_tts.flush()
            except Exception:
                pass

        # 推送音乐卡片（带 autoplay 标记）
        if music_cards:
            music_data = {'type': 'music', 'msg_id': ai_msg_id, 'cards': music_cards, 'autoplay': True}
            await manager.broadcast({"type": "music", "data": music_data})


# ── 指令解析（在 AI 回复完成后调用） ──────────────
async def process_schedule_commands(full_text: str, conv_id: str = None, origin: str = "aion", origin_room_id: str = "", after_msg_id: str = None) -> str:
    """
    检测并处理 AI 回复中的日程指令，返回 strip 后的文本。
    即使 AI 格式有误也不抛异常，静默跳过。
    origin: 'aion' 或 'connor'，标记创建者
    origin_room_id: 群聊/Connor私聊的 room_id（空=Aion私聊创建）
    after_msg_id: 由某条 AI 回复里的指令产生时，前端应把系统提示显示在该回复下方
    """
    text = full_text
    actor_name = get_schedule_origin_name(origin)

    # [ALARM:datetime|content]
    for match in ALARM_CMD.finditer(full_text):
        try:
            raw_dt, content = match.group(1), match.group(2)
            dt = _parse_dt(raw_dt)
            log.info("ALARM detected: raw_dt=%s parsed=%s content=%s", raw_dt, dt, content)
            if dt and content.strip() and not _is_schedule_time_stale(dt):
                await _add_schedule("alarm", dt, content.strip(), origin, origin_room_id)
                if conv_id:
                    await _sys_msg(conv_id, f"【{actor_name}】设定了闹铃：{dt.replace('T', ' ')}，内容：{content.strip()}", after_msg_id=after_msg_id)
            elif dt and content.strip():
                log.warning("ALARM skipped because trigger time is stale: raw_dt=%s parsed=%s content=%s", raw_dt, dt, content)
            else:
                log.warning("ALARM skipped: dt=%s content=%s", dt, content)
        except Exception as e:
            log.error("ALARM processing error: %s", e)
    text = ALARM_CMD.sub("", text)

    # [REMINDER:date|content]
    for match in REMINDER_CMD.finditer(full_text):
        try:
            raw_dt, content = match.group(1), match.group(2)
            dt = _parse_dt(raw_dt)
            log.info("REMINDER detected: raw_dt=%s parsed=%s content=%s", raw_dt, dt, content)
            if dt and content.strip() and not _is_schedule_time_stale(dt):
                await _add_schedule("reminder", dt, content.strip(), origin, origin_room_id)
                if conv_id:
                    await _sys_msg(conv_id, f"【{actor_name}】设定了日程：{dt.replace('T', ' ')}，内容：{content.strip()}", after_msg_id=after_msg_id)
            elif dt and content.strip():
                log.warning("REMINDER skipped because trigger time is stale: raw_dt=%s parsed=%s content=%s", raw_dt, dt, content)
            else:
                log.warning("REMINDER skipped: dt=%s content=%s", dt, content)
        except Exception as e:
            log.error("REMINDER processing error: %s", e)
    text = REMINDER_CMD.sub("", text)

    # [Monitor:datetime|content]
    for match in MONITOR_CMD.finditer(full_text):
        try:
            raw_dt, content = match.group(1), match.group(2)
            dt = _parse_dt(raw_dt)
            log.info("MONITOR detected: raw_dt=%s parsed=%s content=%s", raw_dt, dt, content)
            if dt and content.strip() and not _is_schedule_time_stale(dt):
                await _add_schedule("monitor", dt, content.strip(), origin, origin_room_id)
                if conv_id:
                    await _sys_msg(conv_id, f"【{actor_name}】设定了监督：{dt.replace('T', ' ')}，内容：{content.strip()}", after_msg_id=after_msg_id)
            elif dt and content.strip():
                log.warning("MONITOR skipped because trigger time is stale: raw_dt=%s parsed=%s content=%s", raw_dt, dt, content)
            else:
                log.warning("MONITOR skipped: dt=%s content=%s", dt, content)
        except Exception as e:
            log.error("MONITOR processing error: %s", e)
    text = MONITOR_CMD.sub("", text)

    # [SCHEDULE_DEL:id]
    for match in SCHEDULE_DEL_CMD.finditer(full_text):
        try:
            sid = match.group(1).strip()
            if sid:
                info = await _get_schedule_info(sid)
                changed = await _del_schedule(sid)
                if conv_id and info and changed:
                    type_labels = {"alarm": "闹铃", "reminder": "日程", "monitor": "定时监控"}
                    label = type_labels.get(info["type"], "日程")
                    await _sys_msg(conv_id, f"【{actor_name}】取消了 {info['trigger_at'].replace('T', ' ')} 的{label}：{info['content']}", after_msg_id=after_msg_id)
        except Exception as e:
            log.error("SCHEDULE_DEL processing error: %s", e)
    text = SCHEDULE_DEL_CMD.sub("", text)

    # [SCHEDULE_LIST] → 不需要实际操作，仅 strip
    text = SCHEDULE_LIST_CMD.sub("", text)

    return text.strip()


async def _sys_msg(conv_id: str, content: str, after_msg_id: str = None):
    """插入一条系统消息并广播"""
    now = time.time()
    msg_id = f"msg_{int(now*1000)}_ss"
    order_atts = [{"type": "system_notice_order", "after_msg_id": after_msg_id}] if after_msg_id else []
    att_json = json.dumps(order_atts, ensure_ascii=False) if order_atts else "[]"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "system", content, now, att_json),
        )
        await db.commit()
    msg = {"id": msg_id, "conv_id": conv_id, "role": "system",
           "content": content, "created_at": now, "attachments": order_atts}
    await manager.broadcast({"type": "msg_created", "data": msg})


async def _chatroom_sys_msg(room_id: str, content: str, after_msg_id: str = None):
    """Insert a chatroom system notice and broadcast it immediately."""
    now = time.time()
    msg_id = f"cm_{time.time_ns()}_sys"
    order_atts = [{"type": "system_model_context"}]
    if after_msg_id:
        order_atts.append({"type": "system_notice_order", "after_msg_id": after_msg_id})
    att_json = json.dumps(order_atts, ensure_ascii=False) if order_atts else "[]"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, room_id, "system", content, now, att_json),
        )
        await db.commit()
    msg = {"id": msg_id, "room_id": room_id, "sender": "system",
           "content": content, "created_at": now, "attachments": order_atts}
    await manager.broadcast({"type": "chatroom_msg_created", "data": msg})


async def _process_background_wechat_commands(
    full_text: str,
    *,
    target: dict,
    conv_id: str | None,
    sender: str,
    ai_msg_id: str,
) -> str:
    """Run outbound WeChat commands for alarm/monitor generated replies."""
    if (target or {}).get("type") == "chatroom":
        room_id = (target or {}).get("room_id") or ""
        if not room_id:
            return full_text

        async def _save_chatroom_system(system_text: str):
            await _chatroom_sys_msg(room_id, system_text, after_msg_id=ai_msg_id)

        cleaned, _ = await process_wechat_outbound_commands(
            full_text,
            source_type="chatroom",
            source_id=room_id,
            sender=sender,
            source_msg_id=ai_msg_id,
            save_system_message=_save_chatroom_system,
            send_wechat_message=dispatch_wechat_message,
            record_route=record_wechat_route,
        )
        return cleaned

    if not conv_id:
        return full_text

    async def _save_private_system(system_text: str):
        await _sys_msg(conv_id, system_text, after_msg_id=ai_msg_id)

    cleaned, _ = await process_wechat_outbound_commands(
        full_text,
        source_type="aion_private",
        source_id=conv_id,
        sender=sender,
        source_msg_id=ai_msg_id,
        save_system_message=_save_private_system,
        send_wechat_message=dispatch_wechat_message,
        record_route=record_wechat_route,
    )
    return cleaned


async def _process_background_reply_commands(
    full_text: str,
    *,
    target: dict,
    conv_id: str | None,
    sender: str,
    ai_msg_id: str,
) -> str:
    """Run lightweight shared post-processing for background AI replies."""
    from routes.chat import _hug_pillow_sys_msg, _process_home_commands

    cleaned = await _process_home_commands(full_text)
    cleaned = await _process_background_wechat_commands(
        cleaned,
        target=target,
        conv_id=conv_id,
        sender=sender,
        ai_msg_id=ai_msg_id,
    )
    target_type = "chatroom" if (target or {}).get("type") == "chatroom" else "private"
    source_id = (target or {}).get("room_id") if target_type == "chatroom" else conv_id
    cleaned = await process_band_vibration(
        cleaned,
        source_type=f"background_{target_type}",
        source_id=source_id or "",
        source_msg_id=ai_msg_id,
        sender=sender,
    )
    if target_type == "chatroom":
        save_hug_system_message = lambda text: _chatroom_sys_msg(
            source_id or "", text, after_msg_id=ai_msg_id
        )
    else:
        save_hug_system_message = lambda text: _hug_pillow_sys_msg(
            conv_id or "", text, after_msg_id=ai_msg_id
        )
    cleaned = await process_hug_pillow_commands(
        cleaned,
        source_type=f"background_{target_type}",
        source_id=source_id or "",
        source_msg_id=ai_msg_id,
        sender=sender,
        save_system_message=save_hug_system_message,
    )
    from app_supervision_ai import (
        queue_app_supervision_reply_command,
        broadcast_app_supervision_command,
    )
    cleaned, supervision_command = await queue_app_supervision_reply_command(
        cleaned,
        source_message_id=ai_msg_id,
        role_id="connor" if sender == "connor" else "aion",
        source_kind=f"background_{target_type}",
        source_ref=source_id or "",
    )
    await broadcast_app_supervision_command(supervision_command)
    return cleaned


async def _get_schedule_info(sid: str) -> dict | None:
    """查询日程详情（用于删除时生成系统消息）"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT type, trigger_at, content FROM schedules WHERE id=?", (sid,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def _add_schedule(stype: str, trigger_at: str, content: str, origin: str = "aion", origin_room_id: str = ""):
    sid = f"sch_{int(time.time()*1000)}"
    now = time.time()
    trigger_at = trigger_at.replace("T", " ")
    async with get_db() as db:
        await db.execute(
            "INSERT INTO schedules (id, type, trigger_at, content, created_at, status, origin, origin_room_id) VALUES (?,?,?,?,?,?,?,?)",
            (sid, stype, trigger_at, content, now, "active", origin, origin_room_id),
        )
        await db.commit()
    await manager.broadcast({"type": "schedule_changed"})


async def _del_schedule(sid: str):
    async with get_db() as db:
        changed = await finish_schedule(db, sid, "cancelled")
        await db.commit()
    if changed:
        await manager.broadcast({"type": "schedule_changed"})
    return changed


# ── 获取活跃日程（供 prompt 注入） ────────────────
async def get_active_schedules() -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, type, trigger_at, content, origin, origin_room_id FROM schedules WHERE status='active' ORDER BY trigger_at",
        )
        return [dict(r) for r in await cur.fetchall()]


def build_schedule_prompt(schedules: list[dict]) -> str:
    """构建注入 prompt 的日程列表文本"""
    if not schedules:
        return "暂无日程"
    type_map = {"alarm": ("🔔", "闹铃"), "reminder": ("📋", "日程"), "monitor": ("👁", "监督")}
    lines = []
    for s in schedules:
        icon, label = type_map.get(s["type"], ("📋", "日程"))
        origin_name = get_schedule_origin_name(s.get("origin"))
        lines.append(f"- {icon} 【{origin_name}】设定了{label} #{s['id']}: {s['trigger_at'].replace('T', ' ')} — {s['content']}")
    return "\n".join(lines)


# ── 单例 ──────────────────────────────────────────
schedule_mgr = ScheduleManager()
