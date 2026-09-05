"""
共享上下文构建模块：为 Aion / Connor 构建完整的系统能力、记忆、时间感知注入。
被 routes/chat.py（私聊）和 chatroom.py（群聊）共同使用。
"""

import json, re, time, asyncio
from datetime import datetime

import aiosqlite

from config import load_worldbook
from database import get_db
from taobao_context import trip_card, load_trip_products, render_trip_context
from schedule import get_active_schedules, build_schedule_prompt
from luckin import LUCKIN_CMD_PATTERN
from song_gen import SONG_CMD_PATTERN
from wechat_bridge import WECHAT_MESSAGE_PATTERN
from web_search import WEB_EXTRACT_CMD_PATTERN, WEB_SEARCH_CMD_PATTERN
from capabilities import (
    build_capability_prompt_items,
    build_cli_file_storage_text,
    format_ability_block,
    is_capability_enabled,
)
from app_supervision_ai import APP_COMMAND_PATTERN
from activity import get_device_context_for_prompt
from memory import (
    instant_digest, recall_memories, build_surfacing_memories,
    fetch_source_details, format_recalled_memories_for_prompt,
)

# ── 工具指令正则（供调用方做后处理用，集中定义） ──
MUSIC_CMD_PATTERN = re.compile(r'\[MUSIC:([^\]]+)\]')
MOMENT_CMD_PATTERN = re.compile(r'\[MOMENT:(.+?)(?:\|(true|false))?\]')
MEMORY_CMD_PATTERN = re.compile(
    r'\[\s*M[\u200b\u200c\u200d\ufeff]*E[\u200b\u200c\u200d\ufeff]*M[\u200b\u200c\u200d\ufeff]*O[\u200b\u200c\u200d\ufeff]*R[\u200b\u200c\u200d\ufeff]*Y\s*[：:]\s*([^\]]+)\]',
    re.IGNORECASE,
)
WISH_CMD_PATTERN = re.compile(r'\[\s*许愿\s*[：:]\s*([^\]]+)\]')
ACTIVITY_CHECK_PATTERN = re.compile(r'\[查看动态:(\d+)\]')
SELFIE_CMD_PATTERN = re.compile(r'\[SELFIE:\s*([^\]]+)\]')
DRAW_CMD_PATTERN = re.compile(r'\[DRAW:\s*([^\]]+)\]')
POI_SEARCH_PATTERN = re.compile(r'\[POI_SEARCH:([^\]]+)\]')
TOY_CMD_PATTERN = re.compile(r'\[TOY:(\d|STOP)\]')
PET_CMD_PATTERN = re.compile(r'\[PET:([a-z_\-]+)\]', re.IGNORECASE)
HOME_CMD_PATTERN = re.compile(r'\[HOME:([^\]]+)\]', re.IGNORECASE)
BAND_VIBRATE_CMD_PATTERN = re.compile(r'\[BAND_VIBRATE:(single|call)\]', re.IGNORECASE)
BAND_NOTE_CMD_PATTERN = re.compile(
    r'\[BAND_NOTE_(?:SINGLE|CALL)\s*[：:]\s*[^\]]*\]',
    re.IGNORECASE,
)
TRANSFER_CMD_PATTERN = re.compile(r'\[转账[：:]\s*(-?\d+(?:\.\d+)?)\s*元\]')
PRIVATE_WHISPER_CMD_PATTERN = re.compile(r'\[悄悄话[：:]\s*([^\]]+)\]')
VIDEO_CALL_CMD = '[视频电话]'
META_TAG_PATTERN = re.compile(r'\s*<meta\b[^>]*>.*?</meta\s*>', re.DOTALL | re.IGNORECASE)
MEMORY_SEARCH_CMD_PATTERN = re.compile(
    r'[\[［【]\s*MEMORY_SEARCH\s*[：:]\s*[^\]］】]*[\]］】]', re.IGNORECASE
)

