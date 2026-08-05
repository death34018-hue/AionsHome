"""
聊天室 API 路由：房间 CRUD、发消息(SSE)、AI 互聊、记忆接口
"""

import json, time, asyncio, random, re, mimetypes
from typing import Optional, List, Dict
from pathlib import Path
from datetime import date

import aiosqlite, httpx
from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import DEFAULT_MODEL, DATA_DIR, CODEX_UPLOADS_DIR, MODELS, SETTINGS, get_sentinel_config, resolve_model_key
from database import get_db
from ws import manager
from active_window_state import record_chatroom_active
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from tts import TTSStreamer, synthesize_message_tts_later
from message_dedup import build_message_dedupe_key, reserve_message_ingress
from sync_events import append_sync_event, attach_sync_seq, broadcast_synced
from chatroom import (
    send_to_connor, check_connor_online, load_chatroom_config, save_chatroom_config,
    get_chatroom_names,
    build_aion_group_context, build_connor_group_context,
    build_connor_1v1_context, get_main_chat_recent, format_cross_context,
    recall_chatroom_memories, recall_main_chat_memories, save_chatroom_memory,
    digest_chatroom, connor_1v1_on_message, _CONNOR_TIMEOUT_SENTINEL,
    stream_connor_cli,
)
from context_builder import (
    MUSIC_CMD_PATTERN, MOMENT_CMD_PATTERN, MEMORY_CMD_PATTERN,
    ACTIVITY_CHECK_PATTERN, SELFIE_CMD_PATTERN, DRAW_CMD_PATTERN, SONG_CMD_PATTERN,
    POI_SEARCH_PATTERN, TOY_CMD_PATTERN, PET_CMD_PATTERN,
    PRIVATE_WHISPER_CMD_PATTERN, VIDEO_CALL_CMD, META_TAG_PATTERN, strip_tool_commands,
    WISH_CMD_PATTERN,
    append_message_meta,
)
from memory import (
    get_embedding, _pack_embedding, memory_kind_for_type, memory_kind_label,
    _memory_time_payload, instant_digest,
)
from schedule import (
    process_schedule_commands, ALARM_CMD, REMINDER_CMD, MONITOR_CMD,
    SCHEDULE_DEL_CMD, SCHEDULE_LIST_CMD, _parse_dt, _is_schedule_time_stale,
)
from music import search_songs, get_audio_url
from camera import cam, CAM_CHECK_CMD
from luckin import handle_luckin_commands, luckin_payment_attachments
from link_preview import build_link_preview_attachments
from song_gen import clean_song_visible_reply
from stream_reply import resolve_stream_failure
from stream_safety import (
    CHAT_STREAM_POLICY,
    StreamSafetyResult,
    consume_safe_stream,
)
from band_commands import process_band_vibration, with_band_vibration_attachment
from hug_pillow_commands import process_hug_pillow_commands
from app_supervision_ai import (
    queue_app_supervision_reply_command,
    broadcast_app_supervision_command,
)
from wechat_bridge import (
    dispatch_wechat_message,
    process_wechat_outbound_commands,
    record_wechat_route,
)
from web_search import (
    WEB_EXTRACT_CMD_PATTERN,
    WEB_SEARCH_CMD_PATTERN,
    WebCommandStreamFilter,
    clean_web_command_text,
    format_web_system_message,
    run_web_commands,
)

router = APIRouter(prefix="/api/chatroom", tags=["chatroom"])


async def _consume_chatroom_stream(
    source,
    queue,
    *,
    chunk_type: str,
) -> StreamSafetyResult:
    stream_filter = WebCommandStreamFilter()

    async def on_commit(chunk: str) -> None:
        visible = stream_filter.feed(chunk)
        if visible:
            await queue.put({"type": chunk_type, "content": visible})

    result = await consume_safe_stream(source, CHAT_STREAM_POLICY, on_commit)
    visible_tail = stream_filter.flush()
    if visible_tail:
        await queue.put({"type": chunk_type, "content": visible_tail})
    if result.notice:
        await queue.put({
            "type": chunk_type,
            "content": f"\n\n[{result.notice}]",
        })
    return result


def _done_streaming_response() -> StreamingResponse:
    async def generate():
        if False:
            yield ""

    return StreamingResponse(generate(), media_type="text/event-stream")


def _chatroom_msg_from_row(row) -> dict:
    msg = dict(row)
    try:
        msg["attachments"] = json.loads(msg.get("attachments") or "[]") if msg.get("attachments") else []
    except Exception:
        msg["attachments"] = []
    return msg


def _dedupe_chatroom_attachments(items: list) -> list:
    seen = set()
    out = []
    for item in items or []:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


async def _with_link_previews(content: str, attachments: list | None) -> list:
    att_list = _dedupe_chatroom_attachments(list(attachments or []))
    previews = await build_link_preview_attachments(content, att_list)
    return _dedupe_chatroom_attachments(att_list + previews)

TRANSFER_CMD_PATTERN = re.compile(r'\[转账(?:给([^：:]+?))?[：:]\s*(-?\d+(?:\.\d+)?)\s*元\]')
_STRUCTURED_LINE_RE = re.compile(r"^\s*(```|[-*+]\s+|\d+[.)]\s+|[>|#]|\|)")
ORPHAN_HOME_ARGS_PATTERN = re.compile(
    r'(?im)^\s*[^\n\[]+\|(?:mode|hvac_mode|temperature|temp|fan_mode|fan|swing_mode|swing)\s*=[^\n\]]*\]?\s*$'
)
_AMBIENT_LAST_TRIGGER_BY_ROOM: dict[str, float] = {}
_AMBIENT_GENERATING_ROOMS: set[str] = set()
_AMBIENT_LISTENER_TTL = 18.0
_AMBIENT_ACTIVE_LISTENER: dict = {}


def _normalize_cli_bubble_breaks(text: str, model_key: str | None) -> str:
    """Gemini CLI often returns casual chat paragraphs separated by single LF only."""
    resolved_model = resolve_model_key(model_key or DEFAULT_MODEL)
    if (MODELS.get(resolved_model, {}).get("provider") != "gemini_cli"):
        return text

    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return text

    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    if len(lines) < 2:
        return text
    if any(_STRUCTURED_LINE_RE.match(line) for line in lines):
        return text

    return "\n\n".join(lines)


def _visible_chatroom_text(text: str) -> str:
    """Remove local tool protocol tags and malformed HOME remnants from chatroom replies."""
    if not text:
        return ""

    transfers: list[str] = []

    def _hold_transfer(match: re.Match) -> str:
        transfers.append(match.group(0))
        return f"__AION_CHATROOM_TRANSFER_{len(transfers) - 1}__"

    cleaned = TRANSFER_CMD_PATTERN.sub(_hold_transfer, text)
    cleaned = strip_tool_commands(cleaned)
    for pat in (ALARM_CMD, REMINDER_CMD, MONITOR_CMD, SCHEDULE_DEL_CMD, SCHEDULE_LIST_CMD):
        cleaned = pat.sub("", cleaned)
    cleaned = ORPHAN_HOME_ARGS_PATTERN.sub("", cleaned)
    cleaned = META_TAG_PATTERN.sub("", cleaned).strip()

    for idx, transfer in enumerate(transfers):
        cleaned = cleaned.replace(f"__AION_CHATROOM_TRANSFER_{idx}__", transfer)
    return cleaned.strip()


def _chatroom_auto_tts_voice(sender: str) -> str:
    if sender not in ("aion", "connor"):
        return ""
    cfg = load_chatroom_config()
    if not cfg.get("tts_enabled"):
        return ""
    key = "tts_aion_voice" if sender == "aion" else "tts_connor_voice"
    return (cfg.get(key) or "").strip()


# ══════════════════════════════════════════════════
#  语音附件预处理（与 routes/chat.py 相同逻辑）
# ══════════════════════════════════════════════════

def _process_voice_attachments(history: list):
    """处理上下文中的语音附件：转写文本注入 content，最后一条用户消息保留音频 URL，其余移除。"""
    # 只允许最新一条 user 消息保留附件；不能重复发送更早的语音。
    keep_idx = -1
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            keep_idx = i
            break

    for i, msg in enumerate(history):
        atts = msg.get("attachments", [])
        if not atts:
            continue
        is_kept = (i == keep_idx)
        media_transcripts = []
        non_media_atts = []
        for att in atts:
            if isinstance(att, dict) and att.get("type") == "voice":
                transcript = att.get("transcript", "")
                if transcript:
                    media_transcripts.append(f"[语音消息] {transcript}")
                if is_kept:
                    non_media_atts.append(att.get("url", ""))
            elif isinstance(att, dict) and att.get("type") == "video_clip":
                transcript = att.get("transcript", "")
                if transcript:
                    media_transcripts.append(f"[视频通话] {transcript}")
                if is_kept:
                    non_media_atts.append(att.get("url", ""))
            else:
                if is_kept:
                    non_media_atts.append(att)
        if media_transcripts:
            vt = "\n".join(media_transcripts)
            orig = msg["content"].strip() if msg.get("content") else ""
            msg["content"] = vt + (f"\n{orig}" if orig else "")
        if is_kept:
            msg["attachments"] = non_media_atts
        else:
            msg.pop("attachments", None)


# ══════════════════════════════════════════════════
#  群聊工具指令处理
# ══════════════════════════════════════════════════

async def _chatroom_sys_msg(room_id: str, text: str, _q: asyncio.Queue, after_msg_id: str = None):
    """在聊天室中插入系统消息气泡"""
    now = time.time()
    msg_id = f"cm_{time.time_ns()}_sys"
    order_atts = [{"type": "system_model_context"}]
    if after_msg_id:
        order_atts.append({"type": "system_notice_order", "after_msg_id": after_msg_id})
    att_json = json.dumps(order_atts, ensure_ascii=False) if order_atts else "[]"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, room_id, "system", text, now, att_json),
        )
        await db.commit()
    msg = {"id": msg_id, "room_id": room_id, "sender": "system", "content": text, "created_at": now, "attachments": order_atts}
    await _q.put({"type": "system_msg", "message": msg})
    await broadcast_synced(manager, {"type": "chatroom_msg_created", "data": msg})
    return msg


