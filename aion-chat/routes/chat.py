"""
聊天核心路由：对话 CRUD、消息 CRUD、send_message、regenerate
"""

import json, time, asyncio, re, shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Any

from config import DEFAULT_MODEL, MODELS, load_worldbook, SETTINGS, UPLOADS_DIR, CODEX_UPLOADS_DIR, PUBLIC_DIR, resolve_model_key
from database import get_db
from ws import manager
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from memory import recall_memories, instant_digest, fetch_source_details, build_surfacing_memories, get_embedding, _pack_embedding, _memory_line_with_evidence
from camera import cam, CAM_CHECK_CMD, perform_cam_check
from activity import get_activity_summary_for_prompt, get_user_dynamics_for_prompt
from routes.files import export_conversation
from routes.music import MUSIC_CMD_PATTERN
from song_gen import SONG_CMD_PATTERN, clean_song_visible_reply
from tts import TTSStreamer

MOMENT_CMD_PATTERN = re.compile(r'\[MOMENT:(.+?)(?:\|(true|false))?\]')
MEMORY_CMD_PATTERN = re.compile(
    r'\[\s*M[\u200b\u200c\u200d\ufeff]*E[\u200b\u200c\u200d\ufeff]*M[\u200b\u200c\u200d\ufeff]*O[\u200b\u200c\u200d\ufeff]*R[\u200b\u200c\u200d\ufeff]*Y\s*[：:]\s*([^\]]+)\]',
    re.IGNORECASE,
)
ACTIVITY_CHECK_PATTERN = re.compile(r'\[查看动态:(\d+)\]')
SELFIE_CMD_PATTERN = re.compile(r'\[SELFIE:\s*([^\]]+)\]')
DRAW_CMD_PATTERN = re.compile(r'\[DRAW:\s*([^\]]+)\]')
TRANSFER_CMD_PATTERN = re.compile(r'\[转账[：:]\s*(-?\d+(?:\.\d+)?)\s*元\]')

# ── 活跃生成任务（用于 abort 取消） ──
active_generations: dict[str, asyncio.Event] = {}  # conv_id → cancel_event
VIDEO_CALL_CMD = '[视频电话]'
THEATER_STAT_PATTERN = re.compile(r'\[剧场属性[：:]([^\s]+)\s*([+\-＋－]\d+)\]')
THEATER_ITEM_PATTERN = re.compile(r'\[剧场道具[：:]([^\]]+)\]')

# 允许进入上下文的 system 消息关键词（点歌、查看监控、查看动态）
_SYSTEM_MSG_CONTEXT_KEYWORDS = ('查看了监控', '搜索了', '点歌', '点了一首', '推荐了', '查看了动态', '视频通话')
from context_builder import (
    fetch_merged_timeline, render_merged_timeline, build_health_summary,
    build_ability_block, WISH_CMD_PATTERN, _build_recall_query, strip_tool_commands,
)
from music import search_songs, get_audio_url
from schedule import (
    ALARM_CMD, MONITOR_CMD, REMINDER_CMD, SCHEDULE_DEL_CMD, SCHEDULE_LIST_CMD,
    process_schedule_commands,
)
from mcp_client import mcp_manager
from luckin import (
    LuckinOrderError,
    handle_luckin_commands,
    luckin_payment_attachments,
    query_luckin_order_detail,
)


def _process_voice_attachments_in_history(history: list, keep_idx: int = -1):
    """处理历史消息中的语音/视频附件：
    - 所有语音/视频消息的转写文本注入 content
    - keep_idx 位置的消息保留媒体 URL 用于 inline_data（-1 表示最后一条）
    - 其他消息移除所有附件
    """
    if keep_idx < 0:
        keep_idx = len(history) - 1
    for i, msg in enumerate(history):
        atts = msg.get("attachments", [])
        if not atts:
            if i != keep_idx:
                msg["attachments"] = []
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
            orig = msg["content"].strip() if msg["content"] else ""
            msg["content"] = vt + (f"\n{orig}" if orig else "")
        if is_kept:
            msg["attachments"] = non_media_atts
        else:
            msg["attachments"] = []


async def _insert_private_ability_block(
    history: list,
    cap_idx: int,
    inject_offset: int,
    *,
    user_name: str,
    model_key: str,
    whisper_mode: bool = False,
) -> int:
    ability_block = await build_ability_block(
        user_name,
        whisper_mode=whisper_mode,
        model_key=model_key,
    )
    if not ability_block:
        return inject_offset
    history.insert(cap_idx + inject_offset, {"role": "user", "content": ability_block})
    history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "好的，需要时我会使用这些指令。"})
    return inject_offset + 2

router = APIRouter()

POI_SEARCH_PATTERN = re.compile(r'\[POI_SEARCH:([^\]]+)\]')
TOY_CMD_PATTERN = re.compile(r'\[TOY:(\d|STOP)\]')
PET_CMD_PATTERN = re.compile(r'\[PET:([a-z_\-]+)\]', re.IGNORECASE)
HOME_CMD_PATTERN = re.compile(r'\[HOME:([^\]]+)\]', re.IGNORECASE)
MCP_CMD_PATTERN = re.compile(r'\[MCP:([^\]]+)\]', re.IGNORECASE)
META_TAG_PATTERN = re.compile(r'\s*<meta>.*?</meta>', re.DOTALL)
ORPHAN_HOME_ARGS_PATTERN = re.compile(
    r'(?im)^\s*[^\n\[]+\|(?:mode|hvac_mode|temperature|temp|fan_mode|fan|swing_mode|swing)\s*=[^\n\]]*\]?\s*$'
)
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_MD_IMAGE_PATTERN = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')
_MD_LINK_PATTERN = re.compile(r'(?<!!)\[[^\]]+\]\(([^)]+)\)')
_BARE_HTTP_IMAGE_PATTERN = re.compile(r'(?<!["\'(])https?://[^\s<>"\']+\.(?:png|jpe?g|gif|webp)(?:\?[^\s<>"\']*)?', re.I)
_BARE_LOCAL_IMAGE_PATTERN = re.compile(r'(?<![\w/])(?:[A-Za-z]:[\\/][^\s<>"\']+\.(?:png|jpe?g|gif|webp))', re.I)

TOY_PRESET_NAMES = {1:'微风轻拂',2:'春水初生',3:'暗流涌动',4:'如梦似幻',5:'情潮渐涨',6:'烈焰焚身',7:'极乐之巅',8:'魂飞魄散',9:'失控'}

HOME_ASSISTANT_SERVER_NAME = "Home Assistant"


def _visible_ai_text(text: str) -> str:
    """Clean local tool protocol tags from text that is shown/saved as a chat reply."""
    if not text:
        return ""

    transfers: list[str] = []

    def _hold_transfer(match: re.Match) -> str:
        transfers.append(match.group(0))
        return f"__AION_TRANSFER_{len(transfers) - 1}__"

    cleaned = TRANSFER_CMD_PATTERN.sub(_hold_transfer, text)
    cleaned = strip_tool_commands(cleaned)
    for pat in (ALARM_CMD, REMINDER_CMD, MONITOR_CMD, SCHEDULE_DEL_CMD, SCHEDULE_LIST_CMD):
        cleaned = pat.sub("", cleaned)
    cleaned = ORPHAN_HOME_ARGS_PATTERN.sub("", cleaned)
    cleaned = META_TAG_PATTERN.sub("", cleaned).strip()

    for idx, transfer in enumerate(transfers):
        cleaned = cleaned.replace(f"__AION_TRANSFER_{idx}__", transfer)
    return cleaned.strip()


def _chat_stream_event(model_key: str, full_text: str, chunk: str) -> dict[str, str]:
    provider = (MODELS.get(model_key) or {}).get("provider", "")
    if provider == "antigravity_cli":
        return {"type": "replace", "content": _visible_ai_text(full_text)}
    return {"type": "chunk", "content": chunk}


_AI_ERROR_PREFIXES = (
    "[Gemini错误",
    "[AntigravityCLI错误",
    "[硅基流动错误",
    "[中转站错误",
    "[错误]",
)


def _is_ai_error_text(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    if stripped.startswith(_AI_ERROR_PREFIXES):
        return True
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except Exception:
            return False
        return isinstance(payload, dict) and bool(payload.get("error"))
    return False


def _conversation_dict(row) -> dict:
    data = dict(row)
    data["model"] = resolve_model_key(data.get("model"))
    return data


def _parse_home_args(parts: list[str]) -> dict[str, str]:
    args: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            args[key] = value
    return args


def _decode_mcp_text(contents: Any) -> dict[str, Any]:
    if not isinstance(contents, list):
        return {"ok": False, "message": f"Unexpected MCP response: {contents!r}"}
    for item in contents:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text", "")
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            return {"ok": True, "message": text}
    return {"ok": False, "message": "MCP response did not contain text JSON."}


async def _call_home_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if not mcp_manager.is_connected(HOME_ASSISTANT_SERVER_NAME):
        await mcp_manager.connect(HOME_ASSISTANT_SERVER_NAME)
    contents = await mcp_manager.call_tool(HOME_ASSISTANT_SERVER_NAME, tool_name, arguments)
    return _decode_mcp_text(contents)


def _state_brief(entity: dict[str, Any]) -> str:
    state = entity.get("state", "unknown")
    attrs = entity.get("attributes") or {}
    bits = [f"状态 {state}"]
    temp = attrs.get("temperature")
    current_temp = attrs.get("current_temperature")
    unit = attrs.get("unit_of_measurement") or attrs.get("temperature_unit") or ""
    if current_temp is not None:
        bits.append(f"当前 {current_temp}{unit}")
    if temp is not None:
        bits.append(f"目标 {temp}{unit}")
    return "，".join(bits)


def _home_summary(action: str, alias: str, data: dict[str, Any]) -> str:
    if not data.get("ok"):
        return f"（智能家居：{alias} 操作失败：{data.get('message', '未知错误')}）"

    group_aliases = data.get("group_aliases") if isinstance(data.get("group_aliases"), list) else None
    entity = data.get("entity") if isinstance(data.get("entity"), dict) else None
    if action in {"state", "status", "read", "查看", "查询"} and group_aliases:
        return f"（智能家居：已读取 {alias}，共 {len(group_aliases)} 项。）"
    if action in {"state", "status", "read", "查看", "查询"} and entity:
        return f"（智能家居：{alias} 当前{_state_brief(entity)}。）"
    if action in {"on", "turn_on", "open", "打开", "开启"}:
        return f"（智能家居：已打开 {alias}。）"
    if action in {"off", "turn_off", "close", "关闭", "关掉"}:
        return f"（智能家居：已关闭 {alias}。）"
    if action in {"climate", "temperature", "temp", "set", "空调", "温度"}:
        if entity:
            return f"（智能家居：已调整 {alias}，现在{_state_brief(entity)}。）"
        return f"（智能家居：已调整 {alias}。）"
    return f"（智能家居：已处理 {alias}。）"


async def _process_home_commands(text: str) -> str:
    matches = HOME_CMD_PATTERN.findall(text)
    if not matches:
        return text

    cleaned = HOME_CMD_PATTERN.sub("", text).strip()
    summaries: list[str] = []

    for raw in matches:
        parts = [part.strip() for part in raw.split("|") if part.strip()]
        if len(parts) < 2:
            summaries.append("（智能家居：指令格式不完整，需要像 [HOME:on|客厅灯] 这样写。）")
            continue

        action = parts[0].lower()
        alias = parts[1]
        args = _parse_home_args(parts[2:])

        try:
            if action in {"state", "status", "read", "查看", "查询"}:
                data = await _call_home_tool("get_alias_state", {"alias": alias})
            elif action in {"on", "turn_on", "open", "打开", "开启"}:
                data = await _call_home_tool("turn_on_alias", {"alias": alias})
            elif action in {"off", "turn_off", "close", "关闭", "关掉"}:
                data = await _call_home_tool("turn_off_alias", {"alias": alias})
            elif action in {"climate", "temperature", "temp", "set", "空调", "温度"}:
                data = await _call_home_tool(
                    "set_climate_alias",
                    {
                        "alias": alias,
                        "hvac_mode": args.get("mode", args.get("hvac_mode", "")),
                        "temperature": args.get("temperature", args.get("temp", "")),
                        "fan_mode": args.get("fan_mode", args.get("fan", "")),
                        "swing_mode": args.get("swing_mode", args.get("swing", "")),
                    },
                )
            else:
                data = {
                    "ok": False,
                    "message": f"不支持的智能家居动作：{parts[0]}",
                }
        except Exception as exc:
            data = {"ok": False, "message": str(exc)}

        summaries.append(_home_summary(action, alias, data))

    if summaries:
        cleaned = (cleaned + "\n\n" if cleaned else "") + "\n".join(summaries)
    return cleaned.strip()


async def _call_mcp_tool(server_name: str, tool_name: str, arguments: dict) -> str:
    """通用 MCP 工具调用 — 返回 MCP Server 的原始文本响应（聚合所有 text 片段）。"""
    if not mcp_manager.is_connected(server_name):
        await mcp_manager.connect(server_name)
    contents = await mcp_manager.call_tool(server_name, tool_name, arguments)

    texts: list[str] = []
    for item in contents:
        if isinstance(item, dict) and item.get("type") == "text":
            t = (item.get("text") or "").strip()
            if t:
                texts.append(t)

    if not texts:
        return f"（无文本返回，原始结果: {str(contents)[:300]}）"
    return "\n".join(texts)


async def _process_mcp_commands(text: str, *, conv_id: str = "", ai_msg_id: str = "", model_key: str = "") -> str:
    """处理 AI 消息中的 [MCP:server|tool|arg1=val1|...] 指令。
    工具结果会通过延迟任务触发 AI 新一轮回复（类似 CAM_CHECK 模式）。"""
    matches = MCP_CMD_PATTERN.findall(text)
    if not matches:
        return text

    cleaned = MCP_CMD_PATTERN.sub("", text).strip()
    summaries: list[str] = []
    mcp_results: list[dict] = []  # 收集所有 MCP 调用结果

    for raw in matches:
        parts = [part.strip() for part in raw.split("|") if part.strip()]
        if len(parts) < 2:
            summaries.append("（MCP：指令格式不完整）")
            continue

        server = parts[0]
        tool = parts[1]
        args = _parse_home_args(parts[2:])

        try:
            available = mcp_manager.list_servers()
            names = [s["name"] for s in available]
            if server not in names:
                summaries.append(f"（MCP：未找到服务器「{server}」）")
                continue

            data = await _call_mcp_tool(server, tool, args)
            result_text = data if isinstance(data, str) else data.get("result", str(data))
            summaries.append(f"（MCP「{server}」.{tool} 已完成）")
            mcp_results.append({"server": server, "tool": tool, "result": result_text[:3000]})

        except Exception as exc:
            err_msg = str(exc)[:200]
            if "不支持的传输类型" in err_msg:
                err_msg += "（请重启 Aion）"
            summaries.append(f"（MCP「{server}」.{tool} 异常：{err_msg}）")

    # 触发延迟 AI 跟进回复（仿 CAM_CHECK 模式）
    if mcp_results and conv_id and model_key:
        asyncio.create_task(_delayed_mcp_followup(conv_id, model_key, mcp_results))

    if not cleaned and summaries:
        cleaned = "（正在处理…）"

    if summaries:
        cleaned = (cleaned + "\n\n" if cleaned else "") + "\n".join(summaries)
    return cleaned.strip()


async def _delayed_mcp_followup(conv_id: str, model_key: str, mcp_results: list[dict], delay: float = 1.0):
    """延迟后自动调用 AI 查看 MCP 工具结果并生成回复（仿 _delayed_cam_check）。"""
    await asyncio.sleep(delay)

    wb = load_worldbook()
    user_name = wb.get("user_name") or "用户"
    ai_name = wb.get("ai_name") or "AI"

    # 构建 MCP 结果文本
    result_blocks: list[str] = []
    for r in mcp_results:
        result_blocks.append(f"【{r['server']}.{r['tool']}】\n{r['result']}")
    mcp_result_text = "\n\n".join(result_blocks)

    # 构建人设前缀
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})

    # 获取最近对话上下文
    from context_builder import fetch_merged_timeline, render_merged_timeline
    merged = await fetch_merged_timeline("aion", 10, conv_id=conv_id)
    recent = render_merged_timeline(merged, "aion")

    # 组装 prompt
    mcp_prompt = (
        f"你刚才调用了一个外部工具，以下是工具返回的结果。"
        f"请根据这些结果自然地继续和{user_name}对话，"
        f"用你平时的语气描述你看到了什么、感受到了什么。"
        f"不需要说「让我看看」之类的话，直接基于结果回应即可。\n\n"
        f"{mcp_result_text}"
    )

    messages = prefix + recent + [{"role": "user", "content": mcp_prompt}]

    # 生成 AI 回复
    full_text = ""
    msg_id = f"msg_{int(time.time()*1000)}_mcp"
    try:
        async for chunk in stream_ai(messages, model_key, {}):
            if chunk.startswith(CLI_STATUS_PREFIX):
                continue
            full_text += chunk
    except Exception as e:
        full_text = f"[MCP 跟进失败] {e}"

    if not full_text.strip():
        return

    # 插入系统消息
    sys_now = time.time()
    sys_msg_id = f"msg_{int(sys_now*1000)}_mcp_sys"
    server_names = "、".join(set(r["server"] for r in mcp_results))
    sys_content = f"{ai_name}调用了 MCP 工具（{server_names}）"
    try:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (sys_msg_id, conv_id, "system", sys_content, sys_now, "[]"),
            )
            await db.commit()
        await manager.broadcast({"type": "msg_created", "data": {
            "id": sys_msg_id, "conv_id": conv_id, "role": "system",
            "content": sys_content, "created_at": sys_now, "attachments": [],
        }})
    except Exception:
        pass

    # 保存 AI 回复
    now = time.time()
    try:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (msg_id, conv_id, "assistant", full_text, now, "[]"),
            )
            await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
            await db.commit()
        await manager.broadcast({"type": "msg_created", "data": {
            "id": msg_id, "conv_id": conv_id, "role": "assistant",
            "content": full_text, "created_at": now, "attachments": [],
        }})
    except Exception:
        pass