# 所有需要从 AI 回复中剥离的工具指令正则列表（TTS、保存时统一清理）
_ALL_CMD_PATTERNS = [
    MUSIC_CMD_PATTERN, MOMENT_CMD_PATTERN, MEMORY_CMD_PATTERN, WISH_CMD_PATTERN,
    ACTIVITY_CHECK_PATTERN, SELFIE_CMD_PATTERN, DRAW_CMD_PATTERN, SONG_CMD_PATTERN,
    POI_SEARCH_PATTERN, TOY_CMD_PATTERN, PET_CMD_PATTERN,
    HOME_CMD_PATTERN, BAND_VIBRATE_CMD_PATTERN, BAND_NOTE_CMD_PATTERN,
    LUCKIN_CMD_PATTERN, TRANSFER_CMD_PATTERN, PRIVATE_WHISPER_CMD_PATTERN,
    WECHAT_MESSAGE_PATTERN, WEB_SEARCH_CMD_PATTERN, WEB_EXTRACT_CMD_PATTERN,
    APP_COMMAND_PATTERN, MEMORY_SEARCH_CMD_PATTERN,
]

def strip_tool_commands(text: str) -> str:
    """从文本中移除所有工具指令标记，返回干净文本（用于 TTS 和保存）"""
    for pat in _ALL_CMD_PATTERNS:
        text = pat.sub("", text)
    if VIDEO_CALL_CMD in text:
        text = text.replace(VIDEO_CALL_CMD, "")
    text = META_TAG_PATTERN.sub("", text)
    return text.strip()


def format_message_time(created_at) -> str:
    """把消息时间戳格式化为给模型看的精确本地时间。"""
    dt = datetime.fromtimestamp(float(created_at))
    return f"{dt.year}年{dt.month}月{dt.day}日 {dt.strftime('%H:%M:%S')}"


def append_message_meta(content: str, created_at, label: str = "") -> str:
    """清理旧 meta 后，为单条上下文消息追加发送时间 meta。"""
    text = META_TAG_PATTERN.sub("", content or "").strip()
    if created_at:
        suffix = f" [{label}]" if label else ""
        text += f"\n<meta>发送时间：{format_message_time(created_at)}{suffix}</meta>"
    return text


def _timeline_display_names() -> tuple[str, str, str]:
    wb = load_worldbook()
    user_name = wb.get("user_name") or "用户"
    ai_name = wb.get("ai_name") or "AI"
    connor_name = "AI"
    try:
        from chatroom import load_chatroom_config
        connor_name = load_chatroom_config().get("connor_name") or "AI"
    except Exception:
        pass
    return user_name, ai_name, connor_name