async def _append_system_message_attachment(
    room_id: str,
    msg_id: str,
    attachment: dict | None,
) -> dict | None:
    if not room_id or not msg_id or not attachment:
        return None
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT content, created_at, attachments
            FROM chatroom_messages
            WHERE id=? AND room_id=? AND sender='system'
            """,
            (msg_id, room_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        try:
            attachments = json.loads(row[2] or "[]")
        except (TypeError, json.JSONDecodeError):
            attachments = []
        if not isinstance(attachments, list):
            attachments = []
        if attachment not in attachments:
            attachments.append(attachment)
            await db.execute(
                "UPDATE chatroom_messages SET attachments=? WHERE id=? AND room_id=?",
                (
                    json.dumps(attachments, ensure_ascii=False),
                    msg_id,
                    room_id,
                ),
            )
            await db.commit()
    msg = {
        "id": msg_id,
        "room_id": room_id,
        "sender": "system",
        "content": row[0],
        "created_at": row[1],
        "attachments": attachments,
    }
    await broadcast_synced(
        manager,
        {"type": "chatroom_msg_updated", "data": msg},
    )
    return msg


def _name_for_identity(identity: str) -> str:
    user_name, ai_name, connor_name = get_chatroom_names()
    return {"user": user_name, "aion": ai_name, "connor": connor_name}.get(identity, identity)


def _prefix_for_sender(sender: str) -> str:
    if sender in ("aion", "connor"):
        return f"[{_name_for_identity(sender)}] "
    return ""


async def _save_main_memory_from_chatroom(room_id: str, msg_id: str, content: str) -> dict:
    """Save an Aion-authored chatroom MEMORY command into the main Aion memory table."""
    mem_now = time.time()
    mem_id = f"mem_{int(mem_now * 1000)}"
    vec = await get_embedding(content)
    async with get_db() as mem_db:
        await mem_db.execute(
            "INSERT INTO memories "
            "(id, content, type, created_at, source_conv, embedding, keywords, importance, "
            "source_start_ts, source_end_ts, unresolved, source_msg_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mem_id, content, "重要事件", mem_now, f"chatroom:{room_id}",
                _pack_embedding(vec) if vec else None, "", 0.5,
                None, None, 0, msg_id,
            ),
        )
        await mem_db.commit()
    return {
        "id": mem_id,
        "content": content,
        "type": "重要事件",
        "created_at": mem_now,
        "keywords": "",
        "importance": 0.5,
        "source_start_ts": None,
        "source_end_ts": None,
        "source_msg_id": msg_id,
        "memory_kind": memory_kind_for_type("重要事件"),
        "memory_kind_label": memory_kind_label("重要事件"),
    }


def _render_recent_room_messages_for_ai(msgs: list[dict]) -> list[dict]:
    """把聊天室临时上下文渲染为带说话人名字和精确时间 meta 的历史记录。

    这里不要把 Aion/Connor 的历史发言渲染成 assistant 消息，否则模型在监控/动态等
    二次回复里容易把输入误当成多人剧本，继续输出 [Aion]/[Connor] 前缀。
    """
    recent = []
    for m in msgs:
        sender = m.get("sender", "")
        if sender == "system":
            continue
        name = "系统事件" if sender == "system" else _name_for_identity(sender)
        text = f"历史消息 - {name}：{m.get('content') or ''}"
        content = append_message_meta(text, m.get("created_at"), "聊天室")
        recent.append({"role": "user", "content": content})
    return recent


async def _get_room_type(room_id: str) -> str:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT type FROM chatroom_rooms WHERE id=?", (room_id,))
        row = await cur.fetchone()
    return row["type"] if row else ""


async def _get_or_create_aion_private_conv() -> str:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1")
        row = await cur.fetchone()
        if row:
            return row["id"]

        now = time.time()
        conv_id = f"conv_{time.time_ns()}"
        await db.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            (conv_id, "悄悄话", DEFAULT_MODEL, now, now),
        )
        await db.commit()

    conv = {"id": conv_id, "title": "悄悄话", "model": DEFAULT_MODEL, "created_at": now, "updated_at": now}
    await manager.broadcast({"type": "conv_created", "data": conv})
    return conv_id


async def _send_aion_private_whisper(content: str):
    content = content.strip()
    if not content:
        return

    conv_id = await _get_or_create_aion_private_conv()
    now = time.time()
    msg_id = f"msg_{time.time_ns()}_whisper"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", content, now, "[]"),
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant", "content": content, "created_at": now, "attachments": []}
    await broadcast_synced(manager, {"type": "msg_created", "data": msg})


async def _get_or_create_connor_private_room() -> str:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM chatroom_rooms WHERE type = 'connor_1v1' ORDER BY updated_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
        if row:
            return row["id"]

        now = time.time()
        room_id = f"cr_{time.time_ns()}"
        connor_name = _name_for_identity("connor")
        title = f"和 {connor_name} 私聊"
        await db.execute(
            "INSERT INTO chatroom_rooms (id, title, type, aion_persona, connor_persona, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (room_id, title, "connor_1v1", "", "", now, now),
        )
        await db.commit()

    room = {
        "id": room_id, "title": title, "type": "connor_1v1",
        "aion_persona": "", "connor_persona": "",
        "context_minutes": 30, "ai_chat_rounds": 1,
        "created_at": now, "updated_at": now, "message_count": 0,
    }
    await manager.broadcast({"type": "chatroom_room_created", "data": room})
    return room_id


async def _send_connor_private_whisper(content: str):
    content = content.strip()
    if not content:
        return

    private_room_id = await _get_or_create_connor_private_room()
    await _save_msg(private_room_id, "connor", content, msg_id=f"cm_{time.time_ns()}_w")


async def _send_private_whisper(who_identity: str, content: str):
    if who_identity == "connor":
        await _send_connor_private_whisper(content)
    else:
        await _send_aion_private_whisper(content)


async def _process_chatroom_commands(full_text: str, room_id: str, who: str, msg_id: str, _q: asyncio.Queue) -> tuple[str, dict]:
    """处理 AI 回复中的工具指令，执行副作用，返回 (清理后的文本, 触发的后续动作信息)。
    who: 内部身份 aion 或 connor"""
    from ws import manager as ws_manager
    triggered = {}  # 收集需要后续处理的动作
    who_identity = "connor" if who.lower() == "connor" else "aion"
    who_label = _name_for_identity(who_identity)

    full_text, supervision_command = await queue_app_supervision_reply_command(
        full_text,
        source_message_id=msg_id,
        role_id=who_identity,
        source_kind="chatroom",
        source_ref=room_id,
    )
    if supervision_command:
        triggered["app_supervision_command"] = supervision_command
        await broadcast_app_supervision_command(supervision_command)

    # ── 群聊悄悄话：只在 group 房间生效，内容投递到各自私聊窗口 ──
    private_whispers = PRIVATE_WHISPER_CMD_PATTERN.findall(full_text)
    if private_whispers:
        full_text = PRIVATE_WHISPER_CMD_PATTERN.sub("", full_text)
        if await _get_room_type(room_id) == "group":
            for whisper_text in private_whispers:
                try:
                    await _send_private_whisper(who_identity, whisper_text)
                    print(f"[CHATROOM_WHISPER] {who_label} -> private: {whisper_text[:80]}")
                except Exception as e:
                    print(f"[CHATROOM_WHISPER] 发送失败: {e}")

    # ── 微信消息：隐藏本地指令，插入系统记录，并交给微信发送出口 ──
    async def _save_wechat_system(system_text: str):
        await _chatroom_sys_msg(room_id, system_text, _q, after_msg_id=msg_id)

    full_text, wechat_messages = await process_wechat_outbound_commands(
        full_text,
        source_type="chatroom",
        source_id=room_id,
        sender=who_identity,
        source_msg_id=msg_id,
        save_system_message=_save_wechat_system,
        send_wechat_message=dispatch_wechat_message,
        record_route=record_wechat_route,
    )
    if wechat_messages:
        triggered["wechat_messages"] = wechat_messages

    full_text = await process_band_vibration(
        full_text,
        source_type="chatroom",
        source_id=room_id,
        source_msg_id=msg_id,
        sender=who_identity,
    )
    full_text = await process_hug_pillow_commands(
        full_text,
        source_type="chatroom",
        source_id=room_id,
        source_msg_id=msg_id,
        sender=who_identity,
        sender_name=who_label,
        save_system_message=lambda text: _chatroom_sys_msg(
            room_id, text, _q, after_msg_id=msg_id
        ),
    )

    # ── 点歌 ──
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
        full_text = MUSIC_CMD_PATTERN.sub("", full_text)

    if music_cards:
        parts = [f"《{s['name']}》- {s['artist']}" for s in music_cards]
        await _chatroom_sys_msg(room_id, f"🎵 {who_label}点了一首{' / '.join(parts)}", _q, after_msg_id=msg_id)
        music_data = {"type": "music", "msg_id": msg_id, "cards": music_cards, "autoplay": True}
        triggered["music_cards"] = music_cards
        await _q.put(music_data)
        await ws_manager.broadcast({"type": "music", "data": {**music_data, "source": "chatroom"}})

    # ── 日程/闹钟（先检测指令生成系统消息，再交给 schedule 模块处理） ──
    for match in ALARM_CMD.finditer(full_text):
        try:
            raw_dt, content = match.group(1), match.group(2)
            dt = _parse_dt(raw_dt)
            if dt and content.strip() and not _is_schedule_time_stale(dt):
                await _chatroom_sys_msg(room_id, f"⏰ 【{who_label}】设定了闹铃：{dt.replace('T', ' ')}，内容：{content.strip()}", _q, after_msg_id=msg_id)
        except Exception:
            pass
    for match in REMINDER_CMD.finditer(full_text):
        try:
            raw_dt, content = match.group(1), match.group(2)
            dt = _parse_dt(raw_dt)
            if dt and content.strip() and not _is_schedule_time_stale(dt):
                await _chatroom_sys_msg(room_id, f"📅 【{who_label}】设定了日程：{dt.replace('T', ' ')}，内容：{content.strip()}", _q, after_msg_id=msg_id)
        except Exception:
            pass
    for match in MONITOR_CMD.finditer(full_text):
        try:
            raw_dt, content = match.group(1), match.group(2)
            dt = _parse_dt(raw_dt)
            if dt and content.strip() and not _is_schedule_time_stale(dt):
                await _chatroom_sys_msg(room_id, f"👀 【{who_label}】设定了监督：{dt.replace('T', ' ')}，内容：{content.strip()}", _q, after_msg_id=msg_id)
        except Exception:
            pass
    _origin = "connor" if who.lower() == "connor" else "aion"
    full_text = await process_schedule_commands(full_text, None, origin=_origin, origin_room_id=room_id)

    # ── 智能家居 ──
    from routes.chat import _process_home_commands
    full_text = await _process_home_commands(full_text)

    full_text, luckin_results = await handle_luckin_commands(full_text)
    if luckin_results:
        luckin_atts = luckin_payment_attachments(luckin_results)
        if luckin_atts:
            triggered["luckin_payment"] = luckin_atts
        if any(item.get("ok") for item in luckin_results):
            await _chatroom_sys_msg(room_id, f"{who_label}创建了瑞幸咖啡订单，等待支付确认", _q, after_msg_id=msg_id)
        else:
            await _chatroom_sys_msg(room_id, f"{who_label}的瑞幸咖啡下单未完成", _q, after_msg_id=msg_id)

    # ── 查岗 ──
    cam_triggered = CAM_CHECK_CMD in full_text
    if cam_triggered:
        full_text = full_text.replace(CAM_CHECK_CMD, "")
        cam_notice = await _chatroom_sys_msg(
            room_id,
            f"📷 {who_label}查看了监控",
            _q,
            after_msg_id=msg_id,
        )
        triggered["cam_check"] = cam_notice["id"]

    # ── 查看动态 ──
    activity_match = ACTIVITY_CHECK_PATTERN.search(full_text)
    if activity_match:
        try:
            activity_n = int(activity_match.group(1))
        except (ValueError, IndexError):
            activity_n = 6
        activity_n = max(1, min(12, activity_n)) if activity_n > 0 else 6
        full_text = ACTIVITY_CHECK_PATTERN.sub("", full_text)
        await _chatroom_sys_msg(room_id, f"📊 {who_label}查看了用户动态", _q, after_msg_id=msg_id)
        triggered["activity"] = activity_n

    # ── 联网搜索 / 网页读取 ──
    web_search_matches = WEB_SEARCH_CMD_PATTERN.findall(full_text)
    web_extract_matches = WEB_EXTRACT_CMD_PATTERN.findall(full_text)
    if web_search_matches or web_extract_matches:
        full_text = clean_web_command_text(full_text)
        await _chatroom_sys_msg(
            room_id,
            format_web_system_message(who_label, web_search_matches, web_extract_matches),
            _q,
            after_msg_id=msg_id,
        )
        triggered["web_search"] = {"searches": web_search_matches, "extracts": web_extract_matches}

    # ── MOMENT (朋友圈) ──
    moment_matches = MOMENT_CMD_PATTERN.findall(full_text)
    if moment_matches:
        full_text = MOMENT_CMD_PATTERN.sub("", full_text)
        for mt_content, mt_reply in moment_matches:
            mt_content = mt_content.strip()
            if mt_content:
                try:
                    mt_now = time.time()
                    mt_id = f"mt_{int(mt_now*1000)}"
                    # who 是显示名，需要转为内部标识
                    author = "connor" if who.lower() == "connor" else "aion"
                    expect = 1 if mt_reply == "true" else 0
                    async with get_db() as mt_db:
                        await mt_db.execute(
                            "INSERT INTO moments (id, author, content, source_conv, source_msg_id, expect_reply, created_at) VALUES (?,?,?,?,?,?,?)",
                            (mt_id, author, mt_content, f"chatroom:{room_id}", msg_id, expect, mt_now)
                        )
                        await mt_db.commit()
                    mt_data = {"type": "moment_new", "data": {
                        "id": mt_id, "author": author, "content": mt_content,
                        "expect_reply": expect, "created_at": mt_now,
                        "comments": [], "reactions": [],
                    }}
                    await _q.put(mt_data)
                    await broadcast_synced(ws_manager, mt_data)
                    if expect:
                        from routes.moments import _trigger_ai_replies
                        asyncio.create_task(_trigger_ai_replies(mt_id, exclude_author=author))
                except Exception as e:
                    print(f"[CHATROOM_MOMENT] 发布失败: {e}")

    # ── MEMORY ──
    memory_matches = MEMORY_CMD_PATTERN.findall(full_text)
    if memory_matches:
        full_text = MEMORY_CMD_PATTERN.sub("", full_text)
        for mem_content in memory_matches:
            mem_content = mem_content.strip()
            if mem_content:
                try:
                    if who_identity == "aion":
                        mem_data = await _save_main_memory_from_chatroom(room_id, msg_id, mem_content)
                        await broadcast_synced(ws_manager, {"type": "memory_added", "data": mem_data})
                        mem_id = mem_data["id"]
                        target_label = f"{who_label}记忆库"
                    else:
                        # Connor 的显式记忆始终进入聊天室/Connor 记忆库；群聊中也按 Connor 作用域保存。
                        mem_id = await save_chatroom_memory(
                            room_id=room_id, scope="connor", content=mem_content,
                            keywords="", importance=0.5,
                        )
                        target_label = f"{who_label}记忆库"
                    await _chatroom_sys_msg(room_id, f"💾 {who_label}记住了：{mem_content[:50]}", _q, after_msg_id=msg_id)
                    mr_data = {
                        "type": "memory_record",
                        "msg_id": msg_id,
                        "content": mem_content,
                        "mem_id": mem_id,
                        "store": "main" if who_identity == "aion" else "chatroom",
                    }
                    await _q.put(mr_data)
                    await ws_manager.broadcast({"type": "memory_record", "data": mr_data})
                    print(f"[CHATROOM_MEMORY] {who_label} -> {target_label}: {mem_content[:80]}")
                except Exception as e:
                    print(f"[CHATROOM_MEMORY] 录入失败: {e}")

    # ── 许愿池 ──
    wish_matches = WISH_CMD_PATTERN.findall(full_text)
    if wish_matches:
        full_text = WISH_CMD_PATTERN.sub("", full_text)
        try:
            from wish_pool import create_wish
            for wish_content in wish_matches:
                wish_content = wish_content.strip()
                if not wish_content:
                    continue
                await create_wish(
                    author=who_identity,
                    content=wish_content,
                    visibility="shared",
                    origin="chat_command",
                    source_type="chatroom_command",
                    source_ref=f"{room_id}:{msg_id}",
                )
                print(f"[CHATROOM_WISH] {who_label}: {wish_content[:80]}")
        except Exception as e:
            print(f"[CHATROOM_WISH] 许愿失败: {e}")

    # ── POI 搜索 ──
    poi_matches = POI_SEARCH_PATTERN.findall(full_text)
    if poi_matches:
        full_text = POI_SEARCH_PATTERN.sub("", full_text)
        triggered["poi"] = poi_matches

    # ── 玩具 ──
    toy_matches = TOY_CMD_PATTERN.findall(full_text)
    if toy_matches:
        full_text = TOY_CMD_PATTERN.sub("", full_text)
        triggered["toy_commands"] = toy_matches
        toy_data = {"type": "toy_command", "commands": toy_matches, "msg_id": msg_id}
        await _q.put(toy_data)
        await ws_manager.broadcast({"type": "toy_command", "data": toy_data})

    # ── 桌宠 ──
    pet_matches = PET_CMD_PATTERN.findall(full_text)
    if pet_matches:
        full_text = PET_CMD_PATTERN.sub("", full_text)
        await ws_manager.broadcast({"type": "pet_command", "data": {"action": pet_matches[-1].lower()}})

    # ── 钱包转账（AI 侧） ──
    transfer_matches = TRANSFER_CMD_PATTERN.findall(full_text)
    for t_recipient, t_amount_str in transfer_matches:
        try:
            t_val = float(t_amount_str)
            if t_val > 0:
                if who.lower() == "connor":
                    async with get_db() as t_db:
                        t_now = time.time()
                        t_id = f"cwt_{int(t_now*1000)}"
                        await t_db.execute(
                            "INSERT INTO bookkeeping (id, record_type, amount, description, created_at) VALUES (?,?,?,?,?)",
                            (t_id, 'connor_wallet_ai', -t_val, f'{who_label}转账给用户 {t_val}元', t_now)
                        )
                        await t_db.commit()
                    await ws_manager.broadcast({"type": "connor_wallet_update"})
                    print(f"[CONNOR_WALLET] {who_label} 转账: -{t_val}元")
                elif who.lower() == "aion":
                    async with get_db() as t_db:
                        t_now = time.time()
                        t_id = f"wt_{int(t_now*1000)}"
                        await t_db.execute(
                            "INSERT INTO bookkeeping (id, record_type, amount, description, created_at) VALUES (?,?,?,?,?)",
                            (t_id, 'wallet_ai', -t_val, f'{who_label}转账给用户 {t_val}元', t_now)
                        )
                        await t_db.commit()
                    await ws_manager.broadcast({"type": "wallet_update"})
                    print(f"[WALLET] {who_label} 转账: -{t_val}元")
        except (ValueError, Exception):
            pass

    # ── 图片生成 ──
    selfie_match = SELFIE_CMD_PATTERN.search(full_text)
    draw_match = DRAW_CMD_PATTERN.search(full_text)
    if selfie_match:
        triggered["image_gen"] = {"prompt": selfie_match.group(1).strip(), "is_selfie": True}
        full_text = SELFIE_CMD_PATTERN.sub("", full_text)
    elif draw_match:
        triggered["image_gen"] = {"prompt": draw_match.group(1).strip(), "is_selfie": False}
        full_text = DRAW_CMD_PATTERN.sub("", full_text)

    # ── 视频通话 ──
    song_match = SONG_CMD_PATTERN.search(full_text)
    if song_match:
        song_prompt = None
        if SETTINGS.get("song_gen_enabled", False):
            prompt = song_match.group(1).strip()
            if prompt:
                triggered["song_gen"] = {"prompt": prompt}
                song_prompt = prompt
        full_text = SONG_CMD_PATTERN.sub("", full_text)
        if song_prompt:
            full_text = clean_song_visible_reply(full_text)

    if VIDEO_CALL_CMD in full_text:
        full_text = full_text.replace(VIDEO_CALL_CMD, "")

    # 清理 META 标签
    full_text = META_TAG_PATTERN.sub("", full_text)

    return _visible_chatroom_text(full_text), triggered


def _toy_attachments_from_triggered(triggered: dict) -> list[dict]:
    commands = triggered.get("toy_commands") or []
    if not commands:
        return []
    return [{"type": "toy", "commands": commands}]


def _music_attachments_from_triggered(triggered: dict) -> list[dict]:
    cards = triggered.get("music_cards") or []
    if not cards:
        return []
    attachments = []
    for song in cards:
        if not isinstance(song, dict):
            continue
        item = {
            "type": "music",
            "id": song.get("id"),
            "name": song.get("name", ""),
            "artist": song.get("artist", ""),
            "album": song.get("album", ""),
            "cover": song.get("cover", ""),
        }
        if song.get("audio_url"):
            item["audio_url"] = song.get("audio_url")
        candidates = []
        for candidate in song.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            candidates.append({
                "id": candidate.get("id"),
                "name": candidate.get("name", ""),
                "artist": candidate.get("artist", ""),
                "album": candidate.get("album", ""),
                "cover": candidate.get("cover", ""),
            })
        if candidates:
            item["candidates"] = candidates
        attachments.append(item)
    return attachments


def _luckin_attachments_from_triggered(triggered: dict) -> list[dict]:
    attachments = triggered.get("luckin_payment") or []
    return [item for item in attachments if isinstance(item, dict)]


# ══════════════════════════════════════════════════
#  群聊工具指令后续动作（异步执行）
# ══════════════════════════════════════════════════

def _fire_chatroom_followups(triggered: dict, room_id: str, sender: str, model_key: str, trigger_msg_id: str | None = None):
    """根据 _process_chatroom_commands 返回的 triggered dict，启动异步后续任务"""
    if triggered.get("cam_check"):
        asyncio.create_task(
            _chatroom_cam_check(
                room_id,
                sender,
                model_key,
                system_msg_id=str(triggered["cam_check"]),
            )
        )
    if triggered.get("activity"):
        asyncio.create_task(_chatroom_activity_check(room_id, sender, model_key, triggered["activity"]))
    if triggered.get("poi"):
        asyncio.create_task(_chatroom_poi_check(room_id, sender, model_key, triggered["poi"]))
    if triggered.get("web_search"):
        asyncio.create_task(_chatroom_web_search(room_id, sender, model_key, triggered["web_search"]))
    if triggered.get("image_gen"):
        ig = triggered["image_gen"]
        asyncio.create_task(_chatroom_image_gen(room_id, sender, ig["prompt"], ig["is_selfie"]))
    if triggered.get("song_gen"):
        sg = triggered["song_gen"]
        asyncio.create_task(_chatroom_song_gen(room_id, sender, sg["prompt"], trigger_msg_id))


async def _broadcast_chatroom_ai_status(room_id: str, sender: str, text: str):
    await manager.broadcast({
        "type": "chatroom_ai_status",
        "data": {"room_id": room_id, "sender": sender, "text": text},
    })


async def _chatroom_cam_check(
    room_id: str,
    sender: str,
    model_key: str,
    delay: float = 5.0,
    system_msg_id: str = "",
):
    """聊天室版监控查看：播放提示音 → 延迟截图 → AI 追加回复到聊天室"""
    from config import load_worldbook, SETTINGS, UPLOADS_DIR, SCREENSHOTS_DIR
    from camera import cam, build_monitor_alert_data

    # 播放摄像头调起提示音，给用户反应时间
    await manager.broadcast({
        "type": "monitor_alert",
        "data": build_monitor_alert_data("监控查看"),
    })
    if cam.cfg.get("active_source", "local") != "phone":
        await asyncio.sleep(delay)

    await _broadcast_chatroom_ai_status(room_id, sender, "正在获取监控画面...")

    from camera import acquire_monitor_image, save_monitor_camera_snapshot
    image_result = await acquire_monitor_image(
        "ai_cam_check",
        force_pc_screen=True,
    )
    snapshot_attachment = save_monitor_camera_snapshot(
        image_result.camera_jpeg,
        "chatroom_cam_check",
    )
    if system_msg_id:
        if snapshot_attachment:
            await _append_system_message_attachment(
                room_id,
                system_msg_id,
                snapshot_attachment,
            )
    else:
        system_atts = [{"type": "system_model_context"}]
        if snapshot_attachment:
            system_atts.append(snapshot_attachment)
        notice = await _save_msg(
            room_id,
            "system",
            f"📷 {_name_for_identity(sender)}查看了监控",
            attachments=system_atts,
            auto_tts=False,
        )
        system_msg_id = notice["id"]
    jpg_bytes = image_result.jpeg
    frame_source = image_result.source
    fname = None
    if jpg_bytes:
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"cam_check_{ts}_{time.time_ns()}.jpg"
        fpath = UPLOADS_DIR / fname
        fpath.write_bytes(jpg_bytes)
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        (SCREENSHOTS_DIR / fname).write_bytes(jpg_bytes)

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")

    # 获取聊天室最近消息作为上下文
    _, msgs = await _load_room_and_messages(room_id, limit=10)
    recent = _render_recent_room_messages_for_ai(msgs)

    cam_prompt = (
        f"你刚才想看看{user_name}在干什么，这是系统抓取的实时监控画面。"
        f"画面可能包含摄像头、电脑屏幕和手机屏幕；如果没有摄像头画面，就只根据电脑/手机屏幕内容说明设备使用情况，不要推断身体位置。"
        f"请根据画面内容，自然地描述你看到的情况并和{user_name}互动。"
        f"不需要再说\"让我看看\"之类的话，直接说你看到了什么。"
    )
    if frame_source == "device":
        cam_prompt += "本次没有可用摄像头画面，系统改用电脑屏幕和/或手机屏幕截图。"
    if image_result.no_image_context:
        cam_prompt += image_result.no_image_context

    prefix_msgs = []
    if wb.get("ai_persona") and sender == "aion":
        prefix_msgs.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
        prefix_msgs.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix_msgs.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
        prefix_msgs.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

    cam_message = {"role": "user", "content": cam_prompt}
    if fname:
        cam_message["attachments"] = [f"/uploads/{fname}"]
    messages = prefix_msgs + recent + [cam_message]

    full_text = ""
    tts_from_model = True
    try:
        if sender == "aion":
            _temp = SETTINGS.get("temperature")
            async for chunk in stream_ai(messages, model_key, temperature=_temp):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    await _broadcast_chatroom_ai_status(room_id, sender, chunk[len(CLI_STATUS_PREFIX):])
                    continue
                full_text += chunk
        else:
            async for chunk in _stream_connor_model(messages, model_key):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    await _broadcast_chatroom_ai_status(room_id, sender, chunk[len(CLI_STATUS_PREFIX):])
                    continue
                full_text += chunk
    except Exception as e:
        resolution = resolve_stream_failure(full_text, e, "监控查看失败")
        full_text = resolution.visible_text
        tts_from_model = resolution.had_partial_text

    if not full_text.strip():
        await _save_msg(room_id, "system", "监控画面已获取，但模型没有返回分析结果。", auto_tts=False)
        return

    from schedule import _process_background_reply_commands
    reply_msg_id = f"cm_{time.time_ns()}_{sender[:1]}"
    full_text = await _process_background_reply_commands(
        full_text,
        target={"type": "chatroom", "room_id": room_id},
        conv_id=None,
        sender=sender,
        ai_msg_id=reply_msg_id,
    )
    full_text = _normalize_cli_bubble_breaks(_visible_chatroom_text(full_text), model_key)
    await _save_msg(room_id, sender, full_text, msg_id=reply_msg_id, auto_tts=tts_from_model)
    print(f"[CHATROOM_CAM_CHECK] {sender} 查看监控完成, room={room_id}")


async def _chatroom_activity_check(room_id: str, sender: str, model_key: str, n: int):
    """聊天室版查看动态：获取摘要 → AI 追加回复到聊天室"""
    from activity import get_activity_summary_for_prompt
    from config import load_worldbook, SETTINGS

    n = max(1, min(12, n))
    summary_text = get_activity_summary_for_prompt(n)
    if not summary_text:
        summary_text = "（当前没有设备活动记录）"

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    minutes = n * 10

    # 获取聊天室最近消息作为上下文
    _, msgs = await _load_room_and_messages(room_id, limit=10)
    recent = _render_recent_room_messages_for_ai(msgs)

    activity_prompt = (
        f"你刚才想了解{user_name}最近在干什么，以下是系统采集到的{user_name}过去{minutes}分钟的设备使用动态（每10分钟一条摘要）：\n\n"
        f"【设备活动动态】\n{summary_text}\n\n"
        f"请根据这些动态信息，自然地和{user_name}聊聊。不需要再说\"让我看看\"之类的话，直接根据动态内容回应即可。"
    )

    # 构建 prompt
    prefix_msgs = []
    if wb.get("ai_persona") and sender == "aion":
        prefix_msgs.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
        prefix_msgs.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix_msgs.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
        prefix_msgs.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

    messages = prefix_msgs + recent + [{"role": "user", "content": activity_prompt}]

    full_text = ""
    tts_from_model = True
    try:
        if sender == "aion":
            _temp = SETTINGS.get("temperature")
            async for chunk in stream_ai(messages, model_key, temperature=_temp):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
        else:
            async for chunk in _stream_connor_model(messages, model_key):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
    except Exception as e:
        resolution = resolve_stream_failure(full_text, e, "查看动态失败")
        full_text = resolution.visible_text
        tts_from_model = resolution.had_partial_text

    if not full_text.strip():
        return

    from schedule import _process_background_reply_commands
    reply_msg_id = f"cm_{time.time_ns()}_{sender[:1]}"
    full_text = await _process_background_reply_commands(
        full_text,
        target={"type": "chatroom", "room_id": room_id},
        conv_id=None,
        sender=sender,
        ai_msg_id=reply_msg_id,
    )
    full_text = _normalize_cli_bubble_breaks(_visible_chatroom_text(full_text), model_key)
    await _save_msg(room_id, sender, full_text, msg_id=reply_msg_id, auto_tts=tts_from_model)
    print(f"[CHATROOM_ACTIVITY] {sender} 查看动态完成, room={room_id}, n={n}")


async def _chatroom_poi_check(room_id: str, sender: str, model_key: str, categories: list[str]):
    """聊天室版 POI 搜索：搜索周边 → AI 追加回复到聊天室"""
    from location import (
        load_location_config, load_location_status, save_location_status,
        amap_poi_search, amap_regeo, format_location_for_prompt,
    )
    from config import load_worldbook, SETTINGS

    cfg = load_location_config()
    amap_key = cfg.get("amap_key", "")
    if not amap_key:
        return

    status = load_location_status()
    lng = status.get("lng", 0)
    lat = status.get("lat", 0)
    if not lng or not lat:
        return

    geo_info = await amap_regeo(lng, lat, amap_key)
    if geo_info:
        status["address"] = geo_info["address"]
        status["adcode"] = geo_info["adcode"]

    search_results = {}
    poi_types = cfg.get("poi_types", {})
    for cat in categories:
        cat = cat.strip()
        type_code = poi_types.get(cat)
        if type_code:
            pois = await amap_poi_search(lng, lat, type_code, amap_key, cfg.get("poi_radius", 2000))
            search_results[cat] = pois
            if "nearby_pois" not in status:
                status["nearby_pois"] = {}
            status["nearby_pois"][cat] = pois

    status["last_api_lng"] = lng
    status["last_api_lat"] = lat
    save_location_status(status)

    if not search_results:
        return

    result_lines = []
    for cat, pois in search_results.items():
        if not pois:
            result_lines.append(f"【{cat}】附近暂无相关结果")
            continue
        result_lines.append(f"【{cat}】")
        for p in pois[:10]:
            entry = f"  - {p['name']}"
            if p.get("distance"):
                entry += f"（{int(p['distance'])}m）"
            if p.get("rating") and p["rating"] != "[]":
                entry += f" ⭐{p['rating']}"
            if p.get("cost") and p["cost"] != "[]":
                entry += f" 人均¥{p['cost']}"
            if p.get("address") and p["address"] != "[]":
                entry += f" | {p['address']}"
            result_lines.append(entry)
    poi_text = "\n".join(result_lines)

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    loc_prompt = format_location_for_prompt()
    poi_prompt = (
        f"你刚才想帮{user_name}搜索周边信息，以下是系统根据{user_name}最新实时坐标搜索到的结果：\n\n"
        f"{poi_text}\n\n"
        f"{loc_prompt}\n\n"
        f"请根据搜索结果，自然地向{user_name}推荐或回答。不需要再说\"让我帮你搜一下\"之类的话，直接根据结果回复即可。"
    )

    _, msgs = await _load_room_and_messages(room_id, limit=10)
    recent = _render_recent_room_messages_for_ai(msgs)

    prefix_msgs = []
    if wb.get("ai_persona") and sender == "aion":
        prefix_msgs.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
        prefix_msgs.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})

    messages = prefix_msgs + recent + [{"role": "user", "content": poi_prompt}]

    full_text = ""
    tts_from_model = True
    try:
        if sender == "aion":
            _temp = SETTINGS.get("temperature")
            async for chunk in stream_ai(messages, model_key, temperature=_temp):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
        else:
            async for chunk in _stream_connor_model(messages, model_key):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
    except Exception as e:
        resolution = resolve_stream_failure(full_text, e, "周边搜索完成但回复生成失败")
        full_text = resolution.visible_text
        tts_from_model = resolution.had_partial_text

    if not full_text.strip():
        return

    from schedule import _process_background_reply_commands
    reply_msg_id = f"cm_{time.time_ns()}_{sender[:1]}"
    full_text = await _process_background_reply_commands(
        full_text,
        target={"type": "chatroom", "room_id": room_id},
        conv_id=None,
        sender=sender,
        ai_msg_id=reply_msg_id,
    )
    full_text = _normalize_cli_bubble_breaks(_visible_chatroom_text(full_text), model_key)
    await _save_msg(room_id, sender, full_text, msg_id=reply_msg_id, auto_tts=tts_from_model)
    searched_cats = "、".join(c.strip() for c in categories)
    print(f"[CHATROOM_POI] {sender} 搜索完成, room={room_id}, categories={searched_cats}")


async def _chatroom_web_search(room_id: str, sender: str, model_key: str, payload: dict):
    """聊天室版联网搜索：Tavily 搜索/读取网页 → 触发者追加回复到聊天室"""
    from config import load_worldbook, SETTINGS

    searches = payload.get("searches") or []
    extracts = payload.get("extracts") or []
    if not searches and not extracts:
        return

    try:
        context_items = await run_web_commands(searches, extracts)
        web_context = "\n\n".join(item for item in context_items if item.strip()).strip()
    except Exception as e:
        web_context = f"【联网搜索结果】\n系统搜索失败：{e}"

    if not web_context:
        web_context = "【联网搜索结果】\n系统没有拿到可用结果。"

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    sender_label = _name_for_identity(sender)

    _, msgs = await _load_room_and_messages(room_id, limit=10)
    recent = _render_recent_room_messages_for_ai(msgs)

    prefix_msgs = []
    if wb.get("ai_persona") and sender == "aion":
        prefix_msgs.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
        prefix_msgs.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix_msgs.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
        prefix_msgs.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

    web_prompt = (
        f"{sender_label}刚才为了回答{user_name}发起了联网搜索或网页读取，系统已经完成。以下是结果：\n\n"
        f"{web_context}\n\n"
        f"请以{sender_label}自己的口吻，根据这些结果自然回答{user_name}。如果信息不足，请说明不足；不要编造来源。"
        "请先自行归纳总结搜索结果，只提供与当前问题或分享主题直接相关的关键信息。"
        "请像平时聊天一样自然表达，不要写成搜索报告，不要逐条复述搜索结果，也不要长篇大论。"
        f"除非确实必须继续核实，否则不要再次输出 [WEB_SEARCH:...] 或 [WEB_EXTRACT:...]。"
    )
    messages = prefix_msgs + recent + [{"role": "user", "content": web_prompt}]

    full_text = ""
    tts_from_model = True
    try:
        if sender == "aion":
            _temp = SETTINGS.get("temperature")
            async for chunk in stream_ai(messages, model_key, temperature=_temp):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
        else:
            async for chunk in _stream_connor_model(messages, model_key):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
    except Exception as e:
        resolution = resolve_stream_failure(full_text, e, "联网搜索完成但回复生成失败")
        full_text = resolution.visible_text
        tts_from_model = resolution.had_partial_text

    if not full_text.strip():
        return

    from schedule import _process_background_reply_commands
    reply_msg_id = f"cm_{time.time_ns()}_{sender[:1]}"
    full_text = await _process_background_reply_commands(
        full_text,
        target={"type": "chatroom", "room_id": room_id},
        conv_id=None,
        sender=sender,
        ai_msg_id=reply_msg_id,
    )
    full_text = _normalize_cli_bubble_breaks(_visible_chatroom_text(clean_web_command_text(full_text)), model_key)
    if not full_text.strip():
        return
    await _save_msg(room_id, sender, full_text, msg_id=reply_msg_id, auto_tts=tts_from_model)
    print(f"[CHATROOM_WEB_SEARCH] {sender} 搜索完成, room={room_id}, searches={len(searches)}, extracts={len(extracts)}")


async def _chatroom_image_gen(room_id: str, sender: str, prompt: str, is_selfie: bool):
    """聊天室版图片生成"""
    from image_gen import generate_image

    try:
        filename = await generate_image(prompt, is_selfie=is_selfie, source_identity=sender)
        if filename:
            await _save_msg(room_id, sender, "", attachments=[f"/uploads/{filename}"])
            print(f"[CHATROOM_IMG_GEN] {sender} 生图完成, room={room_id}")
        else:
            print(f"[CHATROOM_IMG_GEN] {sender} 生图失败, room={room_id}")
    except Exception as e:
        print(f"[CHATROOM_IMG_GEN] {sender} 生图异常: {e}")


# ── 图片 URL 检测 & 下载保存 ──
async def _chatroom_song_gen(room_id: str, sender: str, prompt: str, trigger_msg_id: str | None = None):
    """Generate a song for chatroom messages."""
    from song_gen import generate_song

    event_data = {"room_id": room_id, "sender": sender, "msg_id": trigger_msg_id}
    await manager.broadcast({"type": "chatroom_song_gen_start", "data": event_data})
    try:
        result = await generate_song(prompt)
        if result:
            title = result.get("title") or "AI 生成歌曲"
            attachment = {
                "type": "generated_song",
                "url": result.get("url"),
                "title": title,
                "mime_type": result.get("mime_type", "audio/mpeg"),
                "model": result.get("model", "lyria-3-pro-preview"),
            }
            if result.get("lyrics"):
                attachment["lyrics"] = result["lyrics"]
            if result.get("prompt"):
                attachment["prompt"] = result["prompt"]
            if result.get("text"):
                attachment["description"] = result["text"]
            song_msg = await _save_msg(
                room_id,
                sender,
                f"为你写的歌《{title}》",
                attachments=[attachment],
                auto_tts=False,
            )
            await manager.broadcast({"type": "chatroom_song_gen_done", "data": {**event_data, "song_msg_id": song_msg.get("id")}})
            print(f"[CHATROOM_SONG_GEN] {sender} song generated, room={room_id}")
        else:
            await manager.broadcast({"type": "chatroom_song_gen_failed", "data": event_data})
            print(f"[CHATROOM_SONG_GEN] {sender} song generation failed, room={room_id}")
    except Exception as e:
        await manager.broadcast({"type": "chatroom_song_gen_failed", "data": event_data})
        print(f"[CHATROOM_SONG_GEN] {sender} song generation error: {e}")


_IMG_URL_RE = re.compile(r'(https?://\S+\.(?:jpg|jpeg|png|gif|webp)(?:\?\S*)?)', re.IGNORECASE)
_MD_IMG_RE = re.compile(r'!\[.*?\]\((https?://\S+?)\)')

ALLOWED_IMG_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
ALLOWED_AUDIO_TYPES = {'audio/webm', 'audio/wav', 'audio/mp4', 'audio/mpeg', 'audio/ogg', 'audio/x-wav'}
ALLOWED_UPLOAD_TYPES = ALLOWED_IMG_TYPES | ALLOWED_AUDIO_TYPES


def _cr_upload_dir() -> Path:
    """返回当天的聊天室图片目录 Connor-Codex/uploads/YYYY-MM-DD/"""
    day_dir = CODEX_UPLOADS_DIR / date.today().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir


async def _extract_and_save_images(text: str) -> list[str]:
    """从文本中提取图片 URL，下载并保存到本地，返回本地 URL 列表"""
    urls = set(_IMG_URL_RE.findall(text)) | set(_MD_IMG_RE.findall(text))
    if not urls:
        return []
    saved = []
    day_dir = _cr_upload_dir()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                ct = resp.headers.get("content-type", "")
                if not ct.startswith("image/"):
                    continue
                ext = mimetypes.guess_extension(ct.split(";")[0].strip()) or ".jpg"
                if ext == ".jpe":
                    ext = ".jpg"
                fname = f"{int(time.time()*1000)}{ext}"
                fpath = day_dir / fname
                fpath.write_bytes(resp.content)
                local_url = f"/cr-uploads/{date.today().isoformat()}/{fname}"
                saved.append(local_url)
            except Exception:
                continue
    return saved


_CONNOR_IMG_TAG_RE = re.compile(r'\[\[image:/uploads/')


def _rewrite_connor_paths(text: str) -> str:
    """将 Connor 回复中的 [[image:/uploads/...]] 重写为 [[image:/cr-uploads/...]]
    Connor 端 /uploads/ 对应本地 Connor-Codex/uploads/，
    aion-chat 端挂载在 /cr-uploads/"""
    return _CONNOR_IMG_TAG_RE.sub('[[image:/cr-uploads/', text)


def _attachments_to_connor_images(attachments: list) -> list[dict]:
    """将 /cr-uploads/... 附件列表转为 Connor 需要的 {url, path} 格式"""
    images = []
    for att in (attachments or []):
        url = att if isinstance(att, str) else (att.get("url") or "")
        if not url:
            continue
        # /cr-uploads/2026-05-07/xxx.jpg → Connor-Codex/uploads/2026-05-07/xxx.jpg
        if url.startswith("/cr-uploads/"):
            rel = url[len("/cr-uploads/"):]
            abs_path = str(CODEX_UPLOADS_DIR / rel).replace("/", "\\")
        else:
            abs_path = url
        images.append({"url": url, "path": abs_path})
    return images


def _collect_last_user_images(msgs: list[dict]) -> list[dict]:
    """从消息列表中提取最后一条用户消息的图片，转为 Connor images 格式"""
    for m in reversed(msgs):
        if m.get("sender") == "user":
            atts = m.get("attachments", [])
            if isinstance(atts, str):
                try: atts = json.loads(atts) if atts else []
                except: atts = []
            if atts:
                return _attachments_to_connor_images(atts)
            break
    return []


def _resolve_connor_model(model_key: str | None = None) -> str:
    requested = (model_key or "").strip()
    if requested and (requested != "Codex" or requested in MODELS):
        return requested

    configured = (load_chatroom_config().get("connor_model") or "").strip()
    if configured and configured in MODELS:
        return configured
    if configured and configured != "Codex":
        return configured

    for key, cfg in MODELS.items():
        if cfg.get("provider") == "codex_cli":
            return key
    if requested:
        return requested
    if configured:
        return configured
    return "Codex"


async def _stream_connor_model(messages: list[dict], model_key: str | None = None, meta: dict | None = None):
    """Connor 默认走 Codex CLI；选择其他模型时复用统一模型线路。"""
    key = _resolve_connor_model(model_key)
    if key == "Codex":
        async for chunk in stream_connor_cli(messages=messages, meta=meta):
            yield chunk
    else:
        async for chunk in stream_ai(messages, key, meta if meta is not None else {}):
            yield chunk


def _chatroom_debug_payload(
    *,
    room_id: str,
    model_key: str,
    msg_id: str,
    history: list[dict],
    digest_result: dict | None,
    usage_meta: dict | None = None,
    has_error: bool = False,
    error_text: str | None = None,
) -> dict:
    digest = digest_result or {}
    keywords = digest.get("keywords") or []
    if isinstance(keywords, str):
        keywords_text = keywords
    else:
        keywords_text = "、".join(str(k) for k in keywords if k)

    return {
        "type": "debug",
        "room_id": room_id,
        "model": model_key,
        "msg_id": msg_id,
        "recall_keywords": keywords_text,
        "recall_query": digest.get("recall_query", ""),
        "recall_topic": digest.get("topic", ""),
        "is_search_needed": digest.get("is_search_needed", False),
        "recalled_memories": digest.get("recalled_memories") or [],
        "debug_top6": digest.get("debug_top6") or [],
        "prompt_messages": [
            {"role": m.get("role", ""), "content": str(m.get("content", ""))}
            for m in (history or [])
        ],
        "prompt_count": len(history or []),
        "usage": usage_meta if usage_meta else None,
        "has_error": has_error,
        "error_text": error_text if has_error else None,
    }


async def _emit_chatroom_debug(_q: asyncio.Queue, payload: dict):
    await _q.put(payload)
    await manager.broadcast({"type": "debug", "data": payload})


def _ambient_voice_debug_payload(
    *,
    room_id: str,
    transcript: str,
    decision: dict | None = None,
    skipped: str = "",
    forced: bool = False,
    speaker: str = "",
    has_error: bool = False,
    error_text: str | None = None,
) -> dict:
    scfg = get_sentinel_config()
    clean_transcript = _ambient_excerpt(transcript, 900)
    d = decision or {}
    should_wake = bool(d.get("should_wake")) if decision is not None else False
    return {
        "type": "debug",
        "log_kind": "ambient_voice",
        "room_id": room_id,
        "model": scfg.get("model") or "ambient-sentinel",
        "msg_id": f"ambient_{time.time_ns()}",
        "ambient_forced": forced,
        "ambient_skipped": skipped,
        "ambient_should_wake": should_wake,
        "ambient_speaker": speaker,
        "ambient_source": str(d.get("source_label") or d.get("source") or "")[:80],
        "ambient_topic": str(d.get("topic") or "")[:120],
        "ambient_reason": str(d.get("reason") or skipped or "")[:240],
        "ambient_importance": d.get("importance", 0.0) if decision is not None else 0.0,
        "ambient_summary": str(d.get("summary") or "")[:600],
        "ambient_transcript_chars": len(transcript or ""),
        "prompt_messages": [
            {"role": "user", "content": f"ASR 转写：\n{clean_transcript}"}
        ],
        "prompt_count": 1,
        "usage": None,
        "recalled_memories": [],
        "debug_top6": [],
        "has_error": has_error,
        "error_text": error_text if has_error else None,
    }


async def _emit_ambient_voice_debug(**kwargs):
    payload = _ambient_voice_debug_payload(**kwargs)
    await manager.broadcast({"type": "debug", "data": payload})
    return payload


def _ambient_listener_prune(now: float | None = None) -> bool:
    global _AMBIENT_ACTIVE_LISTENER
    if not _AMBIENT_ACTIVE_LISTENER:
        return False
    ts = float(_AMBIENT_ACTIVE_LISTENER.get("updated_at") or 0)
    now = now or time.time()
    if now - ts <= _AMBIENT_LISTENER_TTL:
        return False
    _AMBIENT_ACTIVE_LISTENER = {}
    return True


def _ambient_listener_state(now: float | None = None) -> dict:
    _ambient_listener_prune(now)
    if not _AMBIENT_ACTIVE_LISTENER:
        return {"active": False, "ttl_seconds": int(_AMBIENT_LISTENER_TTL)}
    state = dict(_AMBIENT_ACTIVE_LISTENER)
    expires_at = float(state.get("updated_at") or 0) + _AMBIENT_LISTENER_TTL
    state["active"] = True
    state["expires_at"] = expires_at
    state["ttl_seconds"] = int(_AMBIENT_LISTENER_TTL)
    return state


async def _ambient_listener_broadcast():
    await manager.broadcast({"type": "ambient_voice_listener", "data": _ambient_listener_state()})


def _clamp_int(value, default: int, min_value: int, max_value: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(min_value, min(max_value, n))


def _extract_json_object(raw: str | None) -> dict:
    text = (raw or "").strip()
    if not text:
        return {}
    if "```" in text:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except Exception:
            return {}
    return {}


def _ambient_excerpt(text: str, max_chars: int = 2400) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[-max_chars:]


async def _judge_ambient_voice(transcript: str) -> dict:
    from memory import _call_sentinel_text

    scfg = get_sentinel_config()
    if not scfg.get("api_key"):
        return {
            "should_wake": False,
            "summary": "",
            "topic": "",
            "reason": "未配置哨兵模型",
            "importance": 0.0,
        }

    user_name, ai_name, connor_name = get_chatroom_names()
    prompt = (
        "你是 AionsHome 的环境语音哨兵。输入来自当前认领前端麦克风的 ASR 转写，可能包含误识别、歌词、叹气、宠物/客人背景声、"
        "用户自言自语、朋友聊天，或电脑外放的 AI/TTS 回声。\n"
        "你的任务是判断是否值得让群聊里的一位 AI 自然插一句。请非常克制，避免把无意义碎片交给大模型。\n\n"
        "判定规则：\n"
        "1. 明确话题、有情绪、有决策、有分享欲、在讨论计划/作品/宠物/家庭动态/关系/烦恼/灵感时，可以 should_wake=true。\n"
        "2. 纯噪声、重复歌词、无上下文感叹、很短的嗯啊、明显电视/电脑外放/AI 回复回声、无法判断主题时 should_wake=false。\n"
        "3. 如果适合唤醒，请只提炼去噪后的摘要，不要保留无关口癖，不要把 ASR 错字当成事实。\n"
        f"4. 当前群聊参与者是：{user_name}、{ai_name}、{connor_name}。摘要应描述“{user_name}刚刚在谈论什么”。\n\n"
        "严格只输出 JSON，不要加解释。格式：\n"
        "{\"should_wake\": boolean, \"topic\": \"短话题\", \"summary\": \"给大模型看的去噪摘要\", "
        "\"reason\": \"短原因\", \"importance\": 0到1}\n\n"
        f"ASR 转写：\n{_ambient_excerpt(transcript)}"
    )
    try:
        raw = await _call_sentinel_text(scfg, prompt, timeout=25)
        data = _extract_json_object(raw)
    except Exception as e:
        print(f"[AMBIENT_VOICE] sentinel failed: {e}")
        return {"should_wake": False, "summary": "", "topic": "", "reason": str(e), "importance": 0.0}

    summary = str(data.get("summary") or "").strip()
    topic = str(data.get("topic") or "").strip()
    reason = str(data.get("reason") or "").strip()
    try:
        importance = float(data.get("importance") or 0.0)
    except (TypeError, ValueError):
        importance = 0.0
    return {
        "should_wake": bool(data.get("should_wake")),
        "summary": summary[:600],
        "topic": topic[:120],
        "reason": reason[:240],
        "importance": max(0.0, min(1.0, importance)),
    }


def _ambient_voice_notice(decision: dict, *, forced: bool) -> str:
    kind = "环境语音即时唤醒" if forced else "环境语音触发"
    summary = (decision.get("summary") or "").strip()
    topic = (decision.get("topic") or "").strip()
    lines = [f"{kind}：{summary or topic or '检测到用户刚刚有一段现场谈话'}"]
    if topic and topic not in lines[0]:
        lines.append(f"话题：{topic}")
    return "\n".join(lines)


def _ambient_voice_prompt(decision: dict, *, forced: bool) -> str:
    user_name, ai_name, connor_name = get_chatroom_names()
    summary = (decision.get("summary") or "").strip()
    topic = (decision.get("topic") or "").strip()
    source_label = (decision.get("source_label") or "当前设备麦克风").strip()
    source = "唤醒词触发的即时侦听" if forced else "环境语音哨兵筛选"
    return (
        "[环境语音]\n"
        f"这是刚刚通过{source_label}听到的现场谈话摘要，来源：{source}。\n"
        f"群聊参与者：{user_name}、{ai_name}、{connor_name}。\n"
        f"话题：{topic or '未命名话题'}\n"
        f"摘要：{summary or topic}\n\n"
        "请把它当作刚刚发生的生活上下文，自然插一句。不要说明你收到了系统提示，不要复述“环境语音”这个来源。"
    )


async def _run_ambient_voice_reply(
    room_id: str,
    speaker: str,
    decision: dict,
    *,
    model_key: str,
    connor_model_key: str,
    tts_enabled: bool,
    tts_aion_voice: str,
    tts_connor_voice: str,
    forced: bool,
):
    if room_id in _AMBIENT_GENERATING_ROOMS:
        return
    _AMBIENT_GENERATING_ROOMS.add(room_id)
    try:
        room, _ = await _load_room_and_messages(room_id)
        if not room or room.get("type") != "group":
            return

        cam.reset_patrol_timer()

        notice = _ambient_voice_notice(decision, forced=forced)
        await _save_msg(room_id, "system", notice, msg_id=f"cm_{time.time_ns()}_av", auto_tts=False)
        _, msgs = await _load_room_and_messages(room_id)

        context_limit = room.get("context_minutes", 30)
        query_text = decision.get("summary") or decision.get("topic") or ""
        ambient_context = _ambient_voice_prompt(decision, forced=forced)
        _q: asyncio.Queue = asyncio.Queue()

        if speaker == "aion":
            await _reply_aion(
                room_id, msgs, context_limit, query_text, model_key, _q,
                tts_enabled=tts_enabled,
                tts_voice=tts_aion_voice,
                ambient_context=ambient_context,
            )
        else:
            await _reply_connor(
                room_id, msgs, context_limit, query_text, _q,
                connor_model_key=connor_model_key,
                tts_enabled=tts_enabled,
                tts_voice=tts_connor_voice,
                ambient_context=ambient_context,
            )
    except Exception as e:
        print(f"[AMBIENT_VOICE] reply failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        _AMBIENT_GENERATING_ROOMS.discard(room_id)


# ── Pydantic 模型 ──

class RoomCreate(BaseModel):
    title: str = "新聊天室"
    type: str = "group"  # "group" | "connor_1v1"


class RoomUpdate(BaseModel):
    title: Optional[str] = None
    context_limit: Optional[int] = None
    # Backward compatibility: the DB column and older clients still use this name,
    # but the value is a message count, not minutes.
    context_minutes: Optional[int] = None
    ai_chat_rounds: Optional[int] = None


class MsgSend(BaseModel):
    content: str
    sender: str = "user"  # "user"
    model: str = DEFAULT_MODEL
    connor_model: Optional[str] = None
    attachments: list = []
    voice_attachments: list = []  # [{type:'voice', url, duration, transcript}]
    tts_enabled: bool = False
    tts_aion_voice: str = ""
    tts_connor_voice: str = ""
    whisper_mode: bool = False


class AiChatTrigger(BaseModel):
    rounds: Optional[int] = None
    model: str = DEFAULT_MODEL
    connor_model: str = "Codex"
    tts_enabled: bool = False
    tts_aion_voice: str = ""
    tts_connor_voice: str = ""


class ReplyOnceTrigger(BaseModel):
    speaker: str
    model: str = DEFAULT_MODEL
    connor_model: str = "Codex"
    tts_enabled: bool = False
    tts_aion_voice: str = ""
    tts_connor_voice: str = ""
    whisper_mode: bool = False


class AmbientVoiceEvaluate(BaseModel):
    transcript: str
    forced: bool = False
    listener_client_id: str = ""
    listener_source: str = ""
    listener_label: str = ""
    model: str = DEFAULT_MODEL
    connor_model: str = "Codex"
    tts_enabled: bool = False
    tts_aion_voice: str = ""
    tts_connor_voice: str = ""


class AmbientVoiceListenerClaim(BaseModel):
    client_id: str
    room_id: Optional[str] = None
    source: str = "browser"
    label: str = ""
    takeover: bool = False


class AmbientVoiceListenerRelease(BaseModel):
    client_id: str


class MsgEditResend(BaseModel):
    content: str
    model: str = DEFAULT_MODEL
    connor_model: str = "Codex"
    tts_enabled: bool = False
    tts_aion_voice: str = ""
    tts_connor_voice: str = ""
    whisper_mode: bool = False


class MsgRegenerate(BaseModel):
    model: str = DEFAULT_MODEL
    connor_model: str = "Codex"
    tts_enabled: bool = False
    tts_aion_voice: str = ""
    tts_connor_voice: str = ""
    whisper_mode: bool = False


class DigestTrigger(BaseModel):
    connor_model: Optional[str] = None


class MessageFeedbackUpdate(BaseModel):
    rating: str
    reason: str


class MemoryCreate(BaseModel):
    content: str
    keywords: str = ""
    importance: float = 0.5
    memory_kind: str = "long_term"
    evidence_summary: str = ""


class MemoryUpdate(BaseModel):
    content: Optional[str] = None
    keywords: Optional[str] = None
    importance: Optional[float] = None
    memory_kind: Optional[str] = None
    unresolved: Optional[int] = None
    evidence_summary: Optional[str] = None


class MemorySourceSelection(BaseModel):
    source_message_ids: List[str] = []


async def _ensure_chatroom_memory_source_column():
    async with get_db() as db:
        try:
            await db.execute("ALTER TABLE chatroom_memories ADD COLUMN source_msg_id TEXT")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE chatroom_memories ADD COLUMN memory_kind TEXT DEFAULT 'long_term'")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE chatroom_memories ADD COLUMN evidence_summary TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE chatroom_memories ADD COLUMN evidence_detail_level TEXT DEFAULT 'summary'")
        except Exception:
            pass
        await db.execute(
            "UPDATE chatroom_memories SET memory_kind='daily' "
            "WHERE (memory_kind IS NULL OR memory_kind='' OR memory_kind='long_term') "
            "AND source_start_ts IS NOT NULL AND source_end_ts IS NOT NULL "
            "AND (source_msg_id IS NULL OR TRIM(source_msg_id)='')"
        )
        await db.commit()


def _json_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return [text]


def _source_ids_for_chatroom_memory(mem) -> list[str]:
    ids = []
    for raw in _json_list(mem["source_msg_id"] if "source_msg_id" in mem.keys() else ""):
        source_id = str(raw).strip()
        if not source_id:
            continue
        if ":" not in source_id:
            source_id = f"chatroom:{source_id}"
        ids.append(source_id)
    return ids


def _chatroom_content_needles(content: str) -> list[str]:
    text = "".join(ch.lower() for ch in content if not ch.isspace())
    needles = set()
    for size in (6, 4):
        for start in range(0, max(0, len(text) - size + 1)):
            part = text[start:start + size]
            if len(part) == size and not part.isdigit():
                needles.add(part)
        if len(needles) >= 12:
            break
    return list(needles)[:24]


def _chatroom_keyword_list(value: str) -> list[str]:
    parts = []
    for raw in _json_list(value):
        text = str(raw).strip().lower()
        if len(text) >= 2:
            parts.append(text)
    if isinstance(value, str) and not parts:
        for raw in value.replace("，", ",").split(","):
            text = raw.strip().lower()
            if len(text) >= 2:
                parts.append(text)
    return parts


def _chatroom_source_score(mem, row, keywords: list[str], needles: list[str]) -> float:
    text = (row.get("content") or "").lower()
    if not text:
        return 0.0
    score = 0.0
    for kw in keywords:
        if kw and kw in text:
            score += 4.0
    for needle in needles:
        if needle and needle in text:
            score += 1.0
    mem_text = (mem["content"] if "content" in mem.keys() else "").lower()
    if mem_text and text and (mem_text in text or text in mem_text):
        score += 3.0
    return score


async def _fetch_chatroom_source_rows_by_ids(source_ids: list[str]) -> list[dict]:
    rows = []
    user_name, ai_name, connor_name = get_chatroom_names()
    name_map = {"user": user_name, "aion": ai_name, "connor": connor_name}
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        for source_id in source_ids:
            if ":" not in source_id:
                continue
            prefix, raw_id = source_id.split(":", 1)
            if prefix != "chatroom":
                continue
            cur = await db.execute(
                "SELECT id, sender, content, created_at FROM chatroom_messages WHERE id=? AND sender != 'system'",
                (raw_id,),
            )
            row = await cur.fetchone()
            if row:
                rows.append({
                    "id": f"chatroom:{row['id']}",
                    "role": "assistant" if row["sender"] in ("aion", "connor") else "user",
                    "name": name_map.get(row["sender"], row["sender"]),
                    "content": row["content"],
                    "created_at": row["created_at"],
                })
    return rows


class ConfigUpdate(BaseModel):
    connor_url: Optional[str] = None
    connor_poll_interval: Optional[float] = None
    connor_poll_timeout: Optional[int] = None
    connor_name: Optional[str] = None
    connor_persona: Optional[str] = None
    connor_persona_sections: Optional[Dict[str, str]] = None
    connor_persona_evolution_enabled: Optional[bool] = None
    connor_persona_extra: Optional[str] = None
    connor_persona_extra_enabled: Optional[bool] = None
    tts_enabled: Optional[bool] = None
    tts_aion_voice: Optional[str] = None
    tts_connor_voice: Optional[str] = None
    reply_order: Optional[str] = None
    connor_model: Optional[str] = None
    aion_model: Optional[str] = None
    ambient_voice_enabled: Optional[bool] = None
    ambient_voice_wake_word: Optional[str] = None
    ambient_voice_stop_word: Optional[str] = None
    ambient_voice_min_chars: Optional[int] = None
    ambient_voice_interval_seconds: Optional[int] = None
    ambient_voice_cooldown_seconds: Optional[int] = None


class ChatroomSettingsUpdate(ConfigUpdate):
    title: Optional[str] = None
    context_limit: Optional[int] = None
    ai_chat_rounds: Optional[int] = None


def _apply_config_update(cfg: dict, body: ConfigUpdate) -> dict:
    values = body.model_dump(exclude_none=True)
    allowed = {
        "connor_url", "connor_poll_interval", "connor_poll_timeout", "connor_name",
        "connor_persona", "connor_persona_sections", "connor_persona_evolution_enabled",
        "connor_persona_extra", "connor_persona_extra_enabled", "tts_enabled",
        "tts_aion_voice", "tts_connor_voice", "connor_model", "aion_model",
        "ambient_voice_enabled", "ambient_voice_wake_word", "ambient_voice_stop_word",
        "ambient_voice_min_chars", "ambient_voice_interval_seconds", "ambient_voice_cooldown_seconds",
    }
    for key in allowed:
        if key in values:
            cfg[key] = values[key]
    if body.connor_persona is not None and body.connor_persona_sections is None:
        cfg["connor_persona_sections"] = {}
    if body.reply_order in ("aion", "connor", "random", "manual"):
        cfg["reply_order"] = body.reply_order
    if body.connor_model is not None:
        cfg["connor_model"] = body.connor_model or "Codex"
    if body.ambient_voice_wake_word is not None:
        cfg["ambient_voice_wake_word"] = (body.ambient_voice_wake_word or "").strip() or "现在立刻唤醒"
    if body.ambient_voice_stop_word is not None:
        cfg["ambient_voice_stop_word"] = (body.ambient_voice_stop_word or "").strip() or "结束立刻唤醒"
    if body.ambient_voice_min_chars is not None:
        cfg["ambient_voice_min_chars"] = _clamp_int(body.ambient_voice_min_chars, 500, 80, 2000)
    if body.ambient_voice_interval_seconds is not None:
        cfg["ambient_voice_interval_seconds"] = _clamp_int(body.ambient_voice_interval_seconds, 120, 20, 600)
    if body.ambient_voice_cooldown_seconds is not None:
        cfg["ambient_voice_cooldown_seconds"] = _clamp_int(body.ambient_voice_cooldown_seconds, 180, 30, 1800)
    return cfg


# ══════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════

def _public_chatroom_config(cfg: dict) -> dict:
    from config import load_worldbook
    wb = load_worldbook()
    return {
        **cfg,
        "connor_online": None,
        "ai_name": wb.get("ai_name", "AI"),
        "user_name": wb.get("user_name", "你"),
    }

@router.get("/config")
async def get_config():
    return _public_chatroom_config(load_chatroom_config())


@router.put("/config")
async def update_config(body: ConfigUpdate):
    cfg = _apply_config_update(load_chatroom_config(), body)
    save_chatroom_config(cfg)
    if body.ambient_voice_enabled is False:
        global _AMBIENT_ACTIVE_LISTENER
        if _AMBIENT_ACTIVE_LISTENER:
            _AMBIENT_ACTIVE_LISTENER = {}
            await _ambient_listener_broadcast()
    await broadcast_synced(manager, {
        "type": "chatroom_settings_updated",
        "data": {"room_id": None, "config": cfg},
    })
    return {"ok": True, "config": cfg}


@router.get("/ambient-voice/listener")
async def ambient_voice_listener_state():
    return {"ok": True, "listener": _ambient_listener_state()}


@router.post("/ambient-voice/claim")
async def ambient_voice_listener_claim(body: AmbientVoiceListenerClaim):
    global _AMBIENT_ACTIVE_LISTENER
    cfg = load_chatroom_config()
    if not cfg.get("ambient_voice_enabled"):
        return {"ok": False, "claimed": False, "reason": "disabled", "listener": _ambient_listener_state()}

    client_id = (body.client_id or "").strip()[:120]
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id is required")

    now = time.time()
    _ambient_listener_prune(now)
    current = _AMBIENT_ACTIVE_LISTENER
    if current and current.get("client_id") != client_id and not body.takeover:
        return {"ok": True, "claimed": False, "reason": "occupied", "listener": _ambient_listener_state(now)}

    changed = (
        not current
        or current.get("client_id") != client_id
        or current.get("source") != (body.source or "browser").strip()[:40]
        or current.get("label") != (body.label or "").strip()[:80]
        or current.get("room_id") != (body.room_id or "").strip()[:120]
    )
    _AMBIENT_ACTIVE_LISTENER = {
        "client_id": client_id,
        "room_id": (body.room_id or "").strip()[:120],
        "source": (body.source or "browser").strip()[:40],
        "label": (body.label or "").strip()[:80],
        "updated_at": now,
    }
    if changed:
        await _ambient_listener_broadcast()
    return {"ok": True, "claimed": True, "listener": _ambient_listener_state(now)}


@router.post("/ambient-voice/release")
async def ambient_voice_listener_release(body: AmbientVoiceListenerRelease):
    global _AMBIENT_ACTIVE_LISTENER
    client_id = (body.client_id or "").strip()
    released = False
    _ambient_listener_prune()
    if client_id and _AMBIENT_ACTIVE_LISTENER.get("client_id") == client_id:
        _AMBIENT_ACTIVE_LISTENER = {}
        released = True
        await _ambient_listener_broadcast()
    return {"ok": True, "released": released, "listener": _ambient_listener_state()}


@router.get("/connor-status")
async def connor_status():
    online = await check_connor_online()
    return {"online": online}


# ══════════════════════════════════════════════════
#  聊天室图片上传
# ══════════════════════════════════════════════════

@router.post("/upload")
async def chatroom_upload(file: UploadFile = File(...)):
    """聊天室专用上传，保存到 Connor-Codex/uploads/YYYY-MM-DD/"""
    base_type = (file.content_type or "").split(";")[0].strip()
    if base_type not in ALLOWED_UPLOAD_TYPES:
        return {"error": f"不支持的文件类型: {file.content_type}"}
    ext = mimetypes.guess_extension(base_type) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        return {"error": "文件太大，最大 20MB"}
    day_dir = _cr_upload_dir()
    fname = f"{int(time.time()*1000)}{ext}"
    fpath = day_dir / fname
    fpath.write_bytes(content)
    url = f"/cr-uploads/{date.today().isoformat()}/{fname}"
    return {"url": url, "type": file.content_type, "name": file.filename}


# ══════════════════════════════════════════════════
#  房间 CRUD
# ══════════════════════════════════════════════════

@router.get("/rooms")
async def list_rooms():
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT r.*, "
            "(SELECT COUNT(*) FROM chatroom_messages m WHERE m.room_id = r.id) AS message_count "
            "FROM chatroom_rooms r ORDER BY r.updated_at DESC"
        )
        rows = await cur.fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["context_limit"] = item.get("context_minutes", 30)
            result.append(item)
        return result


@router.get("/rooms/{room_id}/settings")
async def get_room_settings(room_id: str):
    """One RTT for the setting panel's authoritative room and global values."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chatroom_rooms WHERE id=?", (room_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="房间不存在")
    room = dict(row)
    room["context_limit"] = room.get("context_minutes", 30)
    return {"room": room, "config": _public_chatroom_config(load_chatroom_config())}