async def _process_wish_commands(text: str, *, author: str, source_type: str, source_ref: str = "") -> str:
    matches = WISH_CMD_PATTERN.findall(text)
    if not matches:
        return text
    cleaned = WISH_CMD_PATTERN.sub("", text).strip()
    try:
        from wish_pool import create_wish
    except Exception as exc:
        print(f"[WISH_CMD] load failed: {exc}")
        return cleaned
    for raw_content in matches:
        content = raw_content.strip()
        if not content:
            continue
        try:
            await create_wish(
                author=author,
                content=content,
                visibility="shared",
                origin="chat_command",
                source_type=source_type,
                source_ref=source_ref,
            )
            print(f"[WISH_CMD] {author} wished: {content[:80]}")
        except Exception as exc:
            print(f"[WISH_CMD] create failed: {exc}")
    return cleaned


def _is_pet_available() -> bool:
    return bool(SETTINGS.get("pet_enabled", False) and manager.has_active_pet())

def _dedupe_attachments(items: list) -> list:
    seen = set()
    out = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out

def _clean_image_ref(ref: str) -> str:
    ref = (ref or "").strip().strip("<>").strip()
    if (ref.startswith('"') and ref.endswith('"')) or (ref.startswith("'") and ref.endswith("'")):
        ref = ref[1:-1].strip()
    if " " in ref and not ref.lower().startswith(("http://", "https://", "file://")):
        first, rest = ref.split(" ", 1)
        if rest.lstrip().startswith(('"', "'")):
            ref = first
    return ref

def _path_url_for_local_image(ref: str) -> str | None:
    raw = _clean_image_ref(ref).replace("\\", "/")
    lower = raw.lower()
    if lower.startswith(("http://", "https://")):
        path = urlparse(raw).path
        return raw if Path(path).suffix.lower() in _IMAGE_EXTS else None
    if raw.startswith("/uploads/") or raw.startswith("/cr-uploads/") or raw.startswith("/public/"):
        return raw
    if lower.startswith("file://"):
        parsed = urlparse(raw)
        local = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:/", local):
            local = local[1:]
        src = Path(local)
    else:
        src = Path(raw)
    if not src.exists() or src.suffix.lower() not in _IMAGE_EXTS:
        return None
    try:
        resolved = src.resolve()
    except Exception:
        resolved = src
    try:
        rel = resolved.relative_to(UPLOADS_DIR.resolve())
        return "/uploads/" + rel.as_posix()
    except Exception:
        pass
    try:
        rel = resolved.relative_to(CODEX_UPLOADS_DIR.resolve())
        return "/cr-uploads/" + rel.as_posix()
    except Exception:
        pass
    try:
        rel = resolved.relative_to(PUBLIC_DIR.resolve())
        return "/public/" + rel.as_posix()
    except Exception:
        pass
    dest_name = f"inline_{int(time.time()*1000)}_{src.name}"
    dest = UPLOADS_DIR / dest_name
    counter = 1
    while dest.exists():
        dest = UPLOADS_DIR / f"inline_{int(time.time()*1000)}_{counter}_{src.name}"
        counter += 1
    shutil.copy2(resolved, dest)
    return f"/uploads/{dest.name}"

def _extract_reply_image_attachments(text: str) -> tuple[str, list]:
    """Turn image refs in AI text into message attachments so mobile clients render them."""
    attachments = []
    ref_cache = {}

    def collect(ref: str):
        key = _clean_image_ref(ref).replace("\\", "/")
        if key in ref_cache:
            url = ref_cache[key]
        else:
            url = _path_url_for_local_image(ref)
            ref_cache[key] = url
        if url:
            attachments.append(url)

    def strip_md_image(match):
        collect(match.group(1))
        return ""

    def strip_md_link(match):
        ref = match.group(1)
        before = len(attachments)
        collect(ref)
        return "" if len(attachments) > before else match.group(0)

    cleaned = _MD_IMAGE_PATTERN.sub(strip_md_image, text or "")
    cleaned = _MD_LINK_PATTERN.sub(strip_md_link, cleaned)
    for match in _BARE_HTTP_IMAGE_PATTERN.finditer(cleaned):
        collect(match.group(0))
    for match in _BARE_LOCAL_IMAGE_PATTERN.finditer(cleaned):
        collect(match.group(0))
    cleaned = _BARE_HTTP_IMAGE_PATTERN.sub("", cleaned)
    cleaned = _BARE_LOCAL_IMAGE_PATTERN.sub("", cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned, _dedupe_attachments(attachments)


def _extract_song_gen_prompt(text: str) -> tuple[str, str | None]:
    match = SONG_CMD_PATTERN.search(text or "")
    cleaned = SONG_CMD_PATTERN.sub("", text or "").strip()
    if not match or not SETTINGS.get("song_gen_enabled", False):
        return cleaned, None
    prompt = match.group(1).strip()
    return cleaned, prompt or None

async def _toy_sys_msg(conv_id: str, commands: list):
    """为玩具指令插入系统消息"""
    wb = load_worldbook()
    ai_name = wb.get("ai_name", "AI")
    for cmd in commands:
        if cmd == 'STOP':
            text = f"❤️ {ai_name} 停止了玩具"
        else:
            n = int(cmd)
            name = TOY_PRESET_NAMES.get(n, f'档位{n}')
            text = f"❤️ {ai_name} · 心动{n} · {name}"
        now = time.time()
        msg_id = f"msg_{int(now*1000)}_toy"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (msg_id, conv_id, "system", text, now, "[]"),
            )
            await db.commit()
        msg = {"id": msg_id, "conv_id": conv_id, "role": "system",
               "content": text, "created_at": now, "attachments": []}
        await manager.broadcast({"type": "msg_created", "data": msg})

async def _video_call_incoming_sys_msg(conv_id: str):
    """AI 发起视频通话时插入系统消息"""
    wb = load_worldbook()
    ai_name = wb.get("ai_name", "AI")
    text = f"📹 {ai_name}打来了视频电话"
    now = time.time()
    msg_id = f"msg_{int(now*1000)}_vc_in"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "system", text, now, "[]"),
        )
        await db.commit()
    msg = {"id": msg_id, "conv_id": conv_id, "role": "system",
           "content": text, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": msg})

async def _video_call_outgoing_sys_msg(conv_id: str):
    """用户主动发起视频通话时插入系统消息"""
    text = "📹 你拨打了视频电话"
    now = time.time()
    msg_id = f"msg_{int(now*1000)}_vc_out"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "system", text, now, "[]"),
        )
        await db.commit()
    msg = {"id": msg_id, "conv_id": conv_id, "role": "system",
           "content": text, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": msg})

async def _video_call_sys_msg(conv_id: str, duration: int):
    """为视频通话插入系统消息，显示通话时长"""
    wb = load_worldbook()
    ai_name = wb.get("ai_name", "AI")
    mins = duration // 60
    secs = duration % 60
    dur_str = f"{mins:02d}:{secs:02d}"
    text = f"📹【{ai_name}视频通话 {dur_str}】"
    now = time.time()
    msg_id = f"msg_{int(now*1000)}_vc"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "system", text, now, "[]"),
        )
        await db.commit()
    msg = {"id": msg_id, "conv_id": conv_id, "role": "system",
           "content": text, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": msg})

async def _music_sys_msg(conv_id: str, music_cards: list):
    """为点歌操作插入系统消息，使后续上下文能看到点歌信息"""
    wb = load_worldbook()
    ai_name = wb.get("ai_name", "AI")
    parts = [f"《{s['name']}》- {s['artist']}" for s in music_cards]
    text = f"🎵 {ai_name}点了一首{' / '.join(parts)}"
    now = time.time()
    msg_id = f"msg_{int(now*1000)}_music"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "system", text, now, "[]"),
        )
        await db.commit()
    msg = {"id": msg_id, "conv_id": conv_id, "role": "system",
           "content": text, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": msg})

# ── Pydantic 模型 ─────────────────────────────────
class ConvCreate(BaseModel):
    title: str = "新对话"
    model: str = DEFAULT_MODEL

class ConvUpdate(BaseModel):
    title: Optional[str] = None
    model: Optional[str] = None

class MsgCreate(BaseModel):
    content: str
    context_limit: int = 30
    attachments: List[Any] = []
    whisper_mode: bool = False
    fast_mode: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tts_enabled: bool = False
    tts_voice: str = ""
    client_id: str = ""
    theater_session_id: str = ""

class MsgUpdate(BaseModel):
    content: str

class MessageFeedbackUpdate(BaseModel):
    rating: str
    reason: str

class MsgEditResend(BaseModel):
    content: str
    context_limit: int = 30
    whisper_mode: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tts_enabled: bool = False
    tts_voice: str = ""
    client_id: str = ""

# ── 对话 CRUD ─────────────────────────────────────
@router.get("/api/conversations")
async def list_conversations():
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conv_id = c.id AND m.role IN ('user','assistant')) AS message_count "
            "FROM conversations c ORDER BY c.updated_at DESC"
        )
        rows = await cur.fetchall()
        return [_conversation_dict(r) for r in rows]

@router.post("/api/conversations")
async def create_conversation(body: ConvCreate):
    now = time.time()
    conv_id = f"conv_{int(now*1000)}"
    model = resolve_model_key(body.model)
    async with get_db() as db:
        await db.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            (conv_id, body.title, model, now, now)
        )
        await db.commit()
    conv = {"id": conv_id, "title": body.title, "model": model, "created_at": now, "updated_at": now}
    await manager.broadcast({"type": "conv_created", "data": conv})
    await export_conversation(conv_id)
    return conv

@router.put("/api/conversations/{conv_id}")
async def update_conversation(conv_id: str, body: ConvUpdate):
    model = resolve_model_key(body.model) if body.model is not None else None
    async with get_db() as db:
        if body.title is not None:
            await db.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                             (body.title, time.time(), conv_id))
        if model is not None:
            await db.execute("UPDATE conversations SET model=?, updated_at=? WHERE id=?",
                             (model, time.time(), conv_id))
        await db.commit()
    payload = body.dict(exclude_none=True)
    if model is not None:
        payload["model"] = model
    await manager.broadcast({"type": "conv_updated", "data": {"id": conv_id, **payload}})
    await export_conversation(conv_id)
    return {"ok": True}