async def build_health_summary() -> str:
    """当健康数据分享开关打开时，构建简短的身体数据摘要。"""
    if not is_capability_enabled("health_context"):
        return ""
    try:
        from health_context import category_label, classify_heart_rate, get_heart_config
        from routes.health import build_mi_band_summary

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            mi_band = await build_mi_band_summary(db)
            heart_cfg = await get_heart_config(db)
            cur = await db.execute(
                "SELECT weight_kg FROM health_weight_entries ORDER BY date DESC LIMIT 1"
            )
            weight_row = await cur.fetchone()
            cur = await db.execute(
                "SELECT start_date, end_date FROM health_period_entries ORDER BY start_date DESC LIMIT 1"
            )
            period_row = await cur.fetchone()

        parts = []
        hr = mi_band.get("latestHeartRate")
        measured_at = mi_band.get("latestHeartRateAt")
        if hr:
            try:
                age_seconds = time.time() - float(measured_at or 0)
                stale_minutes = int((heart_cfg or {}).get("stale_minutes") or 30)
                if measured_at and age_seconds <= stale_minutes * 60:
                    cat = category_label(classify_heart_rate(int(hr), heart_cfg))
                    parts.append(f"心率:{hr}({cat})")
                else:
                    parts.append(f"心率:{hr}(手环数据过期，可能未佩戴或没电，可以提醒她带好，以便监测健康数据。)")
            except Exception:
                parts.append(f"心率:{hr}")

        parts.append(
            f"今日步数:{int(mi_band.get('todaySteps') or 0)} | "
            f"活动:{int(mi_band.get('activityMinutes') or 0)}分钟"
        )
        parts.append(
            f"最近30分钟：活动{int(mi_band.get('recent30ActivityMinutes') or 0)}分钟，"
            f"{int(mi_band.get('recent30Steps') or 0)}步"
        )
        parts.append(
            f"最近60分钟：活动{int(mi_band.get('recent60ActivityMinutes') or 0)}分钟，"
            f"{int(mi_band.get('recent60Steps') or 0)}步"
        )

        sleep = mi_band.get("sleep") or {}
        total = int(sleep.get("totalMin") or 0)
        deep = int(sleep.get("deepMin") or 0)
        light = int(sleep.get("lightMin") or 0)
        rem = int(sleep.get("remMin") or 0)
        if deep or light or rem:
            if total:
                parts.append(f"睡眠:总计{total}m")
            sessions = sleep.get("sessions") or []
            for session in sessions:
                start_at = session.get("startAt")
                end_at = session.get("endAt")
                if not start_at or not end_at:
                    continue
                label = "小睡" if session.get("kind") == "nap" else "主睡"
                start_text = datetime.fromtimestamp(float(start_at)).strftime("%H:%M")
                end_text = datetime.fromtimestamp(float(end_at)).strftime("%H:%M")
                session_min = int(session.get("totalMin") or 0)
                parts.append(f"{label}:{start_text}-{end_text} | {session_min}m")
            sleep_stage_parts = []
            if deep: sleep_stage_parts.append(f"深睡:{deep}m")
            if light: sleep_stage_parts.append(f"浅睡:{light}m")
            if rem: sleep_stage_parts.append(f"REM:{rem}m")
            if sleep_stage_parts:
                parts.append(" | ".join(sleep_stage_parts))

        if weight_row:
            parts.append(f"体重:{weight_row['weight_kg']}kg")

        if period_row:
            start = period_row["start_date"]
            end = period_row["end_date"]
            if end:
                from datetime import date as _date
                try:
                    days = (_date.fromisoformat(end) - _date.fromisoformat(start)).days + 1
                    parts.append(f"上次例假:{start} 持续{days}天")
                except Exception:
                    parts.append(f"上次例假:{start}")
            else:
                parts.append(f"上次例假:{start}")

        if not parts:
            return ""
        return "\n\n[用户健康数据]\n" + "\n".join(parts)
    except Exception:
        return ""


async def build_ability_block(
    user_name: str,
    *,
    whisper_mode: bool = False,
    include_private_whisper: bool = False,
    include_video_call: bool = True,
    include_image_gen: bool = True,
    who: str = "aion",
    model_key: str | None = None,
    excluded_capabilities: set[str] | None = None,
) -> str:
    """构建 [系统能力] 文本块，who 参数用于 Connor 等角色的细微措辞差异"""
    parts = []
    abilities = await build_capability_prompt_items(
        user_name,
        whisper_mode=whisper_mode,
        include_private_whisper=include_private_whisper,
        include_video_call=include_video_call,
        include_image_gen=include_image_gen,
        who=who,
        excluded_capabilities=excluded_capabilities,
    )
    if abilities:
        parts.append(format_ability_block(abilities))

    if is_capability_enabled("schedule"):
        schedules = await get_active_schedules()
        schedule_text = build_schedule_prompt(schedules)
        parts.append(f"【当前日程列表】\n{schedule_text}")

    if is_capability_enabled("location_context"):
        try:
            from location import format_location_for_prompt, load_location_config
            loc_cfg = load_location_config()
            if loc_cfg.get("enabled"):
                loc_prompt = format_location_for_prompt()
                if loc_prompt:
                    parts.append(f"【位置信息】\n{loc_prompt}")
        except Exception:
            pass

    device_context_text = get_device_context_for_prompt()
    if device_context_text:
        parts.append(device_context_text)

    cli_file_text = build_cli_file_storage_text(model_key)
    if cli_file_text:
        parts.append(cli_file_text.strip())

    return "\n\n".join(parts)


def _memory_debug_item(mem: dict, max_content: int | None = None) -> dict:
    if not isinstance(mem, dict):
        return {}

    def _number(value, default=0.0):
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            return default

    return {
        "content": str(mem.get("content") or "") if max_content is None else str(mem.get("content") or "")[:max_content],
        "type": str(mem.get("type") or mem.get("scope") or mem.get("memory_kind") or ""),
        "score": _number(mem.get("score")),
        "vec_sim": _number(mem.get("vec_sim")),
        "kw_score": _number(mem.get("kw_score")),
        "importance": _number(mem.get("importance"), 0.5),
    }