@router.patch("/rooms/{room_id}/settings")
async def patch_room_settings(room_id: str, body: ChatroomSettingsUpdate):
    """Save all visible setting fields in one idempotent request."""
    cfg = _apply_config_update(load_chatroom_config(), body)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chatroom_rooms WHERE id=?", (room_id,))
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="房间不存在")

        sets, values = [], []
        if body.title is not None:
            sets.append("title=?")
            values.append(body.title)
        if body.context_limit is not None:
            sets.append("context_minutes=?")
            values.append(_clamp_int(body.context_limit, 30, 1, 500))
        if body.ai_chat_rounds is not None:
            sets.append("ai_chat_rounds=?")
            values.append(_clamp_int(body.ai_chat_rounds, 1, 1, 20))
        if sets:
            sets.append("updated_at=?")
            values.extend([time.time(), room_id])
            await db.execute(f"UPDATE chatroom_rooms SET {', '.join(sets)} WHERE id=?", values)

        # Atomic file replacement happens before the DB commit.  A failed file
        # write therefore rolls back the room update instead of reporting a
        # misleading partial success.
        cur = await db.execute("SELECT * FROM chatroom_rooms WHERE id=?", (room_id,))
        room = dict(await cur.fetchone())
        room["context_limit"] = room.get("context_minutes", 30)
        public_cfg = _public_chatroom_config(cfg)
        payload = {"room_id": room_id, "room": room, "config": public_cfg}
        sync_event = {"type": "chatroom_settings_updated", "data": payload}
        seq = await append_sync_event(db, sync_event)
        save_chatroom_config(cfg)
        await db.commit()

    # The HTTP save result no longer waits for every WebSocket client.
    asyncio.create_task(manager.broadcast(attach_sync_seq(sync_event, seq)))
    asyncio.create_task(manager.broadcast({"type": "chatroom_room_updated", "data": {
        "id": room_id,
        "title": room.get("title"),
        "context_limit": room.get("context_limit"),
        "context_minutes": room.get("context_minutes"),
        "ai_chat_rounds": room.get("ai_chat_rounds"),
    }}))
    return {"ok": True, **payload}