@router.get("/api/luckin/order/{order_id}")
async def get_luckin_order_detail(order_id: str):
    try:
        return await query_luckin_order_detail(order_id)
    except LuckinOrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    from routes.files import delete_exported_file
    async with get_db() as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        await db.commit()
    await manager.broadcast({"type": "conv_deleted", "data": {"id": conv_id}})
    delete_exported_file(conv_id)
    return {"ok": True}

# ── 消息 CRUD ─────────────────────────────────────
@router.get("/api/conversations/{conv_id}/messages")
async def list_messages(conv_id: str, limit: int = Query(50, ge=1, le=500), before: Optional[float] = Query(None)):
    """获取消息，支持分页。limit=条数，before=时间戳(加载更早的消息)"""
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        if before:
            cur = await db.execute(
                "SELECT * FROM messages WHERE conv_id=? AND created_at<? ORDER BY created_at DESC LIMIT ?",
                (conv_id, before, limit)
            )
        else:
            cur = await db.execute(
                "SELECT * FROM messages WHERE conv_id=? ORDER BY created_at DESC LIMIT ?",
                (conv_id, limit)
            )
        rows = await cur.fetchall()
        rows = list(reversed(rows))  # 按时间正序返回
        result = []
        for r in rows:
            d = dict(r)
            d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            d["starred"] = d.get("starred") or 0
            result.append(d)
        return result

@router.delete("/api/messages/{msg_id}")
async def delete_message(msg_id: str):
    conv_id = None
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT * FROM messages WHERE id=?", (msg_id,))
        msg = await cur.fetchone()
        if msg:
            conv_id = msg["conv_id"]
            await db.execute("DELETE FROM messages WHERE id=?", (msg_id,))
            await db.commit()
            await manager.broadcast({"type": "msg_deleted", "data": {"id": msg_id, "conv_id": conv_id}})
    if conv_id:
        await export_conversation(conv_id)
    return {"ok": True}

@router.put("/api/messages/{msg_id}")
async def update_message(msg_id: str, body: MsgUpdate):
    conv_id = None
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        await db.execute("UPDATE messages SET content=? WHERE id=?", (body.content, msg_id))
        await db.commit()
        cur = await db.execute("SELECT * FROM messages WHERE id=?", (msg_id,))
        msg = await cur.fetchone()
        if msg:
            d = dict(msg)
            try: d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            except: d["attachments"] = []
            conv_id = d["conv_id"]
            await manager.broadcast({"type": "msg_updated", "data": d})
    if conv_id:
        await export_conversation(conv_id)
    return {"ok": True}

# ── 星标消息 ─────────────────────────────────────
@router.patch("/api/messages/{msg_id}/star")
async def toggle_star_message(msg_id: str):
    """切换消息星标状态"""
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT starred FROM messages WHERE id=?", (msg_id,))
        row = await cur.fetchone()
        if not row:
            return {"error": "message not found"}
        new_val = 0 if row["starred"] else 1
        await db.execute("UPDATE messages SET starred=? WHERE id=?", (new_val, msg_id))
        await db.commit()
        cur2 = await db.execute("SELECT * FROM messages WHERE id=?", (msg_id,))
        msg = await cur2.fetchone()
        d = dict(msg)
        try: d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
        except: d["attachments"] = []
        await manager.broadcast({"type": "msg_updated", "data": d})
    return {"ok": True, "starred": new_val}

@router.patch("/api/messages/{msg_id}/feedback")
async def update_message_feedback(msg_id: str, body: MessageFeedbackUpdate):
    rating = (body.rating or "").strip().lower()
    reason = (body.reason or "").strip()
    if rating not in ("like", "dislike"):
        raise HTTPException(status_code=400, detail="rating must be like or dislike")
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    now = time.time()
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT * FROM messages WHERE id=?", (msg_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="message not found")
        if row["role"] != "assistant":
            raise HTTPException(status_code=400, detail="only assistant messages can be rated")
        created_at = row["ai_feedback_created_at"] or now
        await db.execute(
            "UPDATE messages SET ai_feedback_rating=?, ai_feedback_reason=?, "
            "ai_feedback_created_at=?, ai_feedback_updated_at=? WHERE id=?",
            (rating, reason, created_at, now, msg_id),
        )
        await db.commit()
        cur2 = await db.execute("SELECT * FROM messages WHERE id=?", (msg_id,))
        msg = await cur2.fetchone()
        d = dict(msg)
        try:
            d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
        except:
            d["attachments"] = []
        await manager.broadcast({"type": "msg_updated", "data": d})
    return {"ok": True, "message": d}

@router.get("/api/starred-messages")
async def list_starred_messages():
    """获取所有星标消息，按时间倒序，附带对话标题"""
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute(
            "SELECT m.*, c.title AS conv_title FROM messages m "
            "LEFT JOIN conversations c ON m.conv_id = c.id "
            "WHERE m.starred = 1 ORDER BY m.created_at DESC"
        )
        rows = await cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try: d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            except: d["attachments"] = []
            result.append(d)
        return result

@router.get("/api/conversations/{conv_id}/messages-around/{msg_id}")
async def messages_around(conv_id: str, msg_id: str, limit: int = Query(25, ge=1, le=100)):
    """获取指定消息前后各 limit 条消息，用于跳转定位"""
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT created_at FROM messages WHERE id=?", (msg_id,))
        target = await cur.fetchone()
        if not target:
            return []
        ts = target["created_at"]
        # 取目标消息之前（含自身）的 limit 条
        cur_before = await db.execute(
            "SELECT * FROM messages WHERE conv_id=? AND created_at<=? ORDER BY created_at DESC LIMIT ?",
            (conv_id, ts, limit)
        )
        before = list(reversed(await cur_before.fetchall()))
        # 取目标消息之后的 limit 条
        cur_after = await db.execute(
            "SELECT * FROM messages WHERE conv_id=? AND created_at>? ORDER BY created_at ASC LIMIT ?",
            (conv_id, ts, limit)
        )
        after = await cur_after.fetchall()
        rows = before + list(after)
        result = []
        for r in rows:
            d = dict(r)
            try: d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            except: d["attachments"] = []
            result.append(d)
        return result

# ── 中止 AI 生成 ─────────────────────────────────
@router.post("/api/conversations/{conv_id}/abort")
async def abort_generation(conv_id: str):
    """中止正在进行的 AI 生成任务"""
    evt = active_generations.get(conv_id)
    if evt:
        evt.set()
        return {"ok": True}
    return {"ok": False, "error": "no active generation"}

