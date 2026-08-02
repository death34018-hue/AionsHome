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
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from memory import recall_memories
from music import search_songs, get_audio_url
from routes.music import MUSIC_CMD_PATTERN
from tts import TTSStreamer

log = logging.getLogger("schedule")
BACKGROUND_CLI_META = {"antigravity_print_timeout": "90s"}


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
ALARM_CMD = re.compile(r"\[ALARM:(.+?)\|(.+?)\]")
REMINDER_CMD = re.compile(r"\[REMINDER:(.+?)\|(.+?)\]")
MONITOR_CMD = re.compile(r"\[Monitor:(.+?)\|(.+?)\]")
SCHEDULE_DEL_CMD = re.compile(r"\[SCHEDULE_DEL:(.+?)\]")
SCHEDULE_LIST_CMD = re.compile(r"\[SCHEDULE_LIST\]")


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
            try:
                asyncio.run_coroutine_threadsafe(self._tick(), self._loop).result(timeout=60)
            except Exception as e:
                log.error("schedule tick error: %s", e)
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

    async def _save_to_private(self, conv_id: str, sys_content: str, ai_text: str, ai_msg_id: str, att_json: str, music_atts: list):
        """将系统消息和 AI 回复保存到 Aion 私聊"""
        now = time.time()
        sys_msg_id = f"msg_{int(now*1000)}_st"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (sys_msg_id, conv_id, "system", sys_content, now, "[]"),
            )
            await db.commit()
        sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
                   "content": sys_content, "created_at": now, "attachments": []}
        await manager.broadcast({"type": "msg_created", "data": sys_msg})

        now2 = time.time()
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (ai_msg_id, conv_id, "assistant", ai_text, now2, att_json),
            )
            await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
            await db.commit()
        ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant",
                  "content": ai_text, "created_at": now2, "attachments": music_atts}
        await manager.broadcast({"type": "msg_created", "data": ai_msg})

        from routes.files import export_conversation
        await export_conversation(conv_id)

    async def _save_to_chatroom(self, room_id: str, sender: str, sys_content: str, ai_text: str, ai_msg_id: str, att_json: str, music_atts: list):
        """将系统消息和 AI 回复保存到聊天室（群聊/Connor 私聊）"""
        now = time.time()
        sys_msg_id = f"cm_{int(now*1000)}_sys"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (sys_msg_id, room_id, "system", sys_content, now, "[]"),
            )
            await db.commit()
        sys_msg = {"id": sys_msg_id, "room_id": room_id, "sender": "system",
                   "content": sys_content, "created_at": now, "attachments": []}
        await manager.broadcast({"type": "chatroom_msg_created", "data": sys_msg})

        now2 = time.time()
        async with get_db() as db:
            await db.execute(
                "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (ai_msg_id, room_id, sender, ai_text, now2, att_json),
            )
            await db.execute("UPDATE chatroom_rooms SET updated_at=? WHERE id=?", (now2, room_id))
            await db.commit()
        ai_msg = {"id": ai_msg_id, "room_id": room_id, "sender": sender,
                  "content": ai_text, "created_at": now2, "attachments": music_atts}
        await manager.broadcast({"type": "chatroom_msg_created", "data": ai_msg})

    # ── 触发闹铃 ─────────────────────────────────
    async def _fire_alarm(self, item: dict):
        sid = item["id"]
        content = item["content"]
        trigger_at = item["trigger_at"]
        origin = item.get("origin", "aion")
        origin_name = get_schedule_origin_name(origin)
        log.info("firing alarm %s: %s @%s (origin=%s)", sid, content, trigger_at, origin)

        # 标记为已触发
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE schedules SET status='triggered' WHERE id=?", (sid,))
            await db.commit()

        # 广播给前端弹窗
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
                f"[日程闹铃触发]\n"
                f"日程内容：{trigger_at} — {content}\n"
                f"现在时间已经到了（当前 {now_str}），请提醒【{user_name}】。"
                f"{heart_rate_block}"
            )
            messages_ctx.append({"role": "user", "content": trigger_prompt})
            messages = messages_ctx
        else:
            # Aion 来源 → 沿用原有逻辑
            from context_builder import fetch_merged_timeline, render_merged_timeline
            merged = await fetch_merged_timeline("aion", 20, conv_id=conv_id)
            history = render_merged_timeline(merged, "aion")

            prefix = []
            if wb.get("ai_persona"):
                prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
                prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
            if wb.get("user_persona"):
                prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
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
            ability_block = "[系统能力] 你可以在回复中根据对话氛围，善用以下指令：\n" + "\n".join(f"{i+1}. {a}" for i, a in enumerate(abilities))

            active_schedules = await get_active_schedules()
            schedule_text = build_schedule_prompt(active_schedules)
            ability_block += f"\n\n【当前日程列表】\n{schedule_text}"

            cap_idx = len(prefix) if prefix else 0
            history.insert(cap_idx, {"role": "user", "content": ability_block})
            history.insert(cap_idx + 1, {"role": "assistant", "content": "好的，需要时我会使用这些指令。"})

            trigger_prompt = (
                f"[日程闹铃触发]\n"
                f"日程内容：{trigger_at} — {content}\n"
                f"现在时间已经到了（当前 {now_str}），请提醒【{user_name}】。"
                f"{heart_rate_block}"
            )

            recalled, _ = await recall_memories(trigger_prompt[:300])
            mem_inject = []
            if recalled:
                mem_lines = "\n".join([f"- {m['content']}" for m in recalled])
                mem_inject = [
                    {"role": "user", "content": f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"},
                    {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"},
                ]

            messages = prefix + mem_inject + history + [{"role": "user", "content": trigger_prompt}]

        # 预生成 ai_msg_id（TTS 分段文件命名需要）
        ai_msg_id = f"msg_{int(time.time()*1000)}_sa"

        # Connor 来源时根据配置的模型调用
        if origin == "connor":
            from chatroom import stream_connor_cli, load_chatroom_config as _lcc_cr
            from ai_providers import CLI_STATUS_PREFIX as _CSP

            _connor_model = (_lcc_cr().get("connor_model") or "Codex").strip() or "Codex"

            alarm_tts = None
            tts_voice = _tts_voice_for_target(is_chatroom, "connor")
            if tts_voice:
                alarm_tts = TTSStreamer(ai_msg_id, tts_voice, manager)

            full_text = ""
            try:
                if _connor_model == "Codex":
                    async for chunk in stream_connor_cli(messages=messages):
                        if chunk.startswith(_CSP):
                            continue
                        full_text += chunk
                        if alarm_tts:
                            alarm_tts.feed(chunk)
                else:
                    _temp = SETTINGS.get("temperature")
                    async for chunk in stream_ai(messages, _connor_model, meta=BACKGROUND_CLI_META, temperature=_temp):
                        if chunk.startswith(CLI_STATUS_PREFIX):
                            continue
                        full_text += chunk
                        if alarm_tts:
                            alarm_tts.feed(chunk)
            except Exception as e:
                full_text = f"[闹铃提醒回复失败] {e}"
        else:
            # Aion 来源用常规 stream_ai
            alarm_tts = None
            tts_voice = _tts_voice_for_target(is_chatroom, "aion")
            if tts_voice:
                alarm_tts = TTSStreamer(ai_msg_id, tts_voice, manager)

            full_text = ""
            try:
                _temp = SETTINGS.get("temperature")
                async for chunk in stream_ai(messages, model_key, meta=BACKGROUND_CLI_META, temperature=_temp):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        continue
                    full_text += chunk
                    if alarm_tts:
                        alarm_tts.feed(chunk)
            except Exception as e:
                full_text = f"[闹铃提醒回复失败] {e}"

        if not full_text.strip():
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

        music_atts = [{"type": "music", "name": s["name"], "artist": s["artist"], "id": s["id"]} for s in music_cards] if music_cards else []
        att_json = json.dumps(music_atts, ensure_ascii=False) if music_atts else "[]"

        # ── 保存到目标窗口 ──
        sys_content = f"⏰ 日程闹铃触发：{content}"
        if is_chatroom:
            await self._save_to_chatroom(target["room_id"], sender, sys_content, full_text, ai_msg_id, att_json, music_atts)
        else:
            await self._save_to_private(conv_id, sys_content, full_text, ai_msg_id, att_json, music_atts)

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
            await db.execute("UPDATE schedules SET status='triggered' WHERE id=?", (sid,))
            await db.commit()
        await manager.broadcast({"type": "schedule_changed"})

        # ── 确定响应目标 ──
        target = self._resolve_target(item)
        is_chatroom = target["type"] == "chatroom"
        sender = "connor" if origin == "connor" else "aion"

        # 尝试截图（优先摄像头+屏幕，摄像头未开启时仅截取屏幕+手机）
        from camera import cam
        fname = None
        # 播放提示音 + 5秒延迟，给用户反应时间
        await manager.broadcast({"type": "monitor_alert", "data": {"content": content, "origin": origin, "origin_name": origin_name}})
        await asyncio.sleep(5)

        jpg_bytes = cam.get_frame_jpeg() or cam.get_screen_only_jpeg()
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
                prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
                prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
            if wb.get("user_persona"):
                prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
                prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

            if prefix:
                prefix[-1]["content"] += f"\n系统当前的准确时间是 {now_str}"

            abilities = []
            abilities.append("[MUSIC:歌曲名 歌手名] — 点歌/推荐音乐。系统自动展示播放卡片并自动播放，不要在指令外重复歌曲信息。可同时用多个。")
            abilities.append("[ALARM:YYYY-MM-DDTHH:MM|内容] — 设置闹铃，到时间系统会主动提醒用户。日期时间用ISO格式。")
            abilities.append("[REMINDER:YYYY-MM-DD|内容] — 设置日程提醒（不闹铃），你在合适时机自然提起即可。")
            abilities.append(f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时监督。到时间后系统自动截取摄像头画面发送给你，你可以查看{user_name}的状态。")
            abilities.append("[SCHEDULE_DEL:日程id] — 删除指定日程/闹铃/定时监控。")
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

        # 预生成 ai_msg_id（TTS 分段文件命名需要）
        ai_msg_id = f"msg_{int(time.time()*1000)}_sm"

        # Connor 来源时根据配置的模型调用
        if origin == "connor":
            from chatroom import stream_connor_cli, load_chatroom_config as _lcc_cr
            from ai_providers import CLI_STATUS_PREFIX as _CSP

            _connor_model = (_lcc_cr().get("connor_model") or "Codex").strip() or "Codex"

            monitor_tts = None
            tts_voice = _tts_voice_for_target(is_chatroom, "connor")
            if tts_voice:
                monitor_tts = TTSStreamer(ai_msg_id, tts_voice, manager)

            full_text = ""
            try:
                if _connor_model == "Codex":
                    async for chunk in stream_connor_cli(messages=messages):
                        if chunk.startswith(_CSP):
                            continue
                        full_text += chunk
                        if monitor_tts:
                            monitor_tts.feed(chunk)
                else:
                    _temp = SETTINGS.get("temperature")
                    async for chunk in stream_ai(messages, _connor_model, meta=BACKGROUND_CLI_META, temperature=_temp):
                        if chunk.startswith(CLI_STATUS_PREFIX):
                            continue
                        full_text += chunk
                        if monitor_tts:
                            monitor_tts.feed(chunk)
            except Exception as e:
                full_text = f"[定时监控回复失败] {e}"
        else:
            monitor_tts = None
            tts_voice = _tts_voice_for_target(is_chatroom, "aion")
            if tts_voice:
                monitor_tts = TTSStreamer(ai_msg_id, tts_voice, manager)

            full_text = ""
            try:
                _temp = SETTINGS.get("temperature")
                async for chunk in stream_ai(messages, model_key, meta=BACKGROUND_CLI_META, temperature=_temp):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        continue
                    full_text += chunk
                    if monitor_tts:
                        monitor_tts.feed(chunk)
            except Exception as e:
                full_text = f"[定时监控回复失败] {e}"

        if not full_text.strip():
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

        music_atts = [{"type": "music", "name": s["name"], "artist": s["artist"], "id": s["id"]} for s in music_cards] if music_cards else []
        att_json = json.dumps(music_atts, ensure_ascii=False) if music_atts else "[]"

        # ── 保存到目标窗口 ──
        if origin == "connor":
            from chatroom import load_chatroom_config
            _cname = load_chatroom_config().get("connor_name", "AI")
            sys_content = f"{_cname}查看了监控"
        else:
            sys_content = f"{ai_name}查看了监控"
        if is_chatroom:
            await self._save_to_chatroom(target["room_id"], sender, sys_content, full_text, ai_msg_id, att_json, music_atts)
        else:
            await self._save_to_private(conv_id, sys_content, full_text, ai_msg_id, att_json, music_atts)

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
                await _del_schedule(sid)
                if conv_id and info:
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
        await db.execute("UPDATE schedules SET status='cancelled' WHERE id=?", (sid,))
        await db.commit()
    await manager.broadcast({"type": "schedule_changed"})


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