@router.post("/rooms")
async def create_room(body: RoomCreate):
    now = time.time()
    room_id = f"cr_{int(now * 1000)}"

    async with get_db() as db:
        await db.execute(
            "INSERT INTO chatroom_rooms (id, title, type, aion_persona, connor_persona, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (room_id, body.title, body.type, "", "", now, now),
        )
        await db.commit()

    room = {
        "id": room_id, "title": body.title, "type": body.type,
        "aion_persona": "", "connor_persona": "",
        "context_limit": 30, "context_minutes": 30, "ai_chat_rounds": 1,
        "created_at": now, "updated_at": now, "message_count": 0,
    }
    await manager.broadcast({"type": "chatroom_room_created", "data": room})
    return room


@router.put("/rooms/{room_id}")
async def update_room(room_id: str, body: RoomUpdate):
    async with get_db() as db:
        sets, vals = [], []
        for field in ["title", "ai_chat_rounds"]:
            v = getattr(body, field, None)
            if v is not None:
                sets.append(f"{field}=?")
                vals.append(v)
        context_limit = body.context_limit if body.context_limit is not None else body.context_minutes
        if context_limit is not None:
            sets.append("context_minutes=?")
            vals.append(context_limit)
        if sets:
            sets.append("updated_at=?")
            vals.append(time.time())
            vals.append(room_id)
            await db.execute(f"UPDATE chatroom_rooms SET {', '.join(sets)} WHERE id=?", vals)
            await db.commit()
    payload = {"id": room_id, **body.dict(exclude_none=True)}
    if context_limit is not None:
        payload["context_limit"] = context_limit
        payload["context_minutes"] = context_limit
    await manager.broadcast({"type": "chatroom_room_updated", "data": payload})
    return {"ok": True}