# ── 编辑重新发送（更新消息 + 删后续 + AI 重新回复） ──
@router.post("/api/messages/{msg_id}/edit-resend")
async def edit_resend_message(msg_id: str, body: MsgEditResend):
    """编辑用户消息后重新发送：更新内容 → 删除后续消息 → AI 重新回复"""
    if body.client_id:
        manager.set_last_sender(body.client_id)
    # Aion 侧：用户在 Aion 私聊发消息
    manager.set_aion_last_active("private")

    # 1. 查出原消息信息
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT * FROM messages WHERE id=?", (msg_id,))
        orig = await cur.fetchone()
        if not orig:
            return {"error": "message not found"}
        conv_id = orig["conv_id"]
        msg_created_at = orig["created_at"]

        # 2. 更新消息内容
        await db.execute("UPDATE messages SET content=? WHERE id=?", (body.content, msg_id))

        # 3. 删除该消息之后的所有消息
        cur2 = await db.execute(
            "SELECT id FROM messages WHERE conv_id=? AND created_at>?",
            (conv_id, msg_created_at)
        )
        later_msgs = await cur2.fetchall()
        if later_msgs:
            await db.execute(
                "DELETE FROM messages WHERE conv_id=? AND created_at>?",
                (conv_id, msg_created_at)
            )
        await db.commit()

    # 广播更新和删除事件
    updated_d = dict(orig)
    updated_d["content"] = body.content
    try: updated_d["attachments"] = json.loads(updated_d.get("attachments") or "[]") if updated_d.get("attachments") else []
    except: updated_d["attachments"] = []
    await manager.broadcast({"type": "msg_updated", "data": updated_d})
    for lm in later_msgs:
        await manager.broadcast({"type": "msg_deleted", "data": {"id": lm["id"], "conv_id": conv_id}})

    # 4. 重新构建上下文并调用 AI（复用 send_message 的逻辑）
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT model FROM conversations WHERE id=?", (conv_id,))
        conv = await cur.fetchone()
        model_key = resolve_model_key(conv["model"] if conv else DEFAULT_MODEL)

    # ── 合并私聊 + 群聊消息为统一时间线 ──
    merged = await fetch_merged_timeline("aion", body.context_limit, conv_id=conv_id)
    history = render_merged_timeline(merged, "aion")

    # 只保留最后一条用户消息的图片附件 + 语音消息处理
    _process_voice_attachments_in_history(history)

    actual_recent = [m for m in history if m["role"] in ("user", "assistant")][-3:]

    wb = load_worldbook()
    ai_name = wb.get("ai_name") or "AI"
    user_name = wb.get("user_name") or "用户"
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt") and wb.get("system_prompt_enabled", True):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})
    if prefix:
        history = prefix + history

    cap_idx = len(prefix) if prefix else 0
    inject_offset = 0

    inject_offset = await _insert_private_ability_block(
        history,
        cap_idx,
        inject_offset,
        user_name=user_name,
        model_key=model_key,
        whisper_mode=body.whisper_mode,
    )

    # RAG 记忆召回
    recall_keywords_str = ""
    recalled = []
    detail_text = ""
    topic = ""
    is_search_needed = False
    recall_query = ""
    debug_top6 = []
    debug_top6_data = []
    debug_recalled = []

    digest_result = await instant_digest(actual_recent)
    recall_keywords = digest_result.get("keywords", [])
    recall_keywords_str = "、".join(recall_keywords) if recall_keywords else ""
    topic = digest_result.get("topic", "")
    is_search_needed = digest_result.get("is_search_needed", False)

    recall_query = _build_recall_query(
        topic,
        recall_keywords,
        query_text=body.content,
        recent_messages=actual_recent,
        status=digest_result.get("status", ""),
    )

    async def _do_surfacing():
        return await build_surfacing_memories(topic, recall_keywords)
    async def _do_recall():
        if recall_query:
            return await recall_memories(recall_query, query_keywords=recall_keywords)
        return [], []

    (surfaced, surfaced_ids), (_, debug_top6) = await asyncio.gather(
        _do_surfacing(), _do_recall()
    )

    now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
    bg_block = f"系统当前的准确时间是 {now_str}"
    health_text = await build_health_summary()
    if health_text:
        bg_block += health_text
    if surfaced:
        unresolved_lines = [f"📌 {_memory_line_with_evidence(m)[2:]}（还没做/还没去）" for m in surfaced if m.get("unresolved")]
        normal_lines = [_memory_line_with_evidence(m) for m in surfaced if not m.get("unresolved")]
        mem_text = "\n".join(unresolved_lines + normal_lines)
        bg_block += f"\n\n[背景记忆]\n以下是你记得的近期事件和需要关注的事项，在对话中如果有关联可以自然提起：\n{mem_text}"
    history.insert(cap_idx + inject_offset, {"role": "user", "content": bg_block})
    history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会在合适的时候自然提及。"})
    inject_offset += 2

    if is_search_needed and recall_query:
        recalled = [r for r in debug_top6 if r["score"] >= 0.45 and r["id"] not in surfaced_ids][:5]
        if digest_result.get("require_detail") and recalled:
            detail_text = await fetch_source_details(recalled, recall_keywords)

    debug_recalled = [{"content": m["content"], "type": m["type"], "score": m["score"],
                       "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                       "importance": m.get("importance")} for m in recalled] if recalled else []
    debug_top6_data = [{"content": m["content"], "score": m["score"],
                        "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                        "importance": m.get("importance")} for m in debug_top6] if debug_top6 else []
    if recalled:
        mem_lines = "\n".join([_memory_line_with_evidence(m) for m in recalled])
        mem_block = f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"
        if detail_text:
            mem_block += f"\n\n[记忆来源原文]\n以下是相关记忆挂载的来源原文；旧记忆没有精确来源时才会按时间范围回退筛选原文：\n{detail_text}"
        history.insert(cap_idx + inject_offset, {"role": "user", "content": mem_block})
        history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    debug_prompt = [{"role": m["role"], "content": m["content"]} for m in history]

    ai_msg_id = f"msg_{int(time.time()*1000)}"
    usage_meta: dict = {}

    _q: asyncio.Queue = asyncio.Queue()

    # 取消事件
    cancel_event = asyncio.Event()
    active_generations[conv_id] = cancel_event

    tts_streamer = None
    if body.tts_enabled and body.tts_voice:
        tts_streamer = TTSStreamer(ai_msg_id, body.tts_voice, manager)
    manager.set_tts_fallback(body.tts_enabled, body.tts_voice)

    async def _bg_generate():
        full_text = ""
        has_error = False
        try:
            await _q.put({"id": ai_msg_id, "type": "start"})
            try:
                async for chunk in stream_ai(history, model_key, usage_meta, max_tokens=body.max_tokens, cancel_event=cancel_event):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        await _q.put({"type": "cli_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                        continue
                    full_text += chunk
                    await _q.put(_chat_stream_event(model_key, full_text, chunk))
                    if tts_streamer:
                        tts_streamer.feed(chunk)
            except Exception as e:
                has_error = True
                error_text = f"\n[请求出错: {str(e)}]"
                full_text += error_text
                await _q.put({"type": "chunk", "content": error_text})

            stripped = full_text.strip()
            if not has_error and _is_ai_error_text(stripped):
                has_error = True

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

            toy_matches = TOY_CMD_PATTERN.findall(full_text)
            if toy_matches:
                full_text = TOY_CMD_PATTERN.sub("", full_text).strip()

            pet_matches = PET_CMD_PATTERN.findall(full_text)
            if pet_matches:
                full_text = PET_CMD_PATTERN.sub("", full_text).strip()

            cam_triggered = CAM_CHECK_CMD in full_text
            if cam_triggered:
                full_text = full_text.replace(CAM_CHECK_CMD, "").strip()

            activity_match = ACTIVITY_CHECK_PATTERN.search(full_text)
            activity_n = 0
            if activity_match:
                try:
                    activity_n = int(activity_match.group(1))
                except (ValueError, IndexError):
                    activity_n = 6
                activity_n = max(1, min(12, activity_n)) if activity_n > 0 else 6
                full_text = ACTIVITY_CHECK_PATTERN.sub("", full_text).strip()

            poi_matches = POI_SEARCH_PATTERN.findall(full_text)
            if poi_matches:
                full_text = POI_SEARCH_PATTERN.sub("", full_text).strip()

            video_call_triggered = VIDEO_CALL_CMD in full_text
            if video_call_triggered:
                full_text = full_text.replace(VIDEO_CALL_CMD, "").strip()

            selfie_match = SELFIE_CMD_PATTERN.search(full_text)
            draw_match = DRAW_CMD_PATTERN.search(full_text)
            image_gen_prompt = None
            image_gen_is_selfie = False
            if selfie_match:
                image_gen_prompt = selfie_match.group(1).strip()
                image_gen_is_selfie = True
                full_text = SELFIE_CMD_PATTERN.sub("", full_text).strip()
            elif draw_match:
                image_gen_prompt = draw_match.group(1).strip()
                full_text = DRAW_CMD_PATTERN.sub("", full_text).strip()

            full_text, song_gen_prompt = _extract_song_gen_prompt(full_text)
            if song_gen_prompt:
                full_text = clean_song_visible_reply(full_text)

            full_text = await process_schedule_commands(full_text, conv_id, after_msg_id=ai_msg_id)
            full_text = await _process_home_commands(full_text)
            full_text = await _process_mcp_commands(full_text, conv_id=conv_id, ai_msg_id=ai_msg_id, model_key=model_key)
            full_text, luckin_results = await handle_luckin_commands(full_text)

            moment_matches = MOMENT_CMD_PATTERN.findall(full_text)
            if moment_matches:
                full_text = MOMENT_CMD_PATTERN.sub("", full_text).strip()
                for mt_content, mt_reply in moment_matches:
                    mt_content = mt_content.strip()
                    if mt_content:
                        mt_now = time.time()
                        mt_id = f"mt_{int(mt_now*1000)}"
                        expect = 1 if mt_reply == "true" else 0
                        async with get_db() as mt_db:
                            await mt_db.execute(
                                "INSERT INTO moments (id, author, content, source_conv, source_msg_id, expect_reply, created_at) VALUES (?,?,?,?,?,?,?)",
                                (mt_id, "aion", mt_content, conv_id, ai_msg_id, expect, mt_now)
                            )
                            await mt_db.commit()
                        mt_data = {"type": "moment_new", "data": {
                            "id": mt_id, "author": "aion", "content": mt_content,
                            "expect_reply": expect, "created_at": mt_now,
                            "comments": [], "reactions": [],
                        }}
                        await _q.put(mt_data)
                        await manager.broadcast(mt_data)
                        if expect:
                            from routes.moments import _trigger_ai_replies
                            asyncio.create_task(_trigger_ai_replies(mt_id, exclude_author="aion"))

            memory_matches = MEMORY_CMD_PATTERN.findall(full_text)
            if memory_matches:
                full_text = MEMORY_CMD_PATTERN.sub("", full_text).strip()
                for mem_content in memory_matches:
                    mem_content = mem_content.strip()
                    if mem_content:
                        mem_now = time.time()
                        mem_id = f"mem_{int(mem_now*1000)}"
                        vec = await get_embedding(mem_content)
                        async with get_db() as mem_db:
                            await mem_db.execute(
                                "INSERT INTO memories (id, content, type, created_at, source_conv, embedding, keywords, importance, source_start_ts, source_end_ts, unresolved) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (mem_id, mem_content, "重要事件", mem_now, conv_id,
                                 _pack_embedding(vec) if vec else None, '', 0.5, None, None, 0)
                            )
                            await mem_db.commit()
                        mem_data = {"id": mem_id, "content": mem_content, "type": "重要事件",
                                    "created_at": mem_now, "keywords": "", "importance": 0.5,
                                    "source_start_ts": None, "source_end_ts": None}
                        await manager.broadcast({"type": "memory_added", "data": mem_data})
                        mr_data = {'type': 'memory_record', 'msg_id': ai_msg_id, 'content': mem_content, 'mem_id': mem_id}
                        await _q.put(mr_data)
                        await manager.broadcast({"type": "memory_record", "data": mr_data})

            full_text = await _process_wish_commands(
                full_text,
                author="aion",
                source_type="chat_command",
                source_ref=f"{conv_id}:{ai_msg_id}",
            )

            full_text = _visible_ai_text(full_text)

            # 检测 [转账：N元] 指令 — AI 转账入账（不从 full_text 中剥离，前端渲染卡片需要）
            transfer_matches = TRANSFER_CMD_PATTERN.findall(full_text)
            for t_amount_str in transfer_matches:
                try:
                    t_val = float(t_amount_str)
                    if t_val > 0:
                        _a_n = wb.get('ai_name', 'AI')
                        _u_n = wb.get('user_name', '用户')
                        async with get_db() as t_db:
                            t_now = time.time()
                            t_id = f"wt_{int(t_now*1000)}"
                            await t_db.execute(
                                "INSERT INTO bookkeeping (id, record_type, amount, description, created_at) VALUES (?,?,?,?,?)",
                                (t_id, 'wallet_ai', -t_val, f'{_a_n}转账给{_u_n} {t_val}元', t_now)
                            )
                            await t_db.commit()
                        await manager.broadcast({"type": "wallet_update"})
                        print(f"[WALLET] AI 转账: -{t_val}元")
                except (ValueError, Exception):
                    pass

            music_atts = [{"type": "music", "name": s["name"], "artist": s["artist"], "id": s["id"]} for s in music_cards] if music_cards else []
            full_text, image_atts = _extract_reply_image_attachments(full_text)
            reply_atts = _dedupe_attachments(music_atts + luckin_payment_attachments(luckin_results) + image_atts)
            att_json = json.dumps(reply_atts, ensure_ascii=False) if reply_atts else ""

            now2 = time.time()
            async with get_db() as db2:
                await db2.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments, reasoning_content) VALUES (?,?,?,?,?,?,?)",
                    (ai_msg_id, conv_id, "assistant", full_text, now2, att_json, usage_meta.get("reasoning_content", "").strip())
                )
                await db2.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                await db2.commit()

            ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant", "content": full_text, "created_at": now2, "attachments": reply_atts, "reasoning_content": usage_meta.get("reasoning_content", "").strip()}
            await manager.broadcast({"type": "msg_created", "data": ai_msg})
            await export_conversation(conv_id)

            if toy_matches:
                toy_data = {'type': 'toy_command', 'commands': toy_matches, 'msg_id': ai_msg_id}
                await _q.put(toy_data)
                await manager.broadcast({"type": "toy_command", "data": toy_data})
                await _toy_sys_msg(conv_id, toy_matches)

            if pet_matches and _is_pet_available():
                await manager.broadcast({"type": "pet_command", "data": {"action": pet_matches[-1].lower()}})

            if cam_triggered:
                cam_data = {'type': 'cam_check', 'conv_id': conv_id, 'model_key': model_key, 'msg_id': ai_msg_id}
                await _q.put(cam_data)
                await manager.broadcast({"type": "cam_check", "data": cam_data})
                asyncio.create_task(_delayed_cam_check(conv_id, model_key))

            if poi_matches:
                poi_data = {'type': 'poi_search', 'conv_id': conv_id, 'categories': poi_matches, 'msg_id': ai_msg_id}
                await _q.put(poi_data)
                await manager.broadcast({"type": "poi_search", "data": poi_data})
                asyncio.create_task(perform_poi_check(conv_id, model_key, poi_matches))

            if activity_n > 0:
                activity_data = {'type': 'activity_check', 'conv_id': conv_id, 'n': activity_n, 'msg_id': ai_msg_id}
                await _q.put(activity_data)
                await manager.broadcast({"type": "activity_check", "data": activity_data})
                asyncio.create_task(perform_activity_check(conv_id, model_key, activity_n))

            if video_call_triggered:
                vc_data = {'type': 'video_call_incoming', 'conv_id': conv_id, 'msg_id': ai_msg_id}
                await _q.put(vc_data)
                await _video_call_incoming_sys_msg(conv_id)
                asyncio.create_task(_delayed_video_call(vc_data))

            if music_cards:
                music_data = {'type': 'music', 'msg_id': ai_msg_id, 'cards': music_cards}
                await _q.put(music_data)
                await manager.broadcast({"type": "music", "data": music_data})
                await _music_sys_msg(conv_id, music_cards)

            if image_gen_prompt:
                ig_data = {'type': 'image_gen_start', 'conv_id': conv_id, 'msg_id': ai_msg_id, 'is_selfie': image_gen_is_selfie}
                await _q.put(ig_data)
                await manager.broadcast({"type": "image_gen_start", "data": ig_data})
                asyncio.create_task(_do_image_gen(conv_id, ai_msg_id, image_gen_prompt, image_gen_is_selfie))

            if song_gen_prompt:
                sg_data = {'type': 'song_gen_start', 'conv_id': conv_id, 'msg_id': ai_msg_id}
                await _q.put(sg_data)
                await manager.broadcast({"type": "song_gen_start", "data": sg_data})
                asyncio.create_task(_do_song_gen(conv_id, ai_msg_id, song_gen_prompt))

            debug_data = {
                "type": "debug",
                "model": model_key,
                "msg_id": ai_msg_id,
                "recall_keywords": recall_keywords_str,
                "recall_query": recall_query,
                "recall_topic": topic,
                "is_search_needed": is_search_needed,
                "recalled_memories": debug_recalled,
                "debug_top6": debug_top6_data,
                "prompt_messages": debug_prompt,
                "prompt_count": len(history),
                "usage": usage_meta if usage_meta else None,
                "has_error": has_error,
                "error_text": stripped if has_error else None,
            }
            await _q.put(debug_data)
            await manager.broadcast({"type": "debug", "data": debug_data})
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            active_generations.pop(conv_id, None)
            if tts_streamer:
                try:
                    await tts_streamer.flush()
                except Exception:
                    pass
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_generate())

    async def generate():
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

# ── 发送消息 + AI 回复（SSE 流式） ────────────────
@router.post("/api/conversations/{conv_id}/send")
async def send_message(conv_id: str, body: MsgCreate):
    # 记录最后发消息的客户端 ID
    if body.client_id:
        manager.set_last_sender(body.client_id)
    # Aion 侧：用户在 Aion 私聊发消息
    manager.set_aion_last_active("private")
    now = time.time()
    msg_id = f"msg_{int(now*1000)}"

    att_json = json.dumps(body.attachments, ensure_ascii=False) if body.attachments else "[]"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "user", body.content, now, att_json)
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    user_msg = {"id": msg_id, "conv_id": conv_id, "role": "user", "content": body.content,
                "created_at": now, "attachments": body.attachments}
    await manager.broadcast({"type": "msg_created", "data": user_msg})

    # 用户发消息时重置哨兵巡逻计时器
    cam.reset_patrol_timer()

    # 检测用户消息中的 [转账：N元] → 入账
    user_transfer_matches = TRANSFER_CMD_PATTERN.findall(body.content)
    for t_amount_str in user_transfer_matches:
        try:
            t_val = float(t_amount_str)
            _wb_t = load_worldbook()
            _u_name = _wb_t.get('user_name', '用户')
            _a_name = _wb_t.get('ai_name', 'AI')
            async with get_db() as t_db:
                t_now = time.time()
                t_id = f"wt_{int(t_now*1000)}"
                await t_db.execute(
                    "INSERT INTO bookkeeping (id, record_type, amount, description, created_at) VALUES (?,?,?,?,?)",
                    (t_id, 'wallet_user', t_val, f'{_u_name}转账 {t_val}元', t_now)
                )
                await t_db.commit()
            await manager.broadcast({"type": "wallet_update"})
            print(f"[WALLET] 用户转账: {t_val}元")
        except (ValueError, Exception):
            pass

    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT model FROM conversations WHERE id=?", (conv_id,))
        conv = await cur.fetchone()
        model_key = resolve_model_key(conv["model"] if conv else DEFAULT_MODEL)

    # ── 合并私聊 + 群聊消息为统一时间线 ──
    merged = await fetch_merged_timeline("aion", body.context_limit, conv_id=conv_id)
    history = render_merged_timeline(merged, "aion")

    # 只保留当前（最后一条）用户消息的图片附件，历史图片不带入上下文
    # 语音消息处理：历史语音消息用转写文本替代音频文件，当前消息保留音频原件
    _process_voice_attachments_in_history(history)

    # 即时哨兵：取最近实际对话用于状态更新 + 关键词提取
    # 语音消息此时 content 已包含转写文本，哨兵直接分析文本
    actual_recent = [m for m in history if m["role"] in ("user", "assistant")][-3:]

    wb = load_worldbook()
    ai_name = wb.get("ai_name") or "AI"
    user_name = wb.get("user_name") or "用户"
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt") and wb.get("system_prompt_enabled", True):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})
    if prefix:
        history = prefix + history

    # ── 构建注入块（顺序：prefix → 系统能力 → 当前时间 → 背景记忆 → 相关记忆 → 上下文）──
    # 人设+系统能力 内容稳定可命中缓存，当前时间为缓存分界点，之后全是动态内容

    cap_idx = len(prefix) if prefix else 0
    inject_offset = 0  # 记录已注入的消息对数，用于计算后续插入位置

    inject_offset = await _insert_private_ability_block(
        history,
        cap_idx,
        inject_offset,
        user_name=user_name,
        model_key=model_key,
        whisper_mode=body.whisper_mode,
    )

    # 1.5 注入剧场·场外求助上下文（如果有）
    theater_session = None
    if body.theater_session_id:
        from ghost_forest import load_session as gf_load_session, build_game_state_summary, save_session as gf_save_session, STAT_LABELS
        theater_session = gf_load_session(body.theater_session_id)
        if theater_session:
            state_summary = build_game_state_summary(theater_session)
            # 最近 1-2 轮剧情
            story = theater_session.get("story", [])
            recent_narration = ""
            for entry in story[-2:]:
                recent_narration += f"【第{entry['round']}轮】\n{entry.get('narration', '')}\n\n"
            # 当前选项
            last_story = story[-1] if story else None
            options_text = ""
            if last_story and last_story.get("options") and not last_story.get("chosen"):
                opts = []
                for opt in last_story["options"]:
                    stat_name = STAT_LABELS.get(opt.get("stat", ""), opt.get("stat", ""))
                    dc = opt.get("dc", 0)
                    opts.append(f"{opt['key']}. {opt['text']}（{stat_name} DC{dc}）" if dc > 0 else f"{opt['key']}. {opt['text']}（幸运裸骰）")
                options_text = "\n".join(opts)

            theater_block = f"""[剧场·场外求助]
你的伴侣正在玩「奥罗斯幽林」TRPG游戏，以下是当前状态：
{state_summary}

【当前剧情】
{recent_narration.strip()}"""
            if options_text:
                theater_block += f"\n\n【当前面临的选项】\n{options_text}"
            theater_block += """

如果你愿意帮助，可以在回复中使用以下指令（可多个）：
- [剧场属性：属性名 +N] 或 [剧场属性：属性名 -N]  修改属性（属性名可以是：hp、力量、敏捷、智力、魅力、幸运）
- [剧场道具：道具名]  赠送道具"""

            history.insert(cap_idx + inject_offset, {"role": "user", "content": theater_block})
            history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我了解当前的游戏状况了。"})
            inject_offset += 2

    # 2. 即时哨兵 + 记忆召回（fast_mode 时跳过以加快语音聊天响应）
    recall_keywords_str = ""
    recalled = []
    detail_text = ""
    topic = ""
    is_search_needed = False
    recall_query = ""
    debug_top6 = []
    debug_top6_data = []
    debug_recalled = []

    if body.fast_mode:
        # ── 快速模式：仅注入当前时间，跳过哨兵和记忆 ──
        now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
        bg_block = f"系统当前的准确时间是 {now_str}"
        health_text = await build_health_summary()
        if health_text:
            bg_block += health_text
        history.insert(cap_idx + inject_offset, {"role": "user", "content": bg_block})
        history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到。"})
        inject_offset += 2
    else:
        # ── 正常模式：完整 RAG 流程 ──
        digest_result = await instant_digest(actual_recent)
        recall_keywords = digest_result.get("keywords", [])
        recall_keywords_str = "、".join(recall_keywords) if recall_keywords else ""
        topic = digest_result.get("topic", "")
        is_search_needed = digest_result.get("is_search_needed", False)

        # 3. 并行执行：背景记忆浮现 + 向量召回（两者都只依赖 instant_digest 的结果，互不依赖）
        recall_query = _build_recall_query(
            topic,
            recall_keywords,
            query_text=body.content,
            recent_messages=actual_recent,
            status=digest_result.get("status", ""),
        )

        async def _do_surfacing():
            return await build_surfacing_memories(topic, recall_keywords)

        async def _do_recall():
            if recall_query:
                return await recall_memories(recall_query, query_keywords=recall_keywords)
            return [], []

        (surfaced, surfaced_ids), (_, debug_top6) = await asyncio.gather(
            _do_surfacing(), _do_recall()
        )

        # 注入当前时间（缓存分界点）+ 背景记忆（动态内容）
        now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
        bg_block = f"系统当前的准确时间是 {now_str}"
        health_text = await build_health_summary()
        if health_text:
            bg_block += health_text
        if surfaced:
            unresolved_lines = [f"📌 {_memory_line_with_evidence(m)[2:]}（还没做/还没去）" for m in surfaced if m.get("unresolved")]
            normal_lines = [_memory_line_with_evidence(m) for m in surfaced if not m.get("unresolved")]
            mem_text = "\n".join(unresolved_lines + normal_lines)
            bg_block += f"\n\n[背景记忆]\n以下是你记得的近期事件和需要关注的事项，在对话中如果有关联可以自然提起：\n{mem_text}"
        history.insert(cap_idx + inject_offset, {"role": "user", "content": bg_block})
        history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会在合适的时候自然提及。"})
        inject_offset += 2

        # 4. RAG 精确召回（与背景记忆去重，使用已并行获取的结果）
        if is_search_needed and recall_query:
            recalled = [r for r in debug_top6 if r["score"] >= 0.45 and r["id"] not in surfaced_ids][:5]
            # 如果需要补充记忆证据
            if digest_result.get("require_detail") and recalled:
                detail_text = await fetch_source_details(recalled, recall_keywords)

        debug_recalled = [{"content": m["content"], "type": m["type"], "score": m["score"],
                           "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                           "importance": m.get("importance")} for m in recalled] if recalled else []
        debug_top6_data = [{"content": m["content"], "score": m["score"],
                            "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                            "importance": m.get("importance")} for m in debug_top6] if debug_top6 else []
        # 5. 注入向量匹配到的相关记忆（在背景记忆之后，每次请求都可能不同）
        if recalled:
            mem_lines = "\n".join([_memory_line_with_evidence(m) for m in recalled])
            mem_block = f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"
            if detail_text:
                mem_block += f"\n\n[记忆来源原文]\n以下是相关记忆挂载的来源原文；旧记忆没有精确来源时才会按时间范围回退筛选原文：\n{detail_text}"
            history.insert(cap_idx + inject_offset, {"role": "user", "content": mem_block})
            history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    debug_prompt = [{"role": m["role"], "content": m["content"]} for m in history]

    ai_msg_id = f"msg_{int(time.time()*1000)}"
    usage_meta: dict = {}

    # ── 后台任务 + SSE 转发：AI 生成和保存在后台任务中完成，即使客户端断开也不丢失 ──
    _q: asyncio.Queue = asyncio.Queue()

    # 取消事件
    cancel_event = asyncio.Event()
    active_generations[conv_id] = cancel_event

    # 创建 TTS streamer（如果请求方开了 TTS）
    tts_streamer = None
    if body.tts_enabled and body.tts_voice:
        tts_streamer = TTSStreamer(ai_msg_id, body.tts_voice, manager)
    # 同步备用 TTS 状态，供 cam_check / schedule 等服务端触发场景使用
    manager.set_tts_fallback(body.tts_enabled, body.tts_voice)

    async def _bg_generate():
        """后台任务：AI 流式生成 → 后处理 → 存 DB → WS 广播。始终运行到结束。"""
        full_text = ""
        has_error = False
        try:
            await _q.put({"id": ai_msg_id, "type": "start"})
            try:
                async for chunk in stream_ai(history, model_key, usage_meta, max_tokens=body.max_tokens, cancel_event=cancel_event):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        await _q.put({"type": "cli_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                        continue
                    full_text += chunk
                    await _q.put(_chat_stream_event(model_key, full_text, chunk))
                    if tts_streamer:
                        tts_streamer.feed(chunk)
            except Exception as e:
                has_error = True
                error_text = f"\n[请求出错: {str(e)}]"
                full_text += error_text
                await _q.put({"type": "chunk", "content": error_text})

            # 检查 AI 返回的错误文本
            stripped = full_text.strip()
            if not has_error and _is_ai_error_text(stripped):
                has_error = True

            # 检测 [MUSIC:xxx] 指令 → 搜索歌曲并推送卡片数据
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

            # 检测 [TOY:x] 指令
            toy_matches = TOY_CMD_PATTERN.findall(full_text)
            if toy_matches:
                full_text = TOY_CMD_PATTERN.sub("", full_text).strip()

            # 检测 [PET:xxx] 桌宠指令
            pet_matches = PET_CMD_PATTERN.findall(full_text)
            if pet_matches:
                full_text = PET_CMD_PATTERN.sub("", full_text).strip()

            # 检测 [CAM_CHECK] 指令
            cam_triggered = CAM_CHECK_CMD in full_text
            if cam_triggered:
                full_text = full_text.replace(CAM_CHECK_CMD, "").strip()

            # 检测 [查看动态:n] 指令
            activity_match = ACTIVITY_CHECK_PATTERN.search(full_text)
            activity_n = 0
            if activity_match:
                try:
                    activity_n = int(activity_match.group(1))
                except (ValueError, IndexError):
                    activity_n = 6
                activity_n = max(1, min(12, activity_n)) if activity_n > 0 else 6
                full_text = ACTIVITY_CHECK_PATTERN.sub("", full_text).strip()

            # 检测 [POI_SEARCH:xxx] 指令 → 标记，后续触发自动搜索+追加回复
            poi_matches = POI_SEARCH_PATTERN.findall(full_text)
            if poi_matches:
                full_text = POI_SEARCH_PATTERN.sub("", full_text).strip()

            # 检测 [视频电话] 指令
            video_call_triggered = VIDEO_CALL_CMD in full_text
            if video_call_triggered:
                full_text = full_text.replace(VIDEO_CALL_CMD, "").strip()

            # 检测 [SELFIE:xxx] / [DRAW:xxx] 生图指令
            selfie_match = SELFIE_CMD_PATTERN.search(full_text)
            draw_match = DRAW_CMD_PATTERN.search(full_text)
            image_gen_prompt = None
            image_gen_is_selfie = False
            if selfie_match:
                image_gen_prompt = selfie_match.group(1).strip()
                image_gen_is_selfie = True
                full_text = SELFIE_CMD_PATTERN.sub("", full_text).strip()
            elif draw_match:
                image_gen_prompt = draw_match.group(1).strip()
                full_text = DRAW_CMD_PATTERN.sub("", full_text).strip()

            # 检测日程指令（[ALARM:...], [REMINDER:...], [Monitor:...], [SCHEDULE_DEL:...], [SCHEDULE_LIST]）
            full_text, song_gen_prompt = _extract_song_gen_prompt(full_text)
            if song_gen_prompt:
                full_text = clean_song_visible_reply(full_text)
            full_text = await process_schedule_commands(full_text, conv_id, after_msg_id=ai_msg_id)
            full_text = await _process_home_commands(full_text)
            full_text = await _process_mcp_commands(full_text, conv_id=conv_id, ai_msg_id=ai_msg_id, model_key=model_key)
            full_text, luckin_results = await handle_luckin_commands(full_text)

            # 检测 [MOMENT:...] 朋友圈指令
            moment_matches = MOMENT_CMD_PATTERN.findall(full_text)
            if moment_matches:
                full_text = MOMENT_CMD_PATTERN.sub("", full_text).strip()
                for mt_content, mt_reply in moment_matches:
                    mt_content = mt_content.strip()
                    if mt_content:
                        mt_now = time.time()
                        mt_id = f"mt_{int(mt_now*1000)}"
                        expect = 1 if mt_reply == "true" else 0
                        async with get_db() as mt_db:
                            await mt_db.execute(
                                "INSERT INTO moments (id, author, content, source_conv, source_msg_id, expect_reply, created_at) VALUES (?,?,?,?,?,?,?)",
                                (mt_id, "aion", mt_content, conv_id, ai_msg_id, expect, mt_now)
                            )
                            await mt_db.commit()
                        mt_data = {"type": "moment_new", "data": {
                            "id": mt_id, "author": "aion", "content": mt_content,
                            "expect_reply": expect, "created_at": mt_now,
                            "comments": [], "reactions": [],
                        }}
                        await _q.put(mt_data)
                        await manager.broadcast(mt_data)
                        if expect:
                            from routes.moments import _trigger_ai_replies
                            asyncio.create_task(_trigger_ai_replies(mt_id, exclude_author="aion"))

            # 检测 [MEMORY:xxx] 记忆录入指令
            memory_matches = MEMORY_CMD_PATTERN.findall(full_text)
            if memory_matches:
                full_text = MEMORY_CMD_PATTERN.sub("", full_text).strip()
                for mem_content in memory_matches:
                    mem_content = mem_content.strip()
                    if mem_content:
                        mem_now = time.time()
                        mem_id = f"mem_{int(mem_now*1000)}"
                        vec = await get_embedding(mem_content)
                        async with get_db() as mem_db:
                            await mem_db.execute(
                                "INSERT INTO memories (id, content, type, created_at, source_conv, embedding, keywords, importance, source_start_ts, source_end_ts, unresolved) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (mem_id, mem_content, "重要事件", mem_now, conv_id,
                                 _pack_embedding(vec) if vec else None, '', 0.5, None, None, 0)
                            )
                            await mem_db.commit()
                        mem_data = {"id": mem_id, "content": mem_content, "type": "重要事件",
                                    "created_at": mem_now, "keywords": "", "importance": 0.5,
                                    "source_start_ts": None, "source_end_ts": None}
                        await manager.broadcast({"type": "memory_added", "data": mem_data})
                        mr_data = {'type': 'memory_record', 'msg_id': ai_msg_id, 'content': mem_content, 'mem_id': mem_id}
                        await _q.put(mr_data)
                        await manager.broadcast({"type": "memory_record", "data": mr_data})
                        print(f"[MEMORY] AI 主动录入记忆: {mem_content[:50]}")

            full_text = await _process_wish_commands(
                full_text,
                author="aion",
                source_type="chat_command",
                source_ref=f"{conv_id}:{ai_msg_id}",
            )

            # 检测 [转账：N元] 指令 — AI 转账入账
            transfer_matches = TRANSFER_CMD_PATTERN.findall(full_text)
            for t_amount_str in transfer_matches:
                try:
                    t_val = float(t_amount_str)
                    if t_val > 0:
                        _a_n = wb.get('ai_name', 'AI')
                        _u_n = wb.get('user_name', '用户')
                        async with get_db() as t_db:
                            t_now = time.time()
                            t_id = f"wt_{int(t_now*1000)}"
                            await t_db.execute(
                                "INSERT INTO bookkeeping (id, record_type, amount, description, created_at) VALUES (?,?,?,?,?)",
                                (t_id, 'wallet_ai', -t_val, f'{_a_n}转账给{_u_n} {t_val}元', t_now)
                            )
                            await t_db.commit()
                        await manager.broadcast({"type": "wallet_update"})
                        print(f"[WALLET] AI 转账: -{t_val}元")
                except (ValueError, Exception):
                    pass

            # 检测剧场指令 [剧场属性：xxx ±N] / [剧场道具：xxx]
            theater_updates = []
            if theater_session:
                stat_name_map = {"hp": "hp", "HP": "hp", "力量": "str", "敏捷": "dex", "智力": "int", "魅力": "cha", "幸运": "lck"}
                theater_stat_matches = THEATER_STAT_PATTERN.findall(full_text)
                for stat_name, val_str in theater_stat_matches:
                    stat_name = stat_name.strip()
                    val = int(val_str.replace('＋', '+').replace('－', '-'))
                    stat_key = stat_name_map.get(stat_name)
                    if stat_key and val != 0:
                        ts = gf_load_session(body.theater_session_id)
                        if ts:
                            if stat_key == "hp":
                                ts["player"]["hp"] = max(0, min(ts["player"]["max_hp"], ts["player"]["hp"] + val))
                            else:
                                ts["player"]["stats"][stat_key] = max(1, ts["player"]["stats"].get(stat_key, 0) + val)
                            gf_save_session(ts)
                            label = stat_name if stat_name != "hp" else "HP"
                            theater_updates.append({"type": "stat", "name": label, "value": val})
                            print(f"[剧场] 属性变更: {label} {'+' if val > 0 else ''}{val}")

                theater_item_matches = THEATER_ITEM_PATTERN.findall(full_text)
                for item_name in theater_item_matches:
                    item_name = item_name.strip()
                    if item_name:
                        ts = gf_load_session(body.theater_session_id)
                        if ts:
                            found = False
                            for inv_item in ts.get("inventory", []):
                                if inv_item["name"] == item_name:
                                    inv_item["count"] += 1
                                    found = True
                                    break
                            if not found:
                                ts.setdefault("inventory", []).append({"name": item_name, "count": 1, "description": "场外援助获得"})
                            gf_save_session(ts)
                            theater_updates.append({"type": "item", "name": item_name})
                            print(f"[剧场] 道具赠送: {item_name}")

            # 清洗 AI 回复中模仿产生的 <meta> 标签
            full_text = _visible_ai_text(full_text)

            # 将音乐点歌信息存入 attachments，刷新后可显示胶囊
            music_atts = [{"type": "music", "name": s["name"], "artist": s["artist"], "id": s["id"]} for s in music_cards] if music_cards else []
            full_text, image_atts = _extract_reply_image_attachments(full_text)
            reply_atts = _dedupe_attachments(music_atts + luckin_payment_attachments(luckin_results) + image_atts)
            att_json = json.dumps(reply_atts, ensure_ascii=False) if reply_atts else ""

            now2 = time.time()
            async with get_db() as db2:
                await db2.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments, reasoning_content) VALUES (?,?,?,?,?,?,?)",
                    (ai_msg_id, conv_id, "assistant", full_text, now2, att_json, usage_meta.get("reasoning_content", "").strip())
                )
                await db2.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                await db2.commit()

            ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant", "content": full_text, "created_at": now2, "attachments": reply_atts, "reasoning_content": usage_meta.get("reasoning_content", "").strip()}
            await manager.broadcast({"type": "msg_created", "data": ai_msg})
            await export_conversation(conv_id)

            # 推送 [TOY:x] 指令到前端
            if toy_matches:
                toy_data = {'type': 'toy_command', 'commands': toy_matches, 'msg_id': ai_msg_id}
                await _q.put(toy_data)
                await manager.broadcast({"type": "toy_command", "data": toy_data})
                await _toy_sys_msg(conv_id, toy_matches)

            # 推送 [PET:xxx] 桌宠指令到前端
            if pet_matches and _is_pet_available():
                await manager.broadcast({"type": "pet_command", "data": {"action": pet_matches[-1].lower()}})

            # [CAM_CHECK] 服务端直接触发，前端只显示 UI 指示器
            if cam_triggered:
                cam_data = {'type': 'cam_check', 'conv_id': conv_id, 'model_key': model_key, 'msg_id': ai_msg_id}
                await _q.put(cam_data)
                await manager.broadcast({"type": "cam_check", "data": cam_data})
                asyncio.create_task(_delayed_cam_check(conv_id, model_key))

            # [POI_SEARCH] 搜索周边 → 携带结果自动追加一轮 Core 回复
            if poi_matches:
                poi_data = {'type': 'poi_search', 'conv_id': conv_id, 'categories': poi_matches, 'msg_id': ai_msg_id}
                await _q.put(poi_data)
                await manager.broadcast({"type": "poi_search", "data": poi_data})
                asyncio.create_task(perform_poi_check(conv_id, model_key, poi_matches))

            # [查看动态:n] 查看设备活动摘要 → 携带摘要自动追加一轮 Core 回复
            if activity_n > 0:
                activity_data = {'type': 'activity_check', 'conv_id': conv_id, 'n': activity_n, 'msg_id': ai_msg_id}
                await _q.put(activity_data)
                await manager.broadcast({"type": "activity_check", "data": activity_data})
                asyncio.create_task(perform_activity_check(conv_id, model_key, activity_n))

            # [视频电话] 延迟 10 秒后定向推送到最后发消息的客户端
            if video_call_triggered:
                vc_data = {'type': 'video_call_incoming', 'conv_id': conv_id, 'msg_id': ai_msg_id}
                await _q.put(vc_data)
                await _video_call_incoming_sys_msg(conv_id)
                asyncio.create_task(_delayed_video_call(vc_data))

            # 推送音乐卡片
            if music_cards:
                music_data = {'type': 'music', 'msg_id': ai_msg_id, 'cards': music_cards}
                await _q.put(music_data)
                await manager.broadcast({"type": "music", "data": music_data})
                await _music_sys_msg(conv_id, music_cards)

            if image_gen_prompt:
                ig_data = {'type': 'image_gen_start', 'conv_id': conv_id, 'msg_id': ai_msg_id, 'is_selfie': image_gen_is_selfie}
                await _q.put(ig_data)
                await manager.broadcast({"type": "image_gen_start", "data": ig_data})
                asyncio.create_task(_do_image_gen(conv_id, ai_msg_id, image_gen_prompt, image_gen_is_selfie))

            if song_gen_prompt:
                sg_data = {'type': 'song_gen_start', 'conv_id': conv_id, 'msg_id': ai_msg_id}
                await _q.put(sg_data)
                await manager.broadcast({"type": "song_gen_start", "data": sg_data})
                asyncio.create_task(_do_song_gen(conv_id, ai_msg_id, song_gen_prompt))

            debug_data = {
                "type": "debug",
                "model": model_key,
                "msg_id": ai_msg_id,
                "recall_keywords": recall_keywords_str,
                "recall_query": recall_query,
                "recall_topic": topic,
                "is_search_needed": is_search_needed,
                "recalled_memories": debug_recalled,
                "debug_top6": debug_top6_data,
                "prompt_messages": debug_prompt,
                "prompt_count": len(history),
                "usage": usage_meta if usage_meta else None,
                "has_error": has_error,
                "error_text": stripped if has_error else None,
            }

            # 推送剧场指令结果到前端
            if theater_updates:
                tu_data = {'type': 'theater_update', 'updates': theater_updates, 'session_id': body.theater_session_id, 'msg_id': ai_msg_id}
                await _q.put(tu_data)

            await _q.put(debug_data)
            await manager.broadcast({"type": "debug", "data": debug_data})
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            active_generations.pop(conv_id, None)
            if tts_streamer:
                try:
                    await tts_streamer.flush()
                except Exception:
                    pass
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_generate())

    async def generate():
        """SSE 转发：从队列读取事件转发给客户端。客户端断开时生成器关闭，后台任务不受影响。"""
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

# ── 异步生图任务 ─────────────────────────────────
async def _do_image_gen(conv_id: str, trigger_msg_id: str, prompt: str, is_selfie: bool):
    """异步调用 Gemini 生图，成功后作为新 assistant 消息保存并广播"""
    from image_gen import generate_image

    try:
        filename = await generate_image(prompt, is_selfie=is_selfie)
        if filename:
            # 生成成功 → 创建新的 assistant 消息（仅含图片）
            now = time.time()
            img_msg_id = f"msg_{int(now*1000)}_img"
            att_list = [f"/uploads/{filename}"]
            att_json = json.dumps(att_list, ensure_ascii=False)
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                    (img_msg_id, conv_id, "assistant", "", now, att_json)
                )
                await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
                await db.commit()
            img_msg = {"id": img_msg_id, "conv_id": conv_id, "role": "assistant", "content": "", "created_at": now, "attachments": att_list}
            await manager.broadcast({"type": "msg_created", "data": img_msg})
            # 通知前端生图完成（移除占位）
            await manager.broadcast({"type": "image_gen_done", "data": {"conv_id": conv_id, "trigger_msg_id": trigger_msg_id, "img_msg_id": img_msg_id}})
            await export_conversation(conv_id)
            print(f"[image_gen] 图片消息已创建: {img_msg_id}")
        else:
            # 生图失败 → 通知前端
            await manager.broadcast({"type": "image_gen_failed", "data": {"conv_id": conv_id, "trigger_msg_id": trigger_msg_id}})
            print("[image_gen] 生图失败，已通知前端")
    except Exception as e:
        print(f"[image_gen] 异步生图任务异常: {e}")
        await manager.broadcast({"type": "image_gen_failed", "data": {"conv_id": conv_id, "trigger_msg_id": trigger_msg_id}})


# ── 服务端延迟触发监控查看（不再依赖前端 API 调用） ─────
async def _do_song_gen(conv_id: str, trigger_msg_id: str, prompt: str):
    """Generate a song with Gemini Lyria and save it as a new assistant message."""
    from song_gen import generate_song

    try:
        result = await generate_song(prompt)
        if result:
            now = time.time()
            song_msg_id = f"msg_{int(now*1000)}_song"
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
            att_list = [attachment]
            att_json = json.dumps(att_list, ensure_ascii=False)
            content = f"为你写的歌《{title}》"
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                    (song_msg_id, conv_id, "assistant", content, now, att_json)
                )
                await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
                await db.commit()
            song_msg = {
                "id": song_msg_id,
                "conv_id": conv_id,
                "role": "assistant",
                "content": content,
                "created_at": now,
                "attachments": att_list,
            }
            await manager.broadcast({"type": "msg_created", "data": song_msg})
            await manager.broadcast({"type": "song_gen_done", "data": {"conv_id": conv_id, "trigger_msg_id": trigger_msg_id, "song_msg_id": song_msg_id}})
            await export_conversation(conv_id)
            print(f"[song_gen] Song message created: {song_msg_id}")
        else:
            await manager.broadcast({"type": "song_gen_failed", "data": {"conv_id": conv_id, "trigger_msg_id": trigger_msg_id}})
            print("[song_gen] Song generation failed")
    except Exception as e:
        print(f"[song_gen] Async song generation error: {e}")
        await manager.broadcast({"type": "song_gen_failed", "data": {"conv_id": conv_id, "trigger_msg_id": trigger_msg_id}})


_cam_check_active: set[str] = set()          # 去重：同一时间只允许一个 cam check

async def _delayed_cam_check(conv_id: str, model_key: str, delay: float = 5.0):
    """服务端延迟后直接执行监控查看，避免多客户端重复触发"""
    await asyncio.sleep(delay)
    if conv_id in _cam_check_active:
        return  # 已有一个在进行中
    _cam_check_active.add(conv_id)
    try:
        await perform_cam_check(conv_id, model_key)
    finally:
        _cam_check_active.discard(conv_id)

# ── [视频电话] 延迟 3 秒后定向推送到最后发消息的客户端 ─────
async def _delayed_video_call(vc_data: dict, delay: float = 3.0):
    """等待用户阅读完回复后，定向推送视频来电到最后发消息的客户端"""
    await asyncio.sleep(delay)
    # 优先定向推送，如果没有记录到最后发送者则广播到所有客户端
    if manager._last_sender_client_id:
        await manager.send_to_last_sender({"type": "video_call_ring", "data": vc_data})
    else:
        await manager.broadcast({"type": "video_call_ring", "data": vc_data})

# ── 视频通话结束系统消息 ─────
class VideoCallSysMsg(BaseModel):
    conv_id: str
    duration: int  # 通话时长（秒）

@router.post("/api/video-call-sys-msg")
async def video_call_sys_msg(body: VideoCallSysMsg):
    await _video_call_sys_msg(body.conv_id, body.duration)
    return {"ok": True}

class VideoCallInitSysMsg(BaseModel):
    conv_id: str
    direction: str = "outgoing"  # outgoing = 用户拨出

@router.post("/api/video-call-init-sys-msg")
async def video_call_init_sys_msg(body: VideoCallInitSysMsg):
    await _video_call_outgoing_sys_msg(body.conv_id)
    return {"ok": True}

# 保留 API 端点兼容旧客户端，但加严格去重
class CamCheckTrigger(BaseModel):
    conv_id: str
    model_key: str

@router.post("/api/cam-check-trigger")
async def cam_check_trigger(body: CamCheckTrigger):
    if body.conv_id in _cam_check_active:
        return {"ok": False, "error": "cam check already in progress"}
    _cam_check_active.add(body.conv_id)
    asyncio.create_task(_guarded_cam_check(body.conv_id, body.model_key))
    return {"ok": True}

async def _guarded_cam_check(conv_id: str, model_key: str):
    try:
        await perform_cam_check(conv_id, model_key)
    finally:
        _cam_check_active.discard(conv_id)


# ── 服务端 POI 搜索 + 自动追加 Core 回复 ─────────
async def perform_poi_check(conv_id: str, model_key: str, categories: list[str]):
    """Core 主动搜索周边 POI：拿最新坐标 → 搜索 → 携带结果自动追加一轮 Core 回复"""
    from location import (
        load_location_config, load_location_status, save_location_status,
        amap_poi_search, amap_regeo, format_location_for_prompt,
    )

    cfg = load_location_config()
    amap_key = cfg.get("amap_key", "")
    if not amap_key:
        return

    # 1. 取最新坐标（直接用缓存的最新 GPS 上报坐标，而不是上次 API 坐标）
    status = load_location_status()
    lng = status.get("lng", 0)
    lat = status.get("lat", 0)
    if not lng or not lat:
        return

    # 2. 用最新坐标重新做逆地理编码，更新地址
    geo_info = await amap_regeo(lng, lat, amap_key)
    if geo_info:
        status["address"] = geo_info["address"]
        status["adcode"] = geo_info["adcode"]

    # 3. 搜索用户指定的 POI 类别
    poi_types = cfg.get("poi_types", {})
    search_results = {}
    for cat in categories:
        cat = cat.strip()
        type_code = poi_types.get(cat)
        if type_code:
            pois = await amap_poi_search(lng, lat, type_code, amap_key, cfg.get("poi_radius", 2000))
            search_results[cat] = pois
            # 更新缓存
            if "nearby_pois" not in status:
                status["nearby_pois"] = {}
            status["nearby_pois"][cat] = pois

    # 更新 last_api 坐标
    status["last_api_lng"] = lng
    status["last_api_lat"] = lat
    save_location_status(status)

    if not search_results:
        return

    # 4. 格式化搜索结果
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

    # 5. 构建消息上下文，携带 POI 搜索结果，让 Core 追加一轮回复
    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")

    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt") and wb.get("system_prompt_enabled", True):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})

    # 获取最近对话上下文
    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content, attachments FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 6",
            (conv_id,)
        )
        rows = await cur.fetchall()
    recent = []
    for r in reversed(rows):
        _c = r["content"]
        try:
            _atts = json.loads(r["attachments"] or "[]") if r["attachments"] else []
        except Exception:
            _atts = []
        for _a in _atts:
            if isinstance(_a, dict) and _a.get("type") == "voice" and _a.get("transcript"):
                _orig = _c.strip() if _c else ""
                _c = f"[语音消息] {_a['transcript']}" + (f"\n{_orig}" if _orig else "")
            elif isinstance(_a, dict) and _a.get("type") == "video_clip" and _a.get("transcript"):
                _orig = _c.strip() if _c else ""
                _c = f"[视频通话] {_a['transcript']}" + (f"\n{_orig}" if _orig else "")
        recent.append({"role": r["role"], "content": _c, "attachments": []})

    loc_prompt = format_location_for_prompt()
    poi_prompt = (
        f"你刚才想帮{user_name}搜索周边信息，以下是系统根据{user_name}最新实时坐标搜索到的结果：\n\n"
        f"{poi_text}\n\n"
        f"{loc_prompt}\n\n"
        f"请根据搜索结果，自然地向{user_name}推荐或回答。不需要再说\"让我帮你搜一下\"之类的话，直接根据结果回复即可。"
    )
    messages = prefix + recent + [
        {"role": "user", "content": poi_prompt}
    ]

    # 预生成 msg_id + TTS
    msg_id = f"msg_{int(time.time()*1000)}_poi"
    poi_tts = None
    if manager.any_tts_enabled():
        tts_voice = manager.get_tts_voice()
        if tts_voice:
            poi_tts = TTSStreamer(msg_id, tts_voice, manager)

    full_text = ""
    try:
        _temp = SETTINGS.get("temperature")
        async for chunk in stream_ai(messages, model_key, temperature=_temp):
            if chunk.startswith(CLI_STATUS_PREFIX):
                continue
            full_text += chunk
            if poi_tts:
                poi_tts.feed(chunk)
    except Exception as e:
        full_text = f"[周边搜索完成但回复生成失败] {e}"

    if not full_text.strip():
        return

    # 6. 插入系统提示 + AI 回复
    sys_now = time.time()
    sys_msg_id = f"msg_{int(sys_now*1000)}_poi_sys"
    searched_cats = "、".join(c.strip() for c in categories)
    sys_content = f"{ai_name}搜索了{user_name}周边的{searched_cats}信息"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (sys_msg_id, conv_id, "system", sys_content, sys_now, "[]")
        )
        await db.commit()
    sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
               "content": sys_content, "created_at": sys_now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": sys_msg})

    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", full_text, now, "[]")
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    ai_msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant",
              "content": full_text, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": ai_msg})
    if poi_tts:
        try:
            await poi_tts.flush()
        except Exception:
            pass
    await export_conversation(conv_id)
    print(f"[POI_CHECK] 搜索完成，已自动追加回复: {searched_cats}")