def _clean_recall_snippet(text: str, max_len: int = 80) -> str:
    text = META_TAG_PATTERN.sub("", str(text or ""))
    text = re.sub(r"\[\[image:[^\]]+\]\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def _build_recall_query(
    topic: str,
    keywords: list,
    query_text: str = "",
    recent_messages: list[dict] = None,
    status: str = "",
) -> str:
    """Prefer sentinel clues, falling back to the latest user message."""
    if isinstance(keywords, str):
        keywords = [k.strip() for k in re.split(r"[,，、\s]+", keywords) if k.strip()]
    keyword_text = " ".join(str(k).strip() for k in (keywords or []) if str(k).strip())

    base = str(topic or "").strip()
    base_from_keywords = False
    if not base and keyword_text:
        base = f"当前话题：{keyword_text}"
        base_from_keywords = True
    if not base:
        return str(query_text or "").strip()

    if base_from_keywords:
        return base.strip()
    if keyword_text and keyword_text in base:
        return base.strip()
    return f"{base} {keyword_text}".strip()


async def build_memory_blocks(
    query_text: str,
    recent_messages: list[dict] = None,
    *,
    use_main_memories: bool = True,
    chatroom_recall_fn=None,
    chatroom_surfacing_fn=None,
    chatroom_source_fn=None,
    skip_digest: bool = False,
    digest_result: dict = None,
    always_include_recalled: bool = False,
    include_source_details: bool = True,
    max_recalled_memories: int = 8,
) -> dict:
    """
    执行 instant_digest + 记忆召回，返回注入用的文本块和调试信息。

    参数:
      query_text: 最后一条用户消息文本
      recent_messages: 最近 3 条对话（用于 instant_digest）
      use_main_memories: 是否使用 Aion 主记忆库
      chatroom_recall_fn: 可选的聊天室记忆召回函数 async (query, keywords) -> list
      chatroom_surfacing_fn: 可选的聊天室背景浮现函数 async (topic, keywords) -> (list, set)
      chatroom_source_fn: 可选的聊天室原文追溯函数 async (memories, keywords) -> str
      skip_digest: 跳过 instant_digest（快速模式）
      digest_result: 外部传入的 digest 结果（复用同一次调用）
      always_include_recalled: 已弃用的兼容参数；摘要记忆现在每轮都会按相关度注入

    返回 dict:
      time_block: str — 当前时间 + 背景记忆文本
      memory_block: str — 相关记忆 + 记忆证据文本（可能为空）
      digest_result: dict — instant_digest 的结果
    """
    now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
    time_block = f"系统当前的准确时间是 {now_str}"
    # 健康数据摘要
    health_text = await build_health_summary()
    if health_text:
        time_block += health_text
    memory_block = ""

    if skip_digest:
        return {"time_block": time_block, "memory_block": "", "digest_result": {}}

    # 如果没有外部传入 digest_result，自己跑一次
    if digest_result is None and recent_messages:
        digest_result = await instant_digest(recent_messages)
    elif digest_result is None:
        digest_result = {"is_search_needed": False, "keywords": [], "topic": ""}

    recall_keywords = digest_result.get("keywords", [])
    topic = digest_result.get("topic", "")
    status = digest_result.get("status", "")
    is_search_needed = digest_result.get("is_search_needed", False)

    recall_query = _build_recall_query(
        topic,
        recall_keywords,
        query_text=query_text,
        recent_messages=recent_messages,
        status=status,
    )

    # 并行执行：背景浮现 + 向量召回 + 聊天室记忆
    surfaced = []
    surfaced_ids = set()
    main_candidates = []
    chatroom_mems = []

    tasks = []
    task_labels = []  # 跟踪每个 task 对应的功能

    if use_main_memories:
        tasks.append(build_surfacing_memories(topic, recall_keywords))
        task_labels.append("main_surfacing")
        if recall_query:
            tasks.append(recall_memories(recall_query, query_keywords=recall_keywords))
        else:
            async def _empty_recall():
                return ([], [])
            tasks.append(_empty_recall())
        task_labels.append("main_recall")
    elif chatroom_surfacing_fn:
        tasks.append(chatroom_surfacing_fn(topic, recall_keywords))
        task_labels.append("chatroom_surfacing")

    if chatroom_recall_fn and recall_query:
        tasks.append(chatroom_recall_fn(recall_query, recall_keywords))
        task_labels.append("chatroom_recall")

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, label in enumerate(task_labels):
        if isinstance(results[i], Exception):
            continue
        if label == "main_surfacing":
            surfaced, surfaced_ids = results[i]
        elif label == "chatroom_surfacing":
            surfaced, surfaced_ids = results[i]
        elif label == "main_recall":
            _, main_candidates = results[i]
        elif label == "chatroom_recall":
            chatroom_mems = results[i]

    # 背景记忆
    if surfaced:
        unresolved_lines = [
            f"📌 {format_recalled_memories_for_prompt([m])[2:]}（还没做/还没去）"
            for m in surfaced
            if m.get("unresolved")
        ]
        normal_lines = [
            format_recalled_memories_for_prompt([m])
            for m in surfaced
            if not m.get("unresolved")
        ]
        mem_text = "\n".join(unresolved_lines + normal_lines)
        time_block += f"\n\n[背景记忆]\n以下是你记得的近期事件和需要关注的事项，在对话中如果有关联可以自然提起：\n{mem_text}"

    # RAG 摘要召回；always_include_recalled 用于需要每轮主动带摘要记忆的上下文。
    recalled = []
    if recall_query:
        # 主记忆库
        if main_candidates:
            recalled = [r for r in main_candidates if r["score"] >= 0.45 and r["id"] not in surfaced_ids][:5]
        # 聊天室记忆合并
        if chatroom_mems:
            seen_content = {m["content"][:100] for m in recalled}
            for m in chatroom_mems:
                if m.get("content", "")[:100] not in seen_content:
                    recalled.append(m)
                    seen_content.add(m["content"][:100])
            recalled = recalled[:max_recalled_memories]

    if recalled:
        mem_lines = format_recalled_memories_for_prompt(recalled, limit=200)
        memory_block = f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"
        if include_source_details and (
            is_search_needed or digest_result.get("require_detail")
        ):
            detail_text = ""
            if use_main_memories:
                detail_text = await fetch_source_details(
                    [r for r in recalled if r.get("source_start_ts")], recall_keywords
                )
            elif chatroom_source_fn:
                detail_text = await chatroom_source_fn(
                    [r for r in recalled if r.get("source_start_ts")], recall_keywords
                )
            if detail_text:
                memory_block += f"\n\n[记忆来源原文]\n以下是相关记忆挂载的来源原文；旧记忆没有精确来源时才会按时间范围回退筛选原文：\n{detail_text}"

    debug_candidates = []
    seen_debug = set()
    for mem in list(main_candidates or []) + list(chatroom_mems or []):
        key = mem.get("id") or str(mem.get("content", ""))[:100]
        if key in seen_debug:
            continue
        seen_debug.add(key)
        debug_candidates.append(mem)
    debug_candidates.sort(key=lambda m: float(m.get("score") or 0.0), reverse=True)

    debug_digest = dict(digest_result or {})
    debug_digest.update({
        "recall_query": recall_query,
        "recalled_memories": [_memory_debug_item(m) for m in recalled],
        "debug_top6": [_memory_debug_item(m) for m in debug_candidates[:6]],
    })

    return {
        "time_block": time_block,
        "memory_block": memory_block,
        "digest_result": debug_digest,
    }