@router.delete("/rooms/{room_id}")
async def delete_room(room_id: str):
    async with get_db() as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM chatroom_rooms WHERE id=?", (room_id,))
        # 清理锚点（记忆跨房间共享，不随房间删除）
        await db.execute("DELETE FROM chatroom_digest_anchors WHERE room_id=?", (room_id,))
        await db.commit()
    await manager.broadcast({"type": "chatroom_room_deleted", "data": {"id": room_id}})
    return {"ok": True}


@router.get("/rooms/{room_id}")
async def get_room(room_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chatroom_rooms WHERE id=?", (room_id,))
        row = await cur.fetchone()
        if not row:
            return {"error": "房间不存在"}
        room = dict(row)
        room["context_limit"] = room.get("context_minutes", 30)
        return room


# ══════════════════════════════════════════════════
#  消息
# ══════════════════════════════════════════════════

@router.get("/rooms/{room_id}/messages")
async def list_messages(room_id: str, limit: int = Query(50, ge=1, le=500), before: Optional[float] = Query(None)):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if before:
            cur = await db.execute(
                "SELECT * FROM chatroom_messages WHERE room_id=? AND created_at<? ORDER BY created_at DESC LIMIT ?",
                (room_id, before, limit),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM chatroom_messages WHERE room_id=? ORDER BY created_at DESC LIMIT ?",
                (room_id, limit),
            )
        rows = await cur.fetchall()
        result = []
        for r in reversed(rows):
            d = dict(r)
            d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            result.append(d)
        return result


def _like_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _chatroom_message_dict(row: aiosqlite.Row) -> dict:
    d = dict(row)
    d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
    return d


@router.get("/rooms/{room_id}/messages/search")
async def search_messages(room_id: str, q: str = Query(..., min_length=1, max_length=80), limit: int = Query(50, ge=1, le=100)):
    keyword = q.strip()
    if not keyword:
        return {"items": []}
    like = f"%{_like_escape(keyword)}%"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM chatroom_messages "
            "WHERE room_id=? AND COALESCE(content,'') LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT ?",
            (room_id, like, limit),
        )
        rows = await cur.fetchall()
    return {"items": [_chatroom_message_dict(r) for r in rows]}