# ── [查看动态:n] 查看设备活动摘要 → 自动追加 Core 回复 ─────
async def perform_activity_check(conv_id: str, model_key: str, n: int = 6):
    """Core 在聊天中使用 [查看动态:n]：获取摘要 → 注入 prompt → Core 回应"""
    n = max(1, min(12, n)) if n > 0 else 6

    summary_text = get_activity_summary_for_prompt(n)
    if not summary_text:
        summary_text = "（当前没有设备活动记录）"
    user_dynamics_text = get_user_dynamics_for_prompt(hours=1)
    user_dynamics_block = (
        f"\n\n【用户关键动态】\n{user_dynamics_text}"
        if user_dynamics_text else ""
    )

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    minutes = n * 10

    heart_rate_block = ""
    try:
        from health_context import build_heart_rate_prompt_block
        heart_rate_block = await build_heart_rate_prompt_block(user_name)
    except Exception:
        heart_rate_block = ""

    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt") and wb.get("system_prompt_enabled", True):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})

    import aiosqlite
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content, attachments FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 6",
            (conv_id,)
        )
        rows = await cur.fetchall()
    recent = []
    for r in reversed(rows):
        _c = r["content"]
        try:
            _atts = json.loads(r["attachments"] or "[]") if r["attachments"] else []
        except Exception:
            _atts = []
        for _a in _atts:
            if isinstance(_a, dict) and _a.get("type") == "voice" and _a.get("transcript"):
                _orig = _c.strip() if _c else ""
                _c = f"[语音消息] {_a['transcript']}" + (f"\n{_orig}" if _orig else "")
            elif isinstance(_a, dict) and _a.get("type") == "video_clip" and _a.get("transcript"):
                _orig = _c.strip() if _c else ""
                _c = f"[视频通话] {_a['transcript']}" + (f"\n{_orig}" if _orig else "")
        recent.append({"role": r["role"], "content": _c, "attachments": []})

    activity_prompt = (
        f"你刚才想了解{user_name}最近在干什么，以下是系统采集到的{user_name}过去{minutes}分钟的设备使用动态（每10分钟一条摘要）：\n\n"
        f"【设备活动动态】\n{summary_text}"
        f"{user_dynamics_block}\n\n"
        f"{heart_rate_block}\n\n"
        f"请根据这些动态信息、心率摘要和上下文，自然地和{user_name}聊聊。不需要再说\"让我看看\"之类的话，直接根据动态内容回应即可。"
    )
    messages = prefix + recent + [
        {"role": "user", "content": activity_prompt}
    ]

    # 预生成 msg_id + TTS
    msg_id = f"msg_{int(time.time()*1000)}_ac"
    ac_tts = None
    if manager.any_tts_enabled():
        tts_voice = manager.get_tts_voice()
        if tts_voice:
            ac_tts = TTSStreamer(msg_id, tts_voice, manager)

    full_text = ""
    try:
        _temp = SETTINGS.get("temperature")
        async for chunk in stream_ai(messages, model_key, temperature=_temp):
            if chunk.startswith(CLI_STATUS_PREFIX):
                continue
            full_text += chunk
            if ac_tts:
                ac_tts.feed(chunk)
    except Exception as e:
        full_text = f"[查看动态失败] {e}"

    if not full_text.strip():
        return

    sys_now = time.time()
    sys_msg_id = f"msg_{int(sys_now*1000)}_ac_sys"
    sys_content = f"{ai_name}查看了{user_name}过去{minutes}分钟的动态"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (sys_msg_id, conv_id, "system", sys_content, sys_now, "[]")
        )
        await db.commit()
    sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
               "content": sys_content, "created_at": sys_now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": sys_msg})

    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", full_text, now, "[]")
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    ai_msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant",
              "content": full_text, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": ai_msg})
    if ac_tts:
        try:
            await ac_tts.flush()
        except Exception:
            pass
    await export_conversation(conv_id)
    print(f"[ACTIVITY_CHECK] 查看动态完成，n={n}，已自动追加回复")