# ══════════════════════════════════════════════════
#  统一时间线：合并私聊 + 群聊消息
# ══════════════════════════════════════════════════

# 系统消息过滤关键词（只保留包含这些关键词的系统消息）
SYSTEM_MSG_CONTEXT_KEYWORDS = ('搜索了', '点歌', '点了一首', '推荐了', '查看了动态', '视频通话', '环境语音', '刚刚完成了约会', '本条为微信消息')

# 聊天室图片标记 [[image:/uploads/xxx.jpg]] / [[image:/cr-uploads/xxx.jpg]]
# 这些标记会泄漏文件路径到 LLM 上下文，污染 instant_digest 关键词，
# 也会触发 Gemini CLI 的 agent 模式扫描文件，必须替换为干净占位符。
_CHATROOM_IMG_TAG_RE = re.compile(r'\[\[image:[^\]]+\]\]')
TIMELINE_IMAGE_CONTEXT_USER_TURNS = 2
_TIMELINE_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})


def _parse_timeline_attachments(raw) -> list:
    """把数据库 JSON 或现成列表统一为附件列表。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else []
        except Exception:
            return []
    return list(raw) if isinstance(raw, list) else []


def _is_timeline_image_attachment(attachment) -> bool:
    """判断附件是否为可在后续用户轮次继续提供给模型的图片。"""
    if isinstance(attachment, str):
        url = attachment.strip()
        type_hint = ""
    elif isinstance(attachment, dict):
        url = str(attachment.get("url") or "").strip()
        type_hint = str(
            attachment.get("type") or attachment.get("mime_type") or ""
        ).lower().strip()
    else:
        return False

    if not url:
        return False
    if type_hint == "image" or type_hint.startswith("image/"):
        return True

    clean_url = url.split("?", 1)[0].split("#", 1)[0].lower()
    return any(clean_url.endswith(ext) for ext in _TIMELINE_IMAGE_EXTENSIONS)


def _sanitize_timeline_content(content: str) -> str:
    """清理合并时间线中的图片路径标记，完全移除（不保留占位符）。"""
    if not content:
        return content
    cleaned = _CHATROOM_IMG_TAG_RE.sub('', content)
    # 清理留下的多余空白和空行
    cleaned = re.sub(r'[ \t]+\n', '\n', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def _is_model_visible_timeline_message(message: dict) -> bool:
    """Return whether render_merged_timeline can retain this transcript row."""
    if message.get("sender") != "system":
        return True
    attachments = _parse_timeline_attachments(message.get("attachments", []))
    if trip_card(attachments) is not None:
        return True
    explicitly_model_visible = any(
        isinstance(attachment, dict)
        and attachment.get("type") == "system_model_context"
        for attachment in attachments
    )
    if explicitly_model_visible:
        return True
    content = _sanitize_timeline_content(message.get("content", ""))
    return any(keyword in content for keyword in SYSTEM_MSG_CONTEXT_KEYWORDS)


def _merged_timeline_sources(
    who: str,
    *,
    conv_id: str = None,
    room_id: str = None,
    since_ts: float = None,
    until_ts: float = None,
) -> list[tuple[str, str, str, list[str], list]]:
    """Return the visible timeline sources and their bound filter parameters."""
    sources = []

    if who == "aion":
        conditions = ["role IN ('user','assistant','system')"]
        params = []
        if conv_id:
            conditions.append("conv_id=?")
            params.append(conv_id)
        if since_ts is not None:
            conditions.append("created_at >= ?")
            params.append(since_ts)
        if until_ts is not None:
            conditions.append("created_at <= ?")
            params.append(until_ts)
        sources.append((
            "private", "role", "FROM messages", conditions, params,
        ))
    elif who == "connor":
        conditions = ["r.type = 'connor_1v1'"]
        params = []
        if since_ts is not None:
            conditions.append("m.created_at >= ?")
            params.append(since_ts)
        if until_ts is not None:
            conditions.append("m.created_at <= ?")
            params.append(until_ts)
        sources.append((
            "private", "m.sender", "FROM chatroom_messages m "
            "JOIN chatroom_rooms r ON r.id = m.room_id", conditions, params,
        ))

    conditions = ["r.type = 'group'"]
    params = []
    if room_id:
        conditions.append("m.room_id=?")
        params.append(room_id)
    if since_ts is not None:
        conditions.append("m.created_at >= ?")
        params.append(since_ts)
    if until_ts is not None:
        conditions.append("m.created_at <= ?")
        params.append(until_ts)
    sources.append((
        "group", "m.sender", "FROM chatroom_messages m "
        "JOIN chatroom_rooms r ON r.id = m.room_id", conditions, params,
    ))
    return sources


async def _load_model_visible_merged_timeline(
    who: str,
    *,
    conv_id: str = None,
    room_id: str = None,
    since_ts: float = None,
    until_ts: float = None,
) -> list[dict]:
    """Load one bounded, model-visible merged transcript without limiting it."""
    results = []

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        source_filters = {
            source: (conditions, params)
            for source, _, _, conditions, params in _merged_timeline_sources(
                who,
                conv_id=conv_id,
                room_id=room_id,
                since_ts=since_ts,
                until_ts=until_ts,
            )
        }

        # ── 私聊消息 ──
        if who == "aion":
            private_conditions, private_params = source_filters["private"]
            cur = await db.execute(
                "SELECT role AS sender, content, created_at, attachments "
                "FROM messages "
                f"WHERE {' AND '.join(private_conditions)} "
                "ORDER BY created_at",
                private_params,
            )
            for r in await cur.fetchall():
                d = dict(r)
                d["source"] = "private"
                results.append(d)

        elif who == "connor":
            private_conditions, private_params = source_filters["private"]
            cur = await db.execute(
                "SELECT m.sender, m.content, m.created_at, m.attachments "
                "FROM chatroom_messages m "
                "JOIN chatroom_rooms r ON r.id = m.room_id "
                f"WHERE {' AND '.join(private_conditions)} "
                "ORDER BY m.created_at",
                private_params,
            )
            for r in await cur.fetchall():
                d = dict(r)
                d["source"] = "private"
                results.append(d)

        # ── 群聊消息 ──
        group_conditions, group_params = source_filters["group"]
        cur = await db.execute(
            "SELECT m.sender, m.content, m.created_at, m.attachments "
            "FROM chatroom_messages m "
            "JOIN chatroom_rooms r ON r.id = m.room_id "
            f"WHERE {' AND '.join(group_conditions)} "
            "ORDER BY m.created_at",
            group_params,
        )
        for r in await cur.fetchall():
            d = dict(r)
            d["source"] = "group"
            results.append(d)

    results.sort(key=lambda x: x["created_at"])
    return [
        message
        for message in results
        if _is_model_visible_timeline_message(message)
    ]


async def fetch_merged_timeline(
    who: str,
    limit: int,
    *,
    conv_id: str = None,
    room_id: str = None,
    since_ts: float = None,
    until_ts: float = None,
) -> list[dict]:
    """
    从私聊和群聊同时获取最近的模型可见消息，按时间升序返回。

    System-message eligibility is applied before the global latest-N limit,
    and since/until bounds describe one stable learning-day snapshot.
    """
    normalized_limit = max(0, int(limit))
    if normalized_limit == 0:
        return []
    results = await _load_model_visible_merged_timeline(
        who,
        conv_id=conv_id,
        room_id=room_id,
        since_ts=since_ts,
        until_ts=until_ts,
    )
    results = (
        results[-normalized_limit:]
        if len(results) > normalized_limit
        else results
    )
    cards = [(message, trip_card(_parse_timeline_attachments(message.get("attachments", []))))
             for message in results if message.get("sender") == "system"]
    trip_ids = {card["trip_id"] for _, card in cards if card and card.get("trip_id")}
    products = await load_trip_products(trip_ids)
    for message, card in cards:
        if card is not None:
            message["taobao_products"] = products.get(card.get("trip_id"))
    return results


async def count_merged_timeline(
    who: str,
    *,
    conv_id: str = None,
    room_id: str = None,
    since_ts: float = None,
    until_ts: float = None,
) -> int:
    """Count the same bounded, model-visible rows fetch_merged_timeline uses."""
    return len(
        await _load_model_visible_merged_timeline(
            who,
            conv_id=conv_id,
            room_id=room_id,
            since_ts=since_ts,
            until_ts=until_ts,
        )
    )


def render_merged_timeline(
    merged: list[dict],
    who: str,
    *,
    include_image_attachments: bool = True,
) -> list[dict]:
    """
    将合并时间线转换为 AI 上下文 history 格式。

    - who: "aion" — Aion 视角；"connor" — Connor 视角。
    - 历史消息统一渲染为 user 角色里的“历史消息 - 说话人：内容”，不再把历史里的
      Aion/Connor 渲染成 assistant。这样可以避免模型把群聊记录误当成需要续写的多人剧本。
    - 当存在混合来源时，仅在场景切换的那条消息内容前加一行内联标记。
    - 每条消息末尾仍带 <meta>发送时间：年月日 时分秒 [群聊/私聊]</meta>，模型仍能识别每条消息的时间和来源。
    - 系统消息按关键词过滤

    返回 [{"role": ..., "content": ..., "attachments": ...}]
    """
    merged = [
        message
        for message in merged
        if _is_model_visible_timeline_message(message)
    ]
    if not merged:
        return []

    user_name, ai_name, connor_name = _timeline_display_names()
    sources = set(m["source"] for m in merged)
    has_mixed = len(sources) > 1

    history: list[dict] = [{
        "role": "user",
        "content": (
            "[历史记录说明]\n"
            "以下消息按时间线排列，格式为“历史消息 - 说话人：内容”或“当前用户消息 - 说话人：内容”。"
            "这是上下文记录，不是你的回复格式；你回复时只用自己的口吻直接说话，"
            "不要输出说话人标签，也不要替其他人续写。"
        ),
    }]
    current_source = None
    pending_scene_marker = ""   # 待并入下一条消息内容的场景切换提示

    # 图片按用户发言计轮：发图当轮 + 后续一次用户发言，共两轮。
    # AI 与系统消息不会消耗图片上下文轮数。
    user_message_indices = [
        idx for idx, message in enumerate(merged)
        if message.get("sender") == "user"
    ]
    last_user_idx = user_message_indices[-1] if user_message_indices else None
    retained_image_user_indices = set(
        user_message_indices[-TIMELINE_IMAGE_CONTEXT_USER_TURNS:]
    )

    for idx, msg in enumerate(merged):
        source = msg["source"]
        sender = msg["sender"]
        content = _sanitize_timeline_content(msg.get("content", ""))
        message_attachments = _parse_timeline_attachments(
            msg.get("attachments", [])
        )
        card = trip_card(message_attachments) if sender == "system" else None
        if card is not None:
            content = render_trip_context(
                card, msg.get("taobao_products"), {"aion": ai_name, "connor": connor_name},
            )

        # ── 场景切换标记：不再插入 fake 应答对，仅记录下来在下一条消息前内联输出 ──
        if has_mixed and source != current_source:
            if current_source is not None:
                # 第二次及以后的切换才需要明示，第一次直接由首条消息的 [群聊/私聊] meta 表明即可
                label = "群聊" if source == "group" else "私聊"
                pending_scene_marker = f"（以下切换到{label}场景）\n"
            current_source = source

        # ── 说话人映射：所有历史记录都作为 user 侧 transcript 提供，避免多 assistant 污染输出格式 ──
        if sender == "system":
            speaker = "系统事件"
        elif sender == "user":
            speaker = user_name
        elif sender in ("assistant", "aion"):
            speaker = ai_name
        elif sender == "connor":
            speaker = connor_name
        else:
            speaker = sender or "未知说话人"
        label = "当前用户消息" if idx == last_user_idx and sender == "user" else "历史消息"
        content = f"{label} - {speaker}：{content}"
        role = "user"

        # ── 清洗旧 meta 标签 + 添加精确时间戳 ──
        if has_mixed:
            label = "群聊" if source == "group" else "私聊"
            content = append_message_meta(content, msg.get("created_at"), label)
        else:
            content = append_message_meta(content, msg.get("created_at"))

        # 把待写入的场景切换提示并入本条 content 开头
        if pending_scene_marker:
            content = pending_scene_marker + content
            pending_scene_marker = ""

        entry = {"role": role, "content": content}

        # ── 附件：普通对话保留最近两轮用户图片；后台唤醒可完全关闭历史图片 ──
        attachments = []
        if idx == last_user_idx:
            attachments = (
                message_attachments
                if include_image_attachments
                else [
                    attachment
                    for attachment in message_attachments
                    if not _is_timeline_image_attachment(attachment)
                ]
            )
        elif (
            include_image_attachments
            and idx in retained_image_user_indices
            and sender == "user"
        ):
            attachments = [
                attachment
                for attachment in message_attachments
                if _is_timeline_image_attachment(attachment)
            ]
        if attachments:
            entry["attachments"] = attachments

        history.append(entry)

    return history