@router.get("/rooms/{room_id}/messages/around/{msg_id}")
async def messages_around(room_id: str, msg_id: str, before_count: int = Query(60, ge=10, le=200), after_count: int = Query(30, ge=0, le=100)):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chatroom_messages WHERE room_id=? AND id=?", (room_id, msg_id))
        target = await cur.fetchone()
        if not target:
            return {"ok": False, "error": "message not found", "messages": []}
        target_ts = target["created_at"]
        cur = await db.execute(
            "SELECT * FROM chatroom_messages "
            "WHERE room_id=? AND (created_at<? OR id=?) "
            "ORDER BY created_at DESC LIMIT ?",
            (room_id, target_ts, msg_id, before_count + 1),
        )
        older_rows = list(await cur.fetchall())
        cur = await db.execute(
            "SELECT * FROM chatroom_messages WHERE room_id=? AND created_at>? "
            "ORDER BY created_at ASC LIMIT ?",
            (room_id, target_ts, after_count),
        )
        newer_rows = list(await cur.fetchall())

    older_rows.reverse()
    messages = [_chatroom_message_dict(r) for r in older_rows + newer_rows]
    return {
        "ok": True,
        "target_id": msg_id,
        "messages": messages,
        "has_more_older": len(older_rows) >= before_count + 1,
    }