# ── 重新生成 AI 回复 ──────────────────────────────
@router.post("/api/conversations/{conv_id}/regenerate")
async def regenerate_message(conv_id: str, context_limit: int = 30, whisper_mode: bool = False, fast_mode: bool = False, temperature: Optional[float] = None, max_tokens: Optional[int] = None, tts_enabled: bool = False, tts_voice: str = ""):
    async with get_db() as db:
        db.row_factory = __import__('aiosqlite').Row
        cur = await db.execute("SELECT model FROM conversations WHERE id=?", (conv_id,))
        conv = await cur.fetchone()
        model_key = resolve_model_key(conv["model"] if conv else DEFAULT_MODEL)

    # ── 合并私聊 + 群聊消息为统一时间线 ──
    merged = await fetch_merged_timeline("aion", context_limit, conv_id=conv_id)
    history = render_merged_timeline(merged, "aion")

    # 只保留最后一条用户消息的图片附件 + 语音消息处理（与 send_message 一致）
    last_user_idx = -1
    for i in range(len(history) - 1, -1, -1):
        if history[i]["role"] == "user":
            last_user_idx = i
            break
    _process_voice_attachments_in_history(history, keep_idx=last_user_idx)

    # 即时哨兵：取最近实际对话用于状态更新 + 关键词提取
    actual_recent = [m for m in history if m["role"] in ("user", "assistant")][-3:]

    wb = load_worldbook()
    ai_name = wb.get("ai_name") or "AI"
    user_name = wb.get("user_name") or "用户"
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt") and wb.get("system_prompt_enabled", True):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})
    if prefix:
        history = prefix + history

    # ── 构建注入块（顺序：prefix → 系统能力 → 当前时间 → 背景记忆 → 相关记忆 → 上下文）──
    cap_idx = len(prefix) if prefix else 0
    inject_offset = 0

    inject_offset = await _insert_private_ability_block(
        history,
        cap_idx,
        inject_offset,
        user_name=user_name,
        model_key=model_key,
        whisper_mode=whisper_mode,
    )

    # 2. 即时哨兵 + 记忆召回（fast_mode 时跳过）
    recall_keywords_str = ""
    recalled = []
    detail_text = ""
    topic = ""
    is_search_needed = False
    recall_query = ""
    debug_top6 = []
    debug_top6_data = []
    debug_recalled = []

    if fast_mode:
        # ── 快速模式：仅注入当前时间 ──
        now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
        bg_block = f"系统当前的准确时间是 {now_str}"
        health_text = await build_health_summary()
        if health_text:
            bg_block += health_text
        history.insert(cap_idx + inject_offset, {"role": "user", "content": bg_block})
        history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到。"})
        inject_offset += 2
    else:
        # ── 正常模式：完整 RAG 流程 ──
        digest_result = await instant_digest(actual_recent)
        recall_keywords = digest_result.get("keywords", [])
        recall_keywords_str = "、".join(recall_keywords) if recall_keywords else ""
        topic = digest_result.get("topic", "")
        is_search_needed = digest_result.get("is_search_needed", False)

        # 3. 注入当前时间（缓存分界点）+ 背景记忆（动态内容）
        surfaced, surfaced_ids = await build_surfacing_memories(topic, recall_keywords)
        now_str = datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
        bg_block = f"系统当前的准确时间是 {now_str}"
        health_text = await build_health_summary()
        if health_text:
            bg_block += health_text
        if surfaced:
            unresolved_lines = [f"📌 {_memory_line_with_evidence(m)[2:]}（还没做/还没去）" for m in surfaced if m.get("unresolved")]
            normal_lines = [_memory_line_with_evidence(m) for m in surfaced if not m.get("unresolved")]
            mem_text = "\n".join(unresolved_lines + normal_lines)
            bg_block += f"\n\n[背景记忆]\n以下是你记得的近期事件和需要关注的事项，在对话中如果有关联可以自然提起：\n{mem_text}"
        history.insert(cap_idx + inject_offset, {"role": "user", "content": bg_block})
        history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会在合适的时候自然提及。"})
        inject_offset += 2

        # 4. RAG 精确召回（与背景记忆去重）
        recall_query = _build_recall_query(
            topic,
            recall_keywords,
            query_text=next((m.get("content", "") for m in reversed(actual_recent) if m.get("role") == "user"), ""),
            recent_messages=actual_recent,
            status=digest_result.get("status", ""),
        )

        if recall_query:
            _, debug_top6 = await recall_memories(recall_query, query_keywords=recall_keywords)
        else:
            debug_top6 = []

        if is_search_needed and recall_query:
            recalled = [r for r in debug_top6 if r["score"] >= 0.45 and r["id"] not in surfaced_ids][:5]
            if digest_result.get("require_detail") and recalled:
                detail_text = await fetch_source_details(recalled, recall_keywords)

        debug_recalled = [{"content": m["content"], "type": m["type"], "score": m["score"],
                           "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                           "importance": m.get("importance")} for m in recalled] if recalled else []
        debug_top6_data = [{"content": m["content"], "score": m["score"],
                            "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                            "importance": m.get("importance")} for m in debug_top6] if debug_top6 else []
        # 5. 注入相关记忆（在背景记忆之后）
        if recalled:
            mem_lines = "\n".join([_memory_line_with_evidence(m) for m in recalled])
            mem_block = f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"
            if detail_text:
                mem_block += f"\n\n[记忆来源原文]\n以下是相关记忆挂载的来源原文；旧记忆没有精确来源时才会按时间范围回退筛选原文：\n{detail_text}"
            history.insert(cap_idx + inject_offset, {"role": "user", "content": mem_block})
            history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    debug_prompt = [{"role": m["role"], "content": m["content"]} for m in history]
    ai_msg_id = f"msg_{int(time.time()*1000)}"
    usage_meta: dict = {}

    # ── 后台任务 + SSE 转发：AI 生成和保存在后台任务中完成，即使客户端断开也不丢失 ──
    _q: asyncio.Queue = asyncio.Queue()

    # 取消事件
    cancel_event = asyncio.Event()
    active_generations[conv_id] = cancel_event

    # 创建 TTS streamer（如果请求方开了 TTS）
    regen_tts = None
    if tts_enabled and tts_voice:
        regen_tts = TTSStreamer(ai_msg_id, tts_voice, manager)
    manager.set_tts_fallback(tts_enabled, tts_voice)

    async def _bg_generate():
        """后台任务：AI 流式生成 → 后处理 → 存 DB → WS 广播。始终运行到结束。"""
        full_text = ""
        has_error = False
        try:
            await _q.put({"id": ai_msg_id, "type": "start"})
            try:
                async for chunk in stream_ai(history, model_key, usage_meta, temperature, max_tokens=max_tokens, cancel_event=cancel_event):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        await _q.put({"type": "cli_status", "text": chunk[len(CLI_STATUS_PREFIX):]})
                        continue
                    full_text += chunk
                    await _q.put(_chat_stream_event(model_key, full_text, chunk))
                    if regen_tts:
                        regen_tts.feed(chunk)
            except Exception as e:
                has_error = True
                error_text = f"\n[请求出错: {str(e)}]"
                full_text += error_text
                await _q.put({"type": "chunk", "content": error_text})

            # 检查 AI 返回的错误文本
            stripped = full_text.strip()
            if not has_error and _is_ai_error_text(stripped):
                has_error = True

            # 检测 [MUSIC:xxx] 指令 → 搜索歌曲并推送卡片数据
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

            # 检测 [TOY:x] 指令
            toy_matches = TOY_CMD_PATTERN.findall(full_text)
            if toy_matches:
                full_text = TOY_CMD_PATTERN.sub("", full_text).strip()

            # 检测 [PET:xxx] 桌宠指令
            pet_matches = PET_CMD_PATTERN.findall(full_text)
            if pet_matches:
                full_text = PET_CMD_PATTERN.sub("", full_text).strip()

            # 检测 [CAM_CHECK] 指令
            cam_triggered = CAM_CHECK_CMD in full_text
            if cam_triggered:
                full_text = full_text.replace(CAM_CHECK_CMD, "").strip()

            # 检测 [查看动态:n] 指令
            activity_match = ACTIVITY_CHECK_PATTERN.search(full_text)
            activity_n = 0
            if activity_match:
                try:
                    activity_n = int(activity_match.group(1))
                except (ValueError, IndexError):
                    activity_n = 6
                activity_n = max(1, min(12, activity_n)) if activity_n > 0 else 6
                full_text = ACTIVITY_CHECK_PATTERN.sub("", full_text).strip()

            # 检测 [POI_SEARCH:xxx] 指令
            poi_matches = POI_SEARCH_PATTERN.findall(full_text)
            if poi_matches:
                full_text = POI_SEARCH_PATTERN.sub("", full_text).strip()

            # 检测 [视频电话] 指令
            video_call_triggered = VIDEO_CALL_CMD in full_text
            if video_call_triggered:
                full_text = full_text.replace(VIDEO_CALL_CMD, "").strip()

            # 检测 [SELFIE:xxx] / [DRAW:xxx] 生图指令
            selfie_match = SELFIE_CMD_PATTERN.search(full_text)
            draw_match = DRAW_CMD_PATTERN.search(full_text)
            image_gen_prompt = None
            image_gen_is_selfie = False
            if selfie_match:
                image_gen_prompt = selfie_match.group(1).strip()
                image_gen_is_selfie = True
                full_text = SELFIE_CMD_PATTERN.sub("", full_text).strip()
            elif draw_match:
                image_gen_prompt = draw_match.group(1).strip()
                full_text = DRAW_CMD_PATTERN.sub("", full_text).strip()

            # 检测日程指令
            full_text, song_gen_prompt = _extract_song_gen_prompt(full_text)
            if song_gen_prompt:
                full_text = clean_song_visible_reply(full_text)
            full_text = await process_schedule_commands(full_text, conv_id, after_msg_id=ai_msg_id)
            full_text = await _process_home_commands(full_text)
            full_text = await _process_mcp_commands(full_text, conv_id=conv_id, ai_msg_id=ai_msg_id, model_key=model_key)
            full_text, luckin_results = await handle_luckin_commands(full_text)

            # 检测 [MOMENT:...] 朋友圈指令
            moment_matches = MOMENT_CMD_PATTERN.findall(full_text)
            if moment_matches:
                full_text = MOMENT_CMD_PATTERN.sub("", full_text).strip()
                for mt_content, mt_reply in moment_matches:
                    mt_content = mt_content.strip()
                    if mt_content:
                        mt_now = time.time()
                        mt_id = f"mt_{int(mt_now*1000)}"
                        expect = 1 if mt_reply == "true" else 0
                        async with get_db() as mt_db:
                            await mt_db.execute(
                                "INSERT INTO moments (id, author, content, source_conv, source_msg_id, expect_reply, created_at) VALUES (?,?,?,?,?,?,?)",
                                (mt_id, "aion", mt_content, conv_id, ai_msg_id, expect, mt_now)
                            )
                            await mt_db.commit()
                        mt_data = {"type": "moment_new", "data": {
                            "id": mt_id, "author": "aion", "content": mt_content,
                            "expect_reply": expect, "created_at": mt_now,
                            "comments": [], "reactions": [],
                        }}
                        await _q.put(mt_data)
                        await manager.broadcast(mt_data)
                        if expect:
                            from routes.moments import _trigger_ai_replies
                            asyncio.create_task(_trigger_ai_replies(mt_id, exclude_author="aion"))

            # 检测 [MEMORY:xxx] 记忆录入指令
            memory_matches = MEMORY_CMD_PATTERN.findall(full_text)
            if memory_matches:
                full_text = MEMORY_CMD_PATTERN.sub("", full_text).strip()
                for mem_content in memory_matches:
                    mem_content = mem_content.strip()
                    if mem_content:
                        mem_now = time.time()
                        mem_id = f"mem_{int(mem_now*1000)}"
                        vec = await get_embedding(mem_content)
                        async with get_db() as mem_db:
                            await mem_db.execute(
                                "INSERT INTO memories (id, content, type, created_at, source_conv, embedding, keywords, importance, source_start_ts, source_end_ts, unresolved) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (mem_id, mem_content, "重要事件", mem_now, conv_id,
                                 _pack_embedding(vec) if vec else None, '', 0.5, None, None, 0)
                            )
                            await mem_db.commit()
                        mem_data = {"id": mem_id, "content": mem_content, "type": "重要事件",
                                    "created_at": mem_now, "keywords": "", "importance": 0.5,
                                    "source_start_ts": None, "source_end_ts": None}
                        await manager.broadcast({"type": "memory_added", "data": mem_data})
                        mr_data = {'type': 'memory_record', 'msg_id': ai_msg_id, 'content': mem_content, 'mem_id': mem_id}
                        await _q.put(mr_data)
                        await manager.broadcast({"type": "memory_record", "data": mr_data})
                        print(f"[MEMORY] AI 主动录入记忆: {mem_content[:50]}")

            full_text = await _process_wish_commands(
                full_text,
                author="aion",
                source_type="chat_command",
                source_ref=f"{conv_id}:{ai_msg_id}",
            )

            # 检测 [转账：N元] 指令 — AI 转账入账
            transfer_matches = TRANSFER_CMD_PATTERN.findall(full_text)
            for t_amount_str in transfer_matches:
                try:
                    t_val = float(t_amount_str)
                    if t_val > 0:
                        _a_n = wb.get('ai_name', 'AI')
                        _u_n = wb.get('user_name', '用户')
                        async with get_db() as t_db:
                            t_now = time.time()
                            t_id = f"wt_{int(t_now*1000)}"
                            await t_db.execute(
                                "INSERT INTO bookkeeping (id, record_type, amount, description, created_at) VALUES (?,?,?,?,?)",
                                (t_id, 'wallet_ai', -t_val, f'{_a_n}转账给{_u_n} {t_val}元', t_now)
                            )
                            await t_db.commit()
                        await manager.broadcast({"type": "wallet_update"})
                        print(f"[WALLET] AI 转账: -{t_val}元")
                except (ValueError, Exception):
                    pass

            # 清洗 AI 回复中模仿产生的 <meta> 标签
            full_text = _visible_ai_text(full_text)

            # 将音乐点歌信息存入 attachments，刷新后可显示胶囊
            music_atts = [{"type": "music", "name": s["name"], "artist": s["artist"], "id": s["id"]} for s in music_cards] if music_cards else []
            full_text, image_atts = _extract_reply_image_attachments(full_text)
            reply_atts = _dedupe_attachments(music_atts + luckin_payment_attachments(luckin_results) + image_atts)
            att_json = json.dumps(reply_atts, ensure_ascii=False) if reply_atts else ""

            now2 = time.time()
            async with get_db() as db2:
                await db2.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments, reasoning_content) VALUES (?,?,?,?,?,?,?)",
                    (ai_msg_id, conv_id, "assistant", full_text, now2, att_json, usage_meta.get("reasoning_content", "").strip())
                )
                await db2.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                await db2.commit()

            ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant", "content": full_text, "created_at": now2, "attachments": reply_atts, "reasoning_content": usage_meta.get("reasoning_content", "").strip()}
            await manager.broadcast({"type": "msg_created", "data": ai_msg})
            await export_conversation(conv_id)

            # 推送 [TOY:x] 指令到前端
            if toy_matches:
                toy_data = {'type': 'toy_command', 'commands': toy_matches, 'msg_id': ai_msg_id}
                await _q.put(toy_data)
                await manager.broadcast({"type": "toy_command", "data": toy_data})
                await _toy_sys_msg(conv_id, toy_matches)

            # 推送 [PET:xxx] 桌宠指令到前端
            if pet_matches and _is_pet_available():
                await manager.broadcast({"type": "pet_command", "data": {"action": pet_matches[-1].lower()}})

            # [CAM_CHECK] 服务端直接触发，前端只显示 UI 指示器
            if cam_triggered:
                cam_data = {'type': 'cam_check', 'conv_id': conv_id, 'model_key': model_key, 'msg_id': ai_msg_id}
                await _q.put(cam_data)
                await manager.broadcast({"type": "cam_check", "data": cam_data})
                asyncio.create_task(_delayed_cam_check(conv_id, model_key))

            # [POI_SEARCH] 搜索周边 → 携带结果自动追加一轮 Core 回复
            if poi_matches:
                poi_data = {'type': 'poi_search', 'conv_id': conv_id, 'categories': poi_matches, 'msg_id': ai_msg_id}
                await _q.put(poi_data)
                await manager.broadcast({"type": "poi_search", "data": poi_data})
                asyncio.create_task(perform_poi_check(conv_id, model_key, poi_matches))

            # [查看动态:n] 查看设备活动摘要 → 携带摘要自动追加一轮 Core 回复
            if activity_n > 0:
                activity_data = {'type': 'activity_check', 'conv_id': conv_id, 'n': activity_n, 'msg_id': ai_msg_id}
                await _q.put(activity_data)
                await manager.broadcast({"type": "activity_check", "data": activity_data})
                asyncio.create_task(perform_activity_check(conv_id, model_key, activity_n))

            # [视频电话] 延迟 10 秒后定向推送到最后发消息的客户端
            if video_call_triggered:
                vc_data = {'type': 'video_call_incoming', 'conv_id': conv_id, 'msg_id': ai_msg_id}
                await _q.put(vc_data)
                await _video_call_incoming_sys_msg(conv_id)
                asyncio.create_task(_delayed_video_call(vc_data))

            # 推送音乐卡片
            if music_cards:
                music_data = {'type': 'music', 'msg_id': ai_msg_id, 'cards': music_cards}
                await _q.put(music_data)
                await manager.broadcast({"type": "music", "data": music_data})
                await _music_sys_msg(conv_id, music_cards)

            if image_gen_prompt:
                ig_data = {'type': 'image_gen_start', 'conv_id': conv_id, 'msg_id': ai_msg_id, 'is_selfie': image_gen_is_selfie}
                await _q.put(ig_data)
                await manager.broadcast({"type": "image_gen_start", "data": ig_data})
                asyncio.create_task(_do_image_gen(conv_id, ai_msg_id, image_gen_prompt, image_gen_is_selfie))

            if song_gen_prompt:
                sg_data = {'type': 'song_gen_start', 'conv_id': conv_id, 'msg_id': ai_msg_id}
                await _q.put(sg_data)
                await manager.broadcast({"type": "song_gen_start", "data": sg_data})
                asyncio.create_task(_do_song_gen(conv_id, ai_msg_id, song_gen_prompt))

            debug_data = {
                "type": "debug",
                "model": model_key,
                "msg_id": ai_msg_id,
                "recall_keywords": recall_keywords_str,
                "recall_query": recall_query,
                "recall_topic": topic,
                "is_search_needed": is_search_needed,
                "recalled_memories": debug_recalled,
                "debug_top6": debug_top6_data,
                "prompt_messages": debug_prompt,
                "prompt_count": len(history),
                "usage": usage_meta if usage_meta else None,
                "has_error": has_error,
                "error_text": stripped if has_error else None,
            }
            await _q.put(debug_data)
            await manager.broadcast({"type": "debug", "data": debug_data})
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            active_generations.pop(conv_id, None)
            if regen_tts:
                try:
                    await regen_tts.flush()
                except Exception:
                    pass
            await _q.put({"type": "done"})

    asyncio.create_task(_bg_generate())

    async def generate():
        """SSE 转发：从队列读取事件转发给客户端。客户端断开时生成器关闭，后台任务不受影响。"""
        while True:
            data = await _q.get()
            if data.get("type") == "done":
                break
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