@router.delete("/messages/{msg_id}")
async def delete_message(msg_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT room_id FROM chatroom_messages WHERE id=?", (msg_id,))
        row = await cur.fetchone()
        if row:
            await db.execute("DELETE FROM chatroom_messages WHERE id=?", (msg_id,))
            await db.commit()
            await broadcast_synced(manager, {"type": "chatroom_msg_deleted", "data": {"id": msg_id, "room_id": row["room_id"]}})
    return {"ok": True}


@router.patch("/messages/{msg_id}/feedback")
async def update_message_feedback(msg_id: str, body: MessageFeedbackUpdate):
    rating = (body.rating or "").strip().lower()
    reason = (body.reason or "").strip()
    if rating not in ("like", "dislike"):
        raise HTTPException(status_code=400, detail="rating must be like or dislike")
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    now = time.time()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT m.*, r.type AS room_type FROM chatroom_messages m "
            "JOIN chatroom_rooms r ON r.id = m.room_id WHERE m.id=?",
            (msg_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="message not found")
        can_rate = (
            row["room_type"] == "group" and row["sender"] in ("aion", "connor")
        ) or (
            row["room_type"] == "connor_1v1" and row["sender"] == "connor"
        )
        if not can_rate:
            raise HTTPException(status_code=400, detail="only AI messages can be rated here")
        created_at = row["ai_feedback_created_at"] or now
        await db.execute(
            "UPDATE chatroom_messages SET ai_feedback_rating=?, ai_feedback_reason=?, "
            "ai_feedback_created_at=?, ai_feedback_updated_at=? WHERE id=?",
            (rating, reason, created_at, now, msg_id),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM chatroom_messages WHERE id=?", (msg_id,))
        updated = await cur.fetchone()
        data = _chatroom_message_dict(updated)
        await broadcast_synced(manager, {"type": "chatroom_msg_updated", "data": data})
    return {"ok": True, "message": data}


# ══════════════════════════════════════════════════
#  发送消息 + AI 回复 (SSE)
# ══════════════════════════════════════════════════

async def _save_msg(
    room_id: str,
    sender: str,
    content: str,
    msg_id: str = None,
    attachments: list = None,
    *,
    auto_tts: bool = True,
    reasoning_content: str = "",
    dedupe_key: str | None = None,
    dedupe_target_type: str = "chatroom",
    dedupe_target_id: str = "",
) -> dict:
    """保存消息到数据库"""
    now = time.time()
    if not msg_id:
        msg_id = f"cm_{int(now * 1000)}_{sender[:1]}"
    att_list = attachments or []
    if sender != "system":
        att_list = await _with_link_previews(content, att_list)
    else:
        att_list = _dedupe_chatroom_attachments(list(att_list))
    att_list = await with_band_vibration_attachment(msg_id, att_list)
    att_json = json.dumps(att_list, ensure_ascii=False) if att_list else "[]"
    duplicate_msg = None
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if dedupe_key:
            duplicate = await reserve_message_ingress(
                db,
                dedupe_key=dedupe_key,
                target_type=dedupe_target_type,
                target_id=dedupe_target_id or room_id,
                message_id=msg_id,
                now=now,
            )
            if duplicate:
                cur = await db.execute("SELECT * FROM chatroom_messages WHERE id=?", (duplicate.get("message_id", ""),))
                row = await cur.fetchone()
                if row:
                    duplicate_msg = _chatroom_msg_from_row(row)
                    duplicate_msg["duplicate"] = True
                else:
                    await db.execute("DELETE FROM message_ingress_dedupe WHERE dedupe_key=?", (dedupe_key,))
                    duplicate = await reserve_message_ingress(
                        db,
                        dedupe_key=dedupe_key,
                        target_type=dedupe_target_type,
                        target_id=dedupe_target_id or room_id,
                        message_id=msg_id,
                        now=now,
                    )
        if not duplicate_msg:
            await db.execute(
                "INSERT INTO chatroom_messages (id, room_id, sender, content, attachments, reasoning_content, created_at) VALUES (?,?,?,?,?,?,?)",
                (msg_id, room_id, sender, content, att_json, reasoning_content, now),
            )
            await db.execute("UPDATE chatroom_rooms SET updated_at=? WHERE id=?", (now, room_id))
        await db.commit()
    if duplicate_msg:
        return duplicate_msg
    msg = {"id": msg_id, "room_id": room_id, "sender": sender, "content": content,
           "created_at": now, "attachments": att_list, "reasoning_content": reasoning_content}
    await broadcast_synced(manager, {"type": "chatroom_msg_created", "data": msg})

    if auto_tts and content.strip():
        voice = _chatroom_auto_tts_voice(sender)
        if voice:
            synthesize_message_tts_later(msg_id, content, voice, manager)

    # Connor 相关消息产生时重置自动总结计时器（私聊和群聊都触发）
    async with get_db() as _db:
        _db.row_factory = aiosqlite.Row
        _cur = await _db.execute("SELECT type FROM chatroom_rooms WHERE id=?", (room_id,))
        _room = await _cur.fetchone()
        if _room and _room["type"] in ("connor_1v1", "group"):
            connor_1v1_on_message()

    return msg


async def _load_room_and_messages(room_id: str, limit: int = 50) -> tuple[dict, list[dict]]:
    """加载房间信息和最近消息"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chatroom_rooms WHERE id=?", (room_id,))
        room = await cur.fetchone()
        if not room:
            return None, []
        room = dict(room)

        cur = await db.execute(
            "SELECT * FROM chatroom_messages WHERE room_id=? ORDER BY created_at DESC LIMIT ?",
            (room_id, limit),
        )
        rows = await cur.fetchall()
        msgs = []
        for r in reversed(rows):
            d = dict(r)
            d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            msgs.append(d)
    return room, msgs


def _is_manual_group_reply_mode() -> bool:
    return load_chatroom_config().get("reply_order", "random") == "manual"


@router.post("/rooms/{room_id}/send")
async def send_message(room_id: str, body: MsgSend):
    """用户发消息，触发 AI 回复"""

    # 保存用户消息（语音消息保存完整附件元数据）
    save_atts = body.voice_attachments if body.voice_attachments else body.attachments
    dedupe_key = build_message_dedupe_key(
        target_type="chatroom",
        target_id=room_id,
        sender="user",
        content=body.content,
        attachments=save_atts,
    )
    user_msg = await _save_msg(
        room_id,
        "user",
        body.content,
        attachments=save_atts,
        dedupe_key=dedupe_key,
        dedupe_target_type="chatroom",
        dedupe_target_id=room_id,
    )
    if user_msg.get("duplicate"):
        duplicate_data = dict(user_msg)
        duplicate_data.pop("duplicate", None)
        await broadcast_synced(manager, {"type": "chatroom_msg_created", "data": duplicate_data})
        return _done_streaming_response()

    # 检测用户消息中的 [转账给XXX：N元] 或 [转账：N元] → 根据收款人路由到对应钱包
    if body.content:
        user_transfer_matches = TRANSFER_CMD_PATTERN.findall(body.content)
        _, ai_name, connor_name = get_chatroom_names()
        for t_recipient, t_amount_str in user_transfer_matches:
            try:
                t_val = float(t_amount_str)
                # 判断收款人：匹配 Aion 名字则进 Aion 钱包，否则默认 Connor 钱包
                recipient_stripped = (t_recipient or "").strip()
                is_aion = recipient_stripped and recipient_stripped == ai_name
                if is_aion:
                    # Aion 钱包
                    async with get_db() as t_db:
                        t_now = time.time()
                        t_id = f"wt_{int(t_now*1000)}"
                        await t_db.execute(
                            "INSERT INTO bookkeeping (id, record_type, amount, description, created_at) VALUES (?,?,?,?,?)",
                            (t_id, 'wallet_user', t_val, f'用户转账给{ai_name} {t_val}元', t_now)
                        )
                        await t_db.commit()
                    await manager.broadcast({"type": "wallet_update"})
                    print(f"[WALLET] 用户转账给{ai_name}: {t_val}元")
                else:
                    # Connor 钱包（默认）
                    async with get_db() as t_db:
                        t_now = time.time()
                        t_id = f"cwt_{int(t_now*1000)}"
                        await t_db.execute(
                            "INSERT INTO bookkeeping (id, record_type, amount, description, created_at) VALUES (?,?,?,?,?)",
                            (t_id, 'connor_wallet_user', t_val, f'用户转账给{connor_name} {t_val}元', t_now)
                        )
                        await t_db.commit()
                    await manager.broadcast({"type": "connor_wallet_update"})
                    print(f"[CONNOR_WALLET] 用户转账给{connor_name}: {t_val}元")
            except (ValueError, Exception):
                pass

    # 加载房间信息
    room, msgs = await _load_room_and_messages(room_id)
    if not room:
        return {"error": "房间不存在"}

    room_type = room["type"]
    await record_chatroom_active(room_id, room_type)
    model_key = body.model
    connor_model_key = _resolve_connor_model(body.connor_model)

    # ── 更新用户最后活跃窗口追踪 ──
    if room_type == "group":
        # 群聊：两侧都更新为群聊
        # 用户在 Aion 参与的群聊发消息时，也视为正在聊天，推迟哨兵巡逻。
        cam.reset_patrol_timer()
    context_limit = room.get("context_minutes", 30)

    # TTS 参数
    tts_enabled = body.tts_enabled
    tts_aion_voice = body.tts_aion_voice
    tts_connor_voice = body.tts_connor_voice
    whisper_mode = body.whisper_mode

    _q: asyncio.Queue = asyncio.Queue()

    async def _bg_generate():
        try:
            if room_type == "connor_1v1":
                # Connor 单聊：只请求 Connor
                await _generate_connor_reply(room_id, room, msgs, _q, context_limit,
                                             connor_model_key=connor_model_key,
                                             tts_enabled=tts_enabled, tts_connor_voice=tts_connor_voice,
                                             whisper_mode=whisper_mode)
            elif _is_manual_group_reply_mode():
                return
            else:
                # 群聊：Aion 和 Connor 都回复
                await _generate_group_replies(room_id, room, msgs, model_key, connor_model_key, _q, context_limit,
                                              tts_enabled=tts_enabled, tts_aion_voice=tts_aion_voice, tts_connor_voice=tts_connor_voice,
                                              whisper_mode=whisper_mode)
        except Exception as e:
            import traceback
            traceback.print_exc()
            await _q.put({"type": "error", "content": str(e)})
        finally:
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_generate())

    async def generate():
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/rooms/{room_id}/reply-once")
async def reply_once(room_id: str, body: ReplyOnceTrigger):
    """指定群聊中的某一位 AI 单独回复一次。"""
    room, msgs = await _load_room_and_messages(room_id)
    if not room:
        return {"error": "房间不存在"}
    if room["type"] != "group":
        return {"error": "仅群聊支持指定回复"}

    speaker = (body.speaker or "").strip().lower()
    if speaker not in ("aion", "connor"):
        return {"error": "speaker must be 'aion' or 'connor'"}

    cam.reset_patrol_timer()

    context_limit = room.get("context_minutes", 30)
    query_text = msgs[-1]["content"] if msgs else ""
    model_key = body.model
    connor_model_key = _resolve_connor_model(body.connor_model)

    _q: asyncio.Queue = asyncio.Queue()

    async def _bg_generate():
        try:
            if speaker == "aion":
                await _reply_aion(
                    room_id, msgs, context_limit, query_text, model_key, _q,
                    tts_enabled=body.tts_enabled,
                    tts_voice=body.tts_aion_voice,
                    whisper_mode=body.whisper_mode,
                )
            else:
                await _reply_connor(
                    room_id, msgs, context_limit, query_text, _q,
                    connor_model_key=connor_model_key,
                    tts_enabled=body.tts_enabled,
                    tts_voice=body.tts_connor_voice,
                    whisper_mode=body.whisper_mode,
                )
        except Exception as e:
            import traceback
            traceback.print_exc()
            await _q.put({"type": "error", "content": str(e)})
        finally:
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_generate())

    async def generate():
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/rooms/{room_id}/ambient-voice/evaluate")
async def ambient_voice_evaluate(room_id: str, body: AmbientVoiceEvaluate):
    """当前前端环境侦听：ASR 文本先经哨兵筛选，必要时随机唤醒一位群聊 AI。"""
    cfg = load_chatroom_config()
    if not cfg.get("ambient_voice_enabled"):
        return {"ok": True, "triggered": False, "skipped": "disabled"}

    transcript = _ambient_excerpt(body.transcript, 3000)
    if len(transcript) < 4:
        return {"ok": True, "triggered": False, "skipped": "too_short"}

    listener = _ambient_listener_state()
    listener_client_id = (body.listener_client_id or "").strip()
    if listener.get("active") and listener_client_id and listener.get("client_id") != listener_client_id:
        return {
            "ok": True,
            "triggered": False,
            "skipped": "listener_lost",
            "listener": listener,
        }
    if listener.get("active") and not listener_client_id:
        return {
            "ok": True,
            "triggered": False,
            "skipped": "listener_required",
            "listener": listener,
        }
    if not listener.get("active") and listener_client_id:
        return {"ok": True, "triggered": False, "skipped": "listener_expired", "listener": listener}

    room, _ = await _load_room_and_messages(room_id, limit=5)
    if not room:
        return {"ok": False, "triggered": False, "error": "room_not_found"}
    if room.get("type") != "group":
        debug_payload = await _emit_ambient_voice_debug(room_id=room_id, transcript=transcript, skipped="not_group_room")
        return {"ok": True, "triggered": False, "skipped": "not_group_room", "debug": debug_payload}
    if room_id in _AMBIENT_GENERATING_ROOMS:
        debug_payload = await _emit_ambient_voice_debug(room_id=room_id, transcript=transcript, skipped="already_generating")
        return {"ok": True, "triggered": False, "skipped": "already_generating", "debug": debug_payload}

    forced = bool(body.forced)
    source_label = (body.listener_label or body.listener_source or "当前设备麦克风").strip()[:80]
    now = time.time()
    cooldown = _clamp_int(cfg.get("ambient_voice_cooldown_seconds"), 180, 30, 1800)
    last = _AMBIENT_LAST_TRIGGER_BY_ROOM.get(room_id, 0)
    if not forced and last and now - last < cooldown:
        remaining = max(0, int(cooldown - (now - last)))
        debug_payload = await _emit_ambient_voice_debug(
            room_id=room_id,
            transcript=transcript,
            skipped="cooldown",
            decision={
                "should_wake": False,
                "reason": f"冷却中，剩余 {remaining} 秒",
                "importance": 0.0,
                "source_label": source_label,
                "source": (body.listener_source or "").strip()[:40],
            },
        )
        return {
            "ok": True,
            "triggered": False,
            "skipped": "cooldown",
            "cooldown_remaining": remaining,
            "debug": debug_payload,
        }

    if forced:
        decision = {
            "should_wake": True,
            "topic": "即时语音唤醒",
            "summary": transcript[:600],
            "reason": "wake_word",
            "importance": 1.0,
        }
    else:
        decision = await _judge_ambient_voice(transcript)
    decision["source_label"] = source_label
    decision["source"] = (body.listener_source or "").strip()[:40]
    if not forced:
        if not decision.get("should_wake"):
            debug_payload = await _emit_ambient_voice_debug(
                room_id=room_id,
                transcript=transcript,
                decision=decision,
                skipped="sentinel_rejected",
            )
            return {"ok": True, "triggered": False, "decision": decision, "debug": debug_payload}

    speaker = random.choice(["aion", "connor"])
    debug_payload = await _emit_ambient_voice_debug(
        room_id=room_id,
        transcript=transcript,
        decision=decision,
        forced=forced,
        speaker=speaker,
    )
    _AMBIENT_LAST_TRIGGER_BY_ROOM[room_id] = now
    model_key = (body.model or cfg.get("aion_model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    connor_model_key = _resolve_connor_model(body.connor_model or cfg.get("connor_model"))

    asyncio.create_task(_run_ambient_voice_reply(
        room_id,
        speaker,
        decision,
        model_key=model_key,
        connor_model_key=connor_model_key,
        tts_enabled=bool(body.tts_enabled),
        tts_aion_voice=body.tts_aion_voice or "",
        tts_connor_voice=body.tts_connor_voice or "",
        forced=forced,
    ))

    return {
        "ok": True,
        "triggered": True,
        "speaker": speaker,
        "decision": decision,
        "forced": forced,
        "debug": debug_payload,
    }


@router.post("/messages/{msg_id}/edit-resend")
async def edit_resend_chatroom_message(msg_id: str, body: MsgEditResend):
    """编辑用户消息后重发：更新内容，删除后续消息，再按房间类型重新生成回复。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chatroom_messages WHERE id=?", (msg_id,))
        orig = await cur.fetchone()
        if not orig:
            return {"error": "message not found"}
        if orig["sender"] != "user":
            return {"error": "only user messages can be edited"}
        room_id = orig["room_id"]
        msg_created_at = orig["created_at"]
        resent_at = time.time()
        cur2 = await db.execute(
            "SELECT id FROM chatroom_messages WHERE room_id=? AND created_at>?",
            (room_id, msg_created_at),
        )
        later_msgs = await cur2.fetchall()
        await db.execute("DELETE FROM chatroom_messages WHERE room_id=? AND created_at>?", (room_id, msg_created_at))
        await db.execute(
            "UPDATE chatroom_messages SET content=?, created_at=? WHERE id=?",
            (body.content, resent_at, msg_id),
        )
        await db.execute("UPDATE chatroom_rooms SET updated_at=? WHERE id=?", (resent_at, room_id))
        await db.commit()

    updated = dict(orig)
    updated["content"] = body.content
    updated["created_at"] = resent_at
    try:
        updated["attachments"] = json.loads(updated.get("attachments") or "[]") if updated.get("attachments") else []
    except Exception:
        updated["attachments"] = []
    await broadcast_synced(manager, {"type": "chatroom_msg_updated", "data": updated})
    for lm in later_msgs:
        await broadcast_synced(manager, {"type": "chatroom_msg_deleted", "data": {"id": lm["id"], "room_id": room_id}})

    room, msgs = await _load_room_and_messages(room_id)
    if not room:
        return {"error": "房间不存在"}

    room_type = room["type"]
    await record_chatroom_active(room_id, room_type)
    model_key = body.model
    connor_model_key = _resolve_connor_model(body.connor_model)
    context_limit = room.get("context_minutes", 30)
    if room_type == "group":
        cam.reset_patrol_timer()

    _q: asyncio.Queue = asyncio.Queue()

    async def _bg_generate():
        try:
            if room_type == "connor_1v1":
                await _generate_connor_reply(
                    room_id, room, msgs, _q, context_limit,
                    connor_model_key=connor_model_key,
                    tts_enabled=body.tts_enabled,
                    tts_connor_voice=body.tts_connor_voice,
                    whisper_mode=body.whisper_mode,
                )
            elif _is_manual_group_reply_mode():
                return
            else:
                await _generate_group_replies(
                    room_id, room, msgs, model_key, connor_model_key, _q, context_limit,
                    tts_enabled=body.tts_enabled,
                    tts_aion_voice=body.tts_aion_voice,
                    tts_connor_voice=body.tts_connor_voice,
                    whisper_mode=body.whisper_mode,
                )
        except Exception as e:
            import traceback
            traceback.print_exc()
            await _q.put({"type": "error", "content": str(e)})
        finally:
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_generate())

    async def generate():
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/messages/{msg_id}/regenerate")
async def regenerate_chatroom_message(msg_id: str, body: MsgRegenerate):
    """重新生成一条 AI 消息：删除该消息及其后的消息，再让同一位 AI 重答。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chatroom_messages WHERE id=?", (msg_id,))
        target = await cur.fetchone()
        if not target:
            return {"error": "message not found"}
        if target["sender"] not in ("aion", "connor"):
            return {"error": "only AI messages can be regenerated"}
        room_id = target["room_id"]
        msg_created_at = target["created_at"]
        cur2 = await db.execute(
            "SELECT id FROM chatroom_messages WHERE room_id=? AND created_at>=?",
            (room_id, msg_created_at),
        )
        later_msgs = await cur2.fetchall()
        await db.execute("DELETE FROM chatroom_messages WHERE room_id=? AND created_at>=?", (room_id, msg_created_at))
        await db.execute("UPDATE chatroom_rooms SET updated_at=? WHERE id=?", (time.time(), room_id))
        await db.commit()

    for lm in later_msgs:
        await broadcast_synced(manager, {"type": "chatroom_msg_deleted", "data": {"id": lm["id"], "room_id": room_id}})

    room, msgs = await _load_room_and_messages(room_id)
    if not room:
        return {"error": "房间不存在"}

    context_limit = room.get("context_minutes", 30)
    query_text = msgs[-1]["content"] if msgs else ""
    model_key = body.model
    connor_model_key = _resolve_connor_model(body.connor_model)
    _q: asyncio.Queue = asyncio.Queue()

    async def _bg_generate():
        try:
            if target["sender"] == "aion":
                await _reply_aion(
                    room_id, msgs, context_limit, query_text, model_key, _q,
                    tts_enabled=body.tts_enabled,
                    tts_voice=body.tts_aion_voice,
                    whisper_mode=body.whisper_mode,
                )
            else:
                await _reply_connor(
                    room_id, msgs, context_limit, query_text, _q,
                    connor_model_key=connor_model_key,
                    tts_enabled=body.tts_enabled,
                    tts_voice=body.tts_connor_voice,
                    whisper_mode=body.whisper_mode,
                )
        except Exception as e:
            import traceback
            traceback.print_exc()
            await _q.put({"type": "error", "content": str(e)})
        finally:
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_generate())

    async def generate():
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


async def _generate_connor_reply(room_id, room, msgs, _q, context_limit, *, connor_model_key="Codex", tts_enabled=False, tts_connor_voice="", whisper_mode=False):
    """Connor 单聊回复（Codex CLI 流式调用）"""
    connor_label = _name_for_identity("connor")
    query_text = msgs[-1]["content"] if msgs else ""

    connor_messages, digest_out = await build_connor_1v1_context(
        room_id, msgs,
        context_limit=context_limit,
        query_text=query_text,
        whisper_mode=whisper_mode,
    )
    _process_voice_attachments(connor_messages)

    connor_msg_id = f"cm_{int(time.time() * 1000)}_c"
    await _q.put({"type": "connor_start", "id": connor_msg_id})

    full_text = ""
    has_reply = False
    has_error = False
    error_text = None
    tts_from_model = True
    usage_meta: dict = {}

    async def content_stream():
        nonlocal has_reply
        async for chunk in _stream_connor_model(connor_messages, connor_model_key, usage_meta):
            if chunk.startswith(CLI_STATUS_PREFIX):
                await _q.put({"type": "connor_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                continue
            has_reply = True
            yield chunk

    stream_result = await _consume_chatroom_stream(
        content_stream(),
        _q,
        chunk_type="connor_chunk",
    )
    full_text = stream_result.committed_text
    safety_notice = stream_result.notice
    has_error = stream_result.stop_reason is not None
    error_text = stream_result.diagnostic_error or stream_result.stop_reason
    tts_from_model = bool(full_text)

    full_text = full_text.strip()
    if not full_text:
        full_text = f"{connor_label} 暂时无法回复，请稍后再试。"

    # 工具指令处理（从文本中剥离并执行，与群聊保持一致）
    try:
        clean_text, triggered = await _process_chatroom_commands(full_text, room_id, "connor", connor_msg_id, _q)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[CHATROOM] _process_chatroom_commands 异常: {e}")
        clean_text = _visible_chatroom_text(full_text)
        triggered = {}

    clean_text = _normalize_cli_bubble_breaks(clean_text, connor_model_key)

    # TTS 用干净文本
    if tts_enabled and tts_connor_voice and clean_text and tts_from_model:
        tts = TTSStreamer(connor_msg_id, tts_connor_voice, manager, sse_queue=_q)
        tts.feed(clean_text)
        await tts.flush()

    if safety_notice:
        clean_text = f"{clean_text}\n\n[{safety_notice}]".strip()
    reply = _rewrite_connor_paths(clean_text)
    saved_imgs = await _extract_and_save_images(reply)
    msg = await _save_msg(
        room_id, "connor", reply, connor_msg_id,
        attachments=saved_imgs + _music_attachments_from_triggered(triggered) + _luckin_attachments_from_triggered(triggered),
        auto_tts=not safety_notice and tts_from_model and not (tts_enabled and tts_connor_voice and clean_text),
        reasoning_content=usage_meta.get("reasoning_content", "").strip(),
    )
    await _q.put({"type": "connor_done", "message": msg})
    await _emit_chatroom_debug(_q, _chatroom_debug_payload(
        room_id=room_id,
        model_key=connor_model_key,
        msg_id=connor_msg_id,
        history=connor_messages,
        digest_result=digest_out,
        usage_meta=usage_meta,
        has_error=has_error,
        error_text=error_text,
    ))

    # 触发后续动作
    _fire_chatroom_followups(triggered, room_id, "connor", connor_model_key, connor_msg_id)


async def _generate_group_replies(room_id, room, msgs, model_key, connor_model_key, _q, context_limit, *, tts_enabled=False, tts_aion_voice="", tts_connor_voice="", whisper_mode=False):
    """群聊回复：顺序执行，第二个 AI 能看到第一个的回复和工具执行结果"""
    query_text = msgs[-1]["content"] if msgs else ""

    reply_order = load_chatroom_config().get("reply_order", "random")
    digest = {
        "is_search_needed": False,
        "keywords": [],
        "require_detail": False,
        "status": "",
        "topic": "",
        "first_responder": "random",
    }

    user_name, ai_name, connor_name = get_chatroom_names()
    recent_for_digest = []
    for msg in msgs[-6:]:
        sender = str(msg.get("sender") or "").strip().lower()
        if sender not in ("user", "aion", "connor"):
            continue
        recent_for_digest.append({
            "role": "assistant" if sender in ("aion", "connor") else "user",
            "sender": sender,
            "content": str(msg.get("content") or "")[:200],
        })
    try:
        digest = await asyncio.wait_for(
            instant_digest(
                recent_for_digest,
                group_participants={
                    "user": user_name,
                    "aion": ai_name,
                    "connor": connor_name,
                },
            ),
            timeout=4,
        )
    except Exception:
        pass

    first_responder = digest.get("first_responder", "random")
    if reply_order == "aion":
        first_responder = "aion"
    elif reply_order == "connor":
        first_responder = "connor"
    if first_responder not in ("aion", "connor"):
        first_responder = random.choice(["aion", "connor"])

    if first_responder == "aion":
        digest = await _reply_aion(
            room_id, msgs, context_limit, query_text, model_key, _q,
            tts_enabled=tts_enabled, tts_voice=tts_aion_voice,
            digest_result=digest, whisper_mode=whisper_mode,
        )
        _, updated_msgs = await _load_room_and_messages(room_id)
        await _reply_connor(room_id, updated_msgs, context_limit, query_text, _q,
                            connor_model_key=connor_model_key, tts_enabled=tts_enabled, tts_voice=tts_connor_voice, digest_result=digest, whisper_mode=whisper_mode)
    else:
        digest = await _reply_connor(
            room_id, msgs, context_limit, query_text, _q,
            connor_model_key=connor_model_key, tts_enabled=tts_enabled,
            tts_voice=tts_connor_voice, digest_result=digest,
            whisper_mode=whisper_mode,
        )
        _, updated_msgs = await _load_room_and_messages(room_id)
        await _reply_aion(room_id, updated_msgs, context_limit, query_text, model_key, _q,
                          tts_enabled=tts_enabled, tts_voice=tts_aion_voice, digest_result=digest, whisper_mode=whisper_mode)


async def _reply_aion(room_id, msgs, context_limit, query_text, model_key, _q, *, tts_enabled=False, tts_voice="", digest_result=None, whisper_mode=False, ambient_context: str = ""):
    ai_label = _name_for_identity("aion")
    aion_history, digest_out = await build_aion_group_context(
        room_id, msgs, context_limit, query_text,
        digest_result=digest_result,
        whisper_mode=whisper_mode,
    )
    if ambient_context:
        aion_history.append({"role": "user", "content": append_message_meta(ambient_context, time.time(), "环境语音")})
    _process_voice_attachments(aion_history)
    aion_msg_id = f"cm_{int(time.time() * 1000)}_a"
    await _q.put({"type": "aion_start", "id": aion_msg_id})

    full_text = ""
    has_error = False
    error_text = None
    tts_from_model = True
    usage_meta: dict = {}

    async def content_stream():
        async for chunk in stream_ai(aion_history, model_key, usage_meta):
            if chunk.startswith(CLI_STATUS_PREFIX):
                await _q.put({"type": "aion_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                continue
            yield chunk

    stream_result = await _consume_chatroom_stream(
        content_stream(),
        _q,
        chunk_type="aion_chunk",
    )
    full_text = stream_result.committed_text
    safety_notice = stream_result.notice
    has_error = stream_result.stop_reason is not None
    error_text = stream_result.diagnostic_error or stream_result.stop_reason
    tts_from_model = bool(full_text)

    # 工具指令处理（从文本中剥离并执行）
    try:
        clean_text, triggered = await _process_chatroom_commands(full_text, room_id, "aion", aion_msg_id, _q)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[CHATROOM] _process_chatroom_commands 异常: {e}")
        clean_text = _visible_chatroom_text(full_text)
        triggered = {}

    clean_text = _normalize_cli_bubble_breaks(clean_text, model_key)

    # TTS 用干净文本
    if tts_enabled and tts_voice and clean_text and tts_from_model:
        tts = TTSStreamer(aion_msg_id, tts_voice, manager, sse_queue=_q)
        tts.feed(clean_text)
        await tts.flush()

    if safety_notice:
        clean_text = f"{clean_text}\n\n[{safety_notice}]".strip()
    # 保存干净文本
    saved_imgs = await _extract_and_save_images(clean_text)
    aion_msg = await _save_msg(
        room_id, "aion", clean_text, aion_msg_id,
        attachments=saved_imgs + _music_attachments_from_triggered(triggered) + _luckin_attachments_from_triggered(triggered) + _toy_attachments_from_triggered(triggered),
        auto_tts=not safety_notice and tts_from_model and not (tts_enabled and tts_voice and clean_text),
        reasoning_content=usage_meta.get("reasoning_content", "").strip(),
    )
    await _q.put({"type": "aion_done", "message": aion_msg})
    await _emit_chatroom_debug(_q, _chatroom_debug_payload(
        room_id=room_id,
        model_key=model_key,
        msg_id=aion_msg_id,
        history=aion_history,
        digest_result=digest_out,
        usage_meta=usage_meta,
        has_error=has_error,
        error_text=error_text,
    ))

    # 触发后续动作（异步，不阻塞后续 AI 回复）
    _fire_chatroom_followups(triggered, room_id, "aion", model_key, aion_msg_id)

    return digest_out


async def _reply_connor(room_id, msgs, context_limit, query_text, _q, *, connor_model_key="Codex", tts_enabled=False, tts_voice="", digest_result=None, whisper_mode=False, ambient_context: str = ""):
    connor_label = _name_for_identity("connor")
    connor_history, digest_out = await build_connor_group_context(
        room_id, msgs, context_limit, query_text,
        digest_result=digest_result,
        whisper_mode=whisper_mode,
    )
    if ambient_context:
        connor_history.append({"role": "user", "content": append_message_meta(ambient_context, time.time(), "环境语音")})
    _process_voice_attachments(connor_history)
    connor_msg_id = f"cm_{int(time.time() * 1000)}_c"
    await _q.put({"type": "connor_start", "id": connor_msg_id})

    full_text = ""
    has_error = False
    error_text = None
    tts_from_model = True
    usage_meta: dict = {}

    async def content_stream():
        async for chunk in _stream_connor_model(connor_history, connor_model_key, usage_meta):
            if chunk.startswith(CLI_STATUS_PREFIX):
                await _q.put({"type": "connor_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                continue
            yield chunk

    stream_result = await _consume_chatroom_stream(
        content_stream(),
        _q,
        chunk_type="connor_chunk",
    )
    full_text = stream_result.committed_text
    safety_notice = stream_result.notice
    has_error = stream_result.stop_reason is not None
    error_text = stream_result.diagnostic_error or stream_result.stop_reason
    tts_from_model = bool(full_text)

    full_text = full_text.strip()
    if not full_text:
        full_text = f"{connor_label} 暂时无法回复，请稍后再试。"

    # 工具指令处理
    try:
        clean_text, triggered = await _process_chatroom_commands(full_text, room_id, "connor", connor_msg_id, _q)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[CHATROOM] _process_chatroom_commands 异常: {e}")
        clean_text = _visible_chatroom_text(full_text)
        triggered = {}

    clean_text = _normalize_cli_bubble_breaks(clean_text, connor_model_key)

    # TTS 用干净文本
    if tts_enabled and tts_voice and clean_text and tts_from_model:
        tts = TTSStreamer(connor_msg_id, tts_voice, manager, sse_queue=_q)
        tts.feed(clean_text)
        await tts.flush()

    if safety_notice:
        clean_text = f"{clean_text}\n\n[{safety_notice}]".strip()
    clean_text = _rewrite_connor_paths(clean_text)
    saved_imgs = await _extract_and_save_images(clean_text)
    connor_msg = await _save_msg(
        room_id, "connor", clean_text, connor_msg_id,
        attachments=saved_imgs + _music_attachments_from_triggered(triggered) + _luckin_attachments_from_triggered(triggered) + _toy_attachments_from_triggered(triggered),
        auto_tts=not safety_notice and tts_from_model and not (tts_enabled and tts_voice and clean_text),
        reasoning_content=usage_meta.get("reasoning_content", "").strip(),
    )
    await _q.put({"type": "connor_done", "message": connor_msg})
    await _emit_chatroom_debug(_q, _chatroom_debug_payload(
        room_id=room_id,
        model_key=connor_model_key,
        msg_id=connor_msg_id,
        history=connor_history,
        digest_result=digest_out,
        usage_meta=usage_meta,
        has_error=has_error,
        error_text=error_text,
    ))

    # 触发后续动作
    _fire_chatroom_followups(triggered, room_id, "connor", connor_model_key, connor_msg_id)

    return digest_out


# ══════════════════════════════════════════════════
#  AI 互聊
# ══════════════════════════════════════════════════

@router.post("/rooms/{room_id}/ai-chat")
async def trigger_ai_chat(room_id: str, body: AiChatTrigger):
    """触发 AI 互聊（Aion 和 Connor 轮流对话）"""
    room, msgs = await _load_room_and_messages(room_id)
    if not room:
        return {"error": "房间不存在"}

    max_rounds = body.rounds or room.get("ai_chat_rounds", 1)
    model_key = body.model
    connor_model_key = _resolve_connor_model(body.connor_model)
    context_limit = room.get("context_minutes", 30)
    tts_enabled = body.tts_enabled
    tts_aion_voice = body.tts_aion_voice
    tts_connor_voice = body.tts_connor_voice

    _q: asyncio.Queue = asyncio.Queue()

    async def _bg_ai_chat():
        nonlocal msgs
        try:
            digest = None
            reply_order = load_chatroom_config().get("reply_order", "random")
            for round_num in range(max_rounds):
                await _q.put({"type": "round_start", "round": round_num + 1, "total": max_rounds})

                query_text = msgs[-1]["content"] if msgs else ""

                # 决定回复顺序
                if reply_order == "connor":
                    aion_first = False
                elif reply_order == "aion":
                    aion_first = True
                else:
                    aion_first = random.choice([True, False])

                if aion_first:
                    digest = await _reply_aion(
                        room_id, msgs, context_limit, query_text, model_key, _q,
                        tts_enabled=tts_enabled, tts_voice=tts_aion_voice, digest_result=digest,
                    )
                    _, msgs = await _load_room_and_messages(room_id)
                    digest = await _reply_connor(
                        room_id, msgs, context_limit, query_text, _q,
                        connor_model_key=connor_model_key,
                        tts_enabled=tts_enabled, tts_voice=tts_connor_voice, digest_result=digest,
                    )
                else:
                    digest = await _reply_connor(
                        room_id, msgs, context_limit, query_text, _q,
                        connor_model_key=connor_model_key,
                        tts_enabled=tts_enabled, tts_voice=tts_connor_voice, digest_result=digest,
                    )
                    _, msgs = await _load_room_and_messages(room_id)
                    digest = await _reply_aion(
                        room_id, msgs, context_limit, query_text, model_key, _q,
                        tts_enabled=tts_enabled, tts_voice=tts_aion_voice, digest_result=digest,
                    )

                # 重新加载消息
                _, msgs = await _load_room_and_messages(room_id)

        except Exception as e:
            import traceback
            traceback.print_exc()
            await _q.put({"type": "error", "content": str(e)})
        finally:
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_ai_chat())

    async def generate():
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ══════════════════════════════════════════════════
#  记忆
# ══════════════════════════════════════════════════

@router.get("/rooms/{room_id}/memories")
async def list_room_memories(room_id: str):
    await _ensure_chatroom_memory_source_column()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, room_id, scope, content, keywords, importance, created_at, unresolved, "
            "source_start_ts, source_end_ts, source_msg_id, memory_kind, evidence_summary, evidence_detail_level "
            "FROM chatroom_memories WHERE COALESCE(archive_state,'active')='active' "
            "ORDER BY COALESCE(source_end_ts, source_start_ts, created_at) DESC",
        )
        rows = await cur.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if not item.get("memory_kind"):
                item["memory_kind"] = "daily" if (
                    item.get("source_start_ts") and item.get("source_end_ts")
                    and not str(item.get("source_msg_id") or "").strip()
                ) else "long_term"
            item["memory_kind_label"] = "日常" if item["memory_kind"] == "daily" else "长期重要"
            explicit_source = bool(str(item.get("source_msg_id") or "").strip())
            source_ids = _source_ids_for_chatroom_memory(item)
            if explicit_source:
                item["source_count"] = len(source_ids)
            elif item.get("source_start_ts") and item.get("source_end_ts"):
                cur = await db.execute(
                    "SELECT COUNT(*) FROM chatroom_messages "
                    "WHERE sender != 'system' AND created_at >= ? AND created_at <= ?",
                    (item["source_start_ts"], item["source_end_ts"]),
                )
                item["source_count"] = (await cur.fetchone())[0]
            else:
                item["source_count"] = 0
            item.update(_memory_time_payload(item))
            result.append(item)
        return result


@router.post("/rooms/{room_id}/digest")
async def trigger_digest(room_id: str, body: Optional[DigestTrigger] = None):
    connor_model_key = _resolve_connor_model(body.connor_model if body else None)
    result = await digest_chatroom(model_key=connor_model_key)
    return result


@router.post("/rooms/{room_id}/memories")
async def create_memory(room_id: str, body: MemoryCreate):
    await _ensure_chatroom_memory_source_column()
    # 确定 scope
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT type FROM chatroom_rooms WHERE id=?", (room_id,))
        room = await cur.fetchone()
    scope = "connor" if room and room["type"] == "connor_1v1" else "group"

    mem_id = await save_chatroom_memory(
        room_id=room_id,
        scope=scope,
        content=body.content,
        keywords=body.keywords,
        importance=body.importance,
        memory_kind="daily" if body.memory_kind == "daily" else "long_term",
        evidence_summary=body.evidence_summary,
    )
    return {"ok": True, "id": mem_id}


@router.put("/memories/{mem_id}")
async def update_memory(mem_id: str, body: MemoryUpdate):
    await _ensure_chatroom_memory_source_column()
    async with get_db() as db:
        sets, vals = [], []
        if body.content is not None:
            sets.append("content=?")
            vals.append(body.content)
        if body.keywords is not None:
            sets.append("keywords=?")
            vals.append(body.keywords)
        if body.importance is not None:
            sets.append("importance=?")
            vals.append(body.importance)
        if body.memory_kind is not None:
            sets.append("memory_kind=?")
            vals.append("daily" if body.memory_kind == "daily" else "long_term")
        if body.unresolved is not None:
            sets.append("unresolved=?")
            vals.append(1 if body.unresolved else 0)
        if body.evidence_summary is not None:
            sets.append("evidence_summary=?")
            vals.append(str(body.evidence_summary or "").strip())
        if sets:
            vals.append(mem_id)
            await db.execute(f"UPDATE chatroom_memories SET {', '.join(sets)} WHERE id=?", vals)
            await db.commit()

            # 如果内容修改了，重新生成 embedding
            if body.content is not None:
                emb = await get_embedding(body.content)
                if emb:
                    from memory import _pack_embedding
                    await db.execute("UPDATE chatroom_memories SET embedding=? WHERE id=?",
                                     (_pack_embedding(emb), mem_id))
                    await db.commit()
    return {"ok": True}


@router.patch("/memories/{mem_id}/unresolved")
async def toggle_memory_unresolved(mem_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT unresolved FROM chatroom_memories WHERE id=?",
            (mem_id,),
        )
        row = await cur.fetchone()
        if not row:
            return {"ok": False, "message": "Memory not found"}
        new_val = 0 if row["unresolved"] else 1
        await db.execute(
            "UPDATE chatroom_memories SET unresolved=? WHERE id=?",
            (new_val, mem_id),
        )
        await db.commit()
    return {"ok": True, "unresolved": new_val}


@router.get("/memories/{mem_id}/source-legacy")
async def get_memory_source_legacy(mem_id: str):
    """追溯聊天室记忆对应的原始聊天记录（私聊+群聊）"""
    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT source_start_ts, source_end_ts FROM chatroom_memories WHERE id=?", (mem_id,))
        mem = await cur.fetchone()
    if not mem or not mem["source_start_ts"] or not mem["source_end_ts"]:
        return {"ok": False, "message": "该记忆没有可追溯的原文"}

    user_name, ai_name, connor_name = get_chatroom_names()
    start_ts, end_ts = mem["source_start_ts"], mem["source_end_ts"]

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT sender, content, created_at FROM chatroom_messages "
            "WHERE sender != 'system' AND created_at >= ? AND created_at <= ? "
            "ORDER BY created_at ASC",
            (start_ts, end_ts),
        )
        rows = await cur.fetchall()

    name_map = {"user": user_name, "aion": ai_name, "connor": connor_name}
    messages = []
    for r in rows:
        messages.append({
            "role": "assistant" if r["sender"] in ("aion", "connor") else "user",
            "name": name_map.get(r["sender"], r["sender"]),
            "content": r["content"],
            "created_at": r["created_at"],
        })
    return {"ok": True, "messages": messages}


@router.get("/memories/{mem_id}/source")
async def get_memory_source(mem_id: str):
    from memory_compression import resolve_source_message_ids

    await _ensure_chatroom_memory_source_column()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, content, keywords, source_start_ts, source_end_ts, source_msg_id, "
            "compression_batch_id "
            "FROM chatroom_memories WHERE id=?",
            (mem_id,),
        )
        mem = await cur.fetchone()
    if not mem:
        return {"ok": False, "message": "Memory not found"}
    selected_ids = set(_source_ids_for_chatroom_memory(mem))
    if not selected_ids and str(mem["compression_batch_id"] or "").strip():
        selected_ids.update(await resolve_source_message_ids("chatroom", mem_id))
    if not selected_ids and (not mem["source_start_ts"] or not mem["source_end_ts"]):
        return {"ok": False, "message": "No source messages for this memory"}

    user_name, ai_name, connor_name = get_chatroom_names()
    name_map = {"user": user_name, "aion": ai_name, "connor": connor_name}
    messages = []
    if mem["source_start_ts"] and mem["source_end_ts"]:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, sender, content, created_at FROM chatroom_messages "
                "WHERE sender != 'system' AND created_at >= ? AND created_at <= ? "
                "ORDER BY created_at ASC",
                (mem["source_start_ts"], mem["source_end_ts"]),
            )
            for row in await cur.fetchall():
                messages.append({
                    "id": f"chatroom:{row['id']}",
                    "role": "assistant" if row["sender"] in ("aion", "connor") else "user",
                    "name": name_map.get(row["sender"], row["sender"]),
                    "content": row["content"],
                    "created_at": row["created_at"],
                })

    if selected_ids:
        existing = {m["id"] for m in messages}
        extra = await _fetch_chatroom_source_rows_by_ids(list(selected_ids - existing))
        messages.extend([m for m in extra if m["id"] not in existing])
        messages = [m for m in messages if m["id"] in selected_ids]

    exact_mode = bool(selected_ids)
    keywords = _chatroom_keyword_list(mem["keywords"])
    needles = _chatroom_content_needles(mem["content"])
    scored = []
    for msg in messages:
        score = 1.0 if msg["id"] in selected_ids else _chatroom_source_score(mem, msg, keywords, needles)
        scored.append((score, msg["id"]))
    recommended_ids = selected_ids
    if not exact_mode and scored:
        positive = [(score, source_id) for score, source_id in scored if score > 0]
        positive.sort(key=lambda item: item[0], reverse=True)
        recommended_ids = {source_id for _, source_id in positive[:8]}

    messages.sort(key=lambda x: x["created_at"])
    for msg in messages:
        msg["selected"] = msg["id"] in recommended_ids
        msg["recommended"] = msg["selected"]
    return {
        "ok": True,
        "messages": messages,
        "selected_count": sum(1 for msg in messages if msg["selected"]),
        "selection_mode": "saved" if exact_mode else "suggested",
    }


@router.post("/memories/{mem_id}/source-selection")
async def save_memory_source_selection(mem_id: str, body: MemorySourceSelection):
    await _ensure_chatroom_memory_source_column()
    source_ids = []
    seen = set()
    for source_id in body.source_message_ids:
        text = str(source_id).strip()
        if not text or ":" not in text or text in seen:
            continue
        prefix, raw_id = text.split(":", 1)
        if prefix != "chatroom" or not raw_id:
            continue
        source_ids.append(text)
        seen.add(text)

    source_rows = await _fetch_chatroom_source_rows_by_ids(source_ids)
    found_ids = {row["id"] for row in source_rows}
    source_ids = [source_id for source_id in source_ids if source_id in found_ids]
    source_rows.sort(key=lambda row: row["created_at"])
    source_start = source_rows[0]["created_at"] if source_rows else None
    source_end = source_rows[-1]["created_at"] if source_rows else None
    source_text = json.dumps(source_ids, ensure_ascii=False)

    now = time.time()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, source_msg_id, source_start_ts, source_end_ts FROM chatroom_memories WHERE id=?",
            (mem_id,),
        )
        before = await cur.fetchone()
        if not before:
            return {"ok": False, "message": "Memory not found"}
        await db.execute(
            "CREATE TABLE IF NOT EXISTS chatroom_memory_source_edit_log ("
            "id TEXT PRIMARY KEY, mem_id TEXT NOT NULL, before_json TEXT, after_json TEXT, created_at REAL NOT NULL)"
        )
        before_dict = dict(before)
        after_dict = {
            "id": mem_id,
            "source_msg_id": source_text,
            "source_start_ts": source_start,
            "source_end_ts": source_end,
        }
        await db.execute(
            "INSERT INTO chatroom_memory_source_edit_log (id, mem_id, before_json, after_json, created_at) VALUES (?,?,?,?,?)",
            (
                f"chatroom_mem_source_edit_{time.time_ns()}",
                mem_id,
                json.dumps(before_dict, ensure_ascii=False),
                json.dumps(after_dict, ensure_ascii=False),
                now,
            ),
        )
        await db.execute(
            "UPDATE chatroom_memories SET source_msg_id=?, source_start_ts=?, source_end_ts=? WHERE id=?",
            (source_text, source_start, source_end, mem_id),
        )
        await db.commit()
    return {
        "ok": True,
        "id": mem_id,
        "source_msg_id": source_text,
        "source_start_ts": source_start,
        "source_end_ts": source_end,
        "selected_count": len(source_ids),
        **_memory_time_payload({"source_start_ts": source_start, "source_end_ts": source_end}),
    }


@router.delete("/memories/{mem_id}")
async def delete_memory(mem_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM chatroom_memories WHERE id=?", (mem_id,))
        await db.commit()
    return {"ok": True}
