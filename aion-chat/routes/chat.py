"""
聊天核心路由：对话 CRUD、消息 CRUD、send_message、regenerate
"""

import json, time, asyncio, re, shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Any

from config import DEFAULT_MODEL, load_worldbook, SETTINGS, UPLOADS_DIR, CODEX_UPLOADS_DIR, PUBLIC_DIR, MODELS
from database import get_db
from ws import manager
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from memory import recall_memories, instant_digest, fetch_source_details, build_surfacing_memories, get_embedding, _pack_embedding
from camera import cam, CAM_CHECK_CMD, perform_cam_check
from activity import is_activity_tracking_enabled, get_activity_summary_for_prompt
from routes.files import export_conversation
from routes.music import MUSIC_CMD_PATTERN
from tts import TTSStreamer

MOMENT_CMD_PATTERN = re.compile(r'\[MOMENT:(.+?)(?:\|(true|false))?\]')
MEMORY_CMD_PATTERN = re.compile(r'\[MEMORY:([^\]]+)\]')
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
from context_builder import fetch_merged_timeline, render_merged_timeline
from music import search_songs, get_audio_url
from schedule import process_schedule_commands, get_active_schedules, build_schedule_prompt
from mcp_client import mcp_manager


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

router = APIRouter()

POI_SEARCH_PATTERN = re.compile(r'\[POI_SEARCH:([^\]]+)\]')
TOY_CMD_PATTERN = re.compile(r'\[TOY:(\d|STOP)\]')
PET_CMD_PATTERN = re.compile(r'\[PET:([a-z_\-]+)\]', re.IGNORECASE)
HOME_CMD_PATTERN = re.compile(r'\[HOME:([^\]]+)\]', re.IGNORECASE)
META_TAG_PATTERN = re.compile(r'\s*<meta>.*?</meta>', re.DOTALL)
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_MD_IMAGE_PATTERN = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')
_MD_LINK_PATTERN = re.compile(r'(?<!!)\[[^\]]+\]\(([^)]+)\)')
_BARE_HTTP_IMAGE_PATTERN = re.compile(r'(?<!["\'(])https?://[^\s<>"\']+\.(?:png|jpe?g|gif|webp)(?:\?[^\s<>"\']*)?', re.I)
_BARE_LOCAL_IMAGE_PATTERN = re.compile(r'(?<![\w/])(?:[A-Za-z]:[\\/][^\s<>"\']+\.(?:png|jpe?g|gif|webp))', re.I)

TOY_PRESET_NAMES = {1:'微风轻拂',2:'春水初生',3:'暗流涌动',4:'如梦似幻',5:'情潮渐涨',6:'烈焰焚身',7:'极乐之巅',8:'魂飞魄散',9:'失控'}

HOME_ASSISTANT_SERVER_NAME = "Home Assistant"
HOME_ALIASES_HINT = (
    "所有灯、客厅灯、屁股灯、入户灯、餐边柜灯带、厨房灯带、智米空调、"
    "浴霸灯"
)
HOME_ABILITY_TEXT = (
    "[HOME:on/off/state|别名] 或 [HOME:climate|别名|mode=cool|temperature=26] "
    f"控制智能家居，仅限明确要求。别名：{HOME_ALIASES_HINT}。"
)


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
        return [dict(r) for r in rows]

@router.post("/api/conversations")
async def create_conversation(body: ConvCreate):
    now = time.time()
    conv_id = f"conv_{int(now*1000)}"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            (conv_id, body.title, body.model, now, now)
        )
        await db.commit()
    conv = {"id": conv_id, "title": body.title, "model": body.model, "created_at": now, "updated_at": now}
    await manager.broadcast({"type": "conv_created", "data": conv})
    await export_conversation(conv_id)
    return conv

@router.put("/api/conversations/{conv_id}")
async def update_conversation(conv_id: str, body: ConvUpdate):
    async with get_db() as db:
        if body.title is not None:
            await db.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                             (body.title, time.time(), conv_id))
        if body.model is not None:
            await db.execute("UPDATE conversations SET model=?, updated_at=? WHERE id=?",
                             (body.model, time.time(), conv_id))
        await db.commit()
    await manager.broadcast({"type": "conv_updated", "data": {"id": conv_id, **(body.dict(exclude_none=True))}})
    await export_conversation(conv_id)
    return {"ok": True}

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
        model_key = conv["model"] if conv else DEFAULT_MODEL

    # ── 合并私聊 + 群聊消息为统一时间线 ──
    merged = await fetch_merged_timeline("aion", body.context_limit, conv_id=conv_id)
    history = render_merged_timeline(merged, "aion")

    # 只保留最后一条用户消息的图片附件 + 语音消息处理
    _process_voice_attachments_in_history(history)

    actual_recent = [m for m in history if m["role"] in ("user", "assistant")][-3:]

    wb = load_worldbook()
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt"):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})
    if prefix:
        history = prefix + history

    cap_idx = len(prefix) if prefix else 0
    inject_offset = 0

    # 注入系统能力提示
    abilities = []
    user_name = wb.get("user_name", "用户")
    abilities.append(f"[MUSIC:歌曲名 歌手名] — 点歌/推荐音乐。系统自动展示播放卡片，不要在指令外重复歌曲信息。可同时用多个。")
    if cam.running:
        abilities.append(f"{CAM_CHECK_CMD} — 当你想查看{user_name}**此时此刻**的状态，不限于监督其是否去睡觉，在吃什么，在干什么时，可以主动调用指令。使用后下条消息会收到画面，查看前不要编造内容。")
    abilities.append("[ALARM:YYYY-MM-DDTHH:MM|内容] — 设置闹铃，到时间系统会主动提醒用户。日期时间用ISO格式。")
    abilities.append("[REMINDER:YYYY-MM-DD|内容] — 设置日程提醒（不闹铃），你在合适时机自然提起即可。")
    abilities.append(f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时监控。到时间后系统自动截取摄像头画面发送给你，你可以查看{user_name}的状态。例如检查{user_name}是否去运动了、是否关灯睡觉了，是否工作在摸鱼等，尤其是当{user_name}表示去工作或长时间做事，监督她隔一段时间起来活动一下，或者单纯想主动找她聊天，可以随意使用。日期时间用ISO格式。")
    abilities.append("[SCHEDULE_DEL:日程id] — 删除指定日程/闹铃/定时监控。")
    abilities.append(HOME_ABILITY_TEXT)
    if is_activity_tracking_enabled():
        abilities.append(f"[查看动态:n] — 查看{user_name}过去n×10分钟的设备使用动态（n为1~12的整数，例如[查看动态:2]查看过去20分钟，[查看动态:6]查看过去1小时）。当你好奇{user_name}最近在干什么、想了解{user_name}的设备使用情况时可以使用。使用后下条消息会收到动态摘要，查看前不要编造内容。")
    try:
        from location import load_location_config, load_location_status
        loc_cfg = load_location_config()
        if loc_cfg.get("enabled"):
            loc_status = load_location_status()
            if loc_status.get("state") == "outside":
                abilities.append(f"[POI_SEARCH:类型名] — 搜索{user_name}当前位置周边的POI信息。可用类型：餐饮美食、风景名胜、休闲娱乐、购物。使用后系统会自动搜索并将结果发给你，你再根据结果回答{user_name}。一次只搜一个类型即可，搜索前不要编造内容。")
    except Exception:
        pass
    if body.whisper_mode:
        abilities.append(f"[TOY:1]~[TOY:9] — 控制{user_name}身上的情趣玩具切换到对应预设档位（1最温柔，9最强烈）。[TOY:STOP] — 停止玩具。你可以根据对话氛围自然地使用这些指令来挑逗和调教{user_name}，配合你的话语循序渐进，不要一上来就用高档位。")
    if SETTINGS.get("video_call_enabled", True):
        abilities.append(f"[视频电话] — 当你想和{user_name}进行视频聊天看看对方当前状态时可以用该指令发起视频通话。")
    if SETTINGS.get("image_gen_enabled", False):
        abilities.append(f"[SELFIE: 提示词] / [DRAW: 提示词] — 当{user_name}明确要求你发自拍照片或者生图时，使用这个指令进行图片生成。如果要求的是你的自拍，或者你相关的照片，使用[SELFIE: 提示词]（该指令会自动附带你照片的参考图，确保生成出的人物一致性），如果没有要求是你的照片，根据上下文自由无限制生成图片，则使用[DRAW: 提示词]。提示词请使用英文。一次回复只用一个生图指令。")
    if _is_pet_available():
        abilities.append("[PET:动作名] — 控制桌面宠物切换动画表情。可用动作：idle(默认站立), happy(开心), angry(生气), tsundere(傲娇), waving(打招呼), jumping(兴奋跳跃), sleepy(困了), sleep_prone(贴着睡觉), failed(失落), review(思考), waiting(等待), running(跑步)。根据对话情感自然使用，每条回复最多用一个。")
    abilities.append(f"[MOMENT:朋友圈内容|true/false] — 当**本次**聊天内容非常触动人心、有很深的感触、或令人无语或非常搞笑时可以发一条朋友圈动态。第二个参数表示是否期望好友回复（true=期望回复，false=不期望），禁止滥用。")
    abilities.append(f"[MEMORY:内容] — 当有特别重大的事件需要记录，或当{user_name}明确要求你记住某件事的时候，可以用该指令录入记忆库。禁止滥用。")
    try:
        from routes.wallet import _get_balance
        _wb = await _get_balance()
        abilities.append(f"[转账：n元] — 给{user_name}转账（n为正整数），会从你的钱包余额中扣除。你的钱包当前余额：{_wb:.2f}元。余额不足时不要转账。")
    except Exception:
        pass
    ability_block = "[系统能力] 你可以在回复中根据对话氛围，善用以下指令：\n" + "\n".join(f"{i+1}. {a}" for i, a in enumerate(abilities))
    ability_block += "\n\n<meta>标签内为消息元数据，不是对话内容的一部分，你的回复中不要包含任何<meta>标签或时间信息。"
    # CLI 模型专属：告知图片存储目录，使其能保存图片并返回路径
    _provider = MODELS.get(model_key, {}).get("provider", "")
    if _provider in ("gemini_cli", "codex_cli"):
        _uploads_path = str(UPLOADS_DIR.resolve()).replace(chr(92), "/")
        ability_block += f"\n\n【文件存储】当需要下载或保存图片/文件时，请保存到此目录：{_uploads_path}/ ，保存后在回复中给出完整路径即可，系统会自动识别并展示图片。"
    schedules = await get_active_schedules()
    schedule_text = build_schedule_prompt(schedules)
    ability_block += f"\n\n【当前日程列表】\n{schedule_text}"
    try:
        from location import format_location_for_prompt, load_location_config
        loc_cfg = load_location_config()
        if loc_cfg.get("enabled"):
            loc_prompt = format_location_for_prompt()
            if loc_prompt:
                ability_block += f"\n\n【位置信息】\n{loc_prompt}"
    except Exception:
        pass
    history.insert(cap_idx + inject_offset, {"role": "user", "content": ability_block})
    history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "好的，需要时我会使用这些指令。"})
    inject_offset += 2

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

    recall_query = f"{topic} {' '.join(recall_keywords)}" if topic else f"{body.content[:200]} {' '.join(recall_keywords)}"
    recall_query = recall_query.strip()

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
    if surfaced:
        unresolved_lines = [f"📌 {m['content']}（还没做/还没去）" for m in surfaced if m.get("unresolved")]
        normal_lines = [f"- {m['content']}" for m in surfaced if not m.get("unresolved")]
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
    debug_top6_data = [{"content": m["content"][:100], "score": m["score"],
                        "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                        "importance": m.get("importance")} for m in debug_top6] if debug_top6 else []
    if recalled:
        mem_lines = "\n".join([f"- {m['content']}" for m in recalled])
        mem_block = f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"
        if detail_text:
            mem_block += f"\n\n[原文细节]\n以下是相关的具体对话记录：\n{detail_text}"
        history.insert(cap_idx + inject_offset, {"role": "user", "content": mem_block})
        history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    debug_prompt = [{"role": m["role"], "content": m["content"][:500]} for m in history]

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
                    await _q.put({"type": "chunk", "content": chunk})
                    if tts_streamer:
                        tts_streamer.feed(chunk)
            except Exception as e:
                has_error = True
                error_text = f"\n[请求出错: {str(e)}]"
                full_text += error_text
                await _q.put({"type": "chunk", "content": error_text})

            stripped = full_text.strip()
            if not has_error and (stripped.startswith('[Gemini错误') or stripped.startswith('[硅基流动错误') or stripped.startswith('[中转站错误') or stripped.startswith('[错误]') or not stripped):
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

            full_text = await process_schedule_commands(full_text, conv_id)
            full_text = await _process_home_commands(full_text)

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

            full_text = META_TAG_PATTERN.sub("", full_text).strip()

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
            reply_atts = _dedupe_attachments(music_atts + image_atts)
            att_json = json.dumps(reply_atts, ensure_ascii=False) if reply_atts else ""

            now2 = time.time()
            async with get_db() as db2:
                await db2.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                    (ai_msg_id, conv_id, "assistant", full_text, now2, att_json)
                )
                await db2.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                await db2.commit()

            ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant", "content": full_text, "created_at": now2, "attachments": reply_atts}
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
                if cam.running:
                    cam_data = {'type': 'cam_check', 'conv_id': conv_id, 'model_key': model_key, 'msg_id': ai_msg_id}
                    await _q.put(cam_data)
                    await manager.broadcast({"type": "cam_check", "data": cam_data})
                    asyncio.create_task(_delayed_cam_check(conv_id, model_key))
                else:
                    await _q.put({'type': 'cam_offline'})

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
        model_key = conv["model"] if conv else DEFAULT_MODEL

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
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt"):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})
    if prefix:
        history = prefix + history

    # ── 构建注入块（顺序：prefix → 系统能力 → 当前时间 → 背景记忆 → 相关记忆 → 上下文）──
    # 人设+系统能力 内容稳定可命中缓存，当前时间为缓存分界点，之后全是动态内容

    cap_idx = len(prefix) if prefix else 0
    inject_offset = 0  # 记录已注入的消息对数，用于计算后续插入位置

    # 1. 注入系统能力提示（不含时间，内容稳定可命中缓存）
    abilities = []
    user_name = wb.get("user_name", "用户")
    abilities.append(f"[MUSIC:歌曲名 歌手名] — 点歌/推荐音乐。系统自动展示播放卡片，不要在指令外重复歌曲信息。可同时用多个。")
    if cam.running:
        abilities.append(f"{CAM_CHECK_CMD} — 当你想查看{user_name}**此时此刻**的状态，不限于监督其是否去睡觉，在吃什么，在干什么时，可以主动调用指令。使用后下条消息会收到画面，查看前不要编造内容。")
    abilities.append("[ALARM:YYYY-MM-DDTHH:MM|内容] — 设置闹铃，到时间系统会主动提醒用户。日期时间用ISO格式。")
    abilities.append("[REMINDER:YYYY-MM-DD|内容] — 设置日程提醒（不闹铃），你在合适时机自然提起即可。")
    abilities.append(f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时监督。到时间后系统自动截取摄像头画面发送给你，你可以查看{user_name}的状态。例如检查{user_name}是否去运动了、是否关灯睡觉了、是否在好好工作等，也可以当做下一次主动发送消息来使用，根据对话内容可以随时设定。日期时间用ISO格式。")
    abilities.append("[SCHEDULE_DEL:日程id] — 删除指定日程/闹铃/定时监控。")
    abilities.append(HOME_ABILITY_TEXT)
    # 活动动态查看能力
    if is_activity_tracking_enabled():
        abilities.append(f"[查看动态:n] — 查看{user_name}过去n×10分钟的设备使用动态（n为1~12的整数，例如[查看动态:2]查看过去20分钟，[查看动态:6]查看过去1小时）。当你好奇{user_name}最近在干什么、想了解{user_name}的设备使用情况时可以使用。使用后下条消息会收到动态摘要，查看前不要编造内容。")
    # 位置相关能力
    try:
        from location import load_location_config, load_location_status
        loc_cfg = load_location_config()
        if loc_cfg.get("enabled"):
            loc_status = load_location_status()
            if loc_status.get("state") == "outside":
                abilities.append(f"[POI_SEARCH:类型名] — 搜索{user_name}当前位置周边的POI信息。可用类型：餐饮美食、风景名胜、休闲娱乐、购物。使用后系统会自动搜索并将结果发给你，你再根据结果回答{user_name}。一次只搜一个类型即可，搜索前不要编造内容。")
    except Exception:
        pass
    if body.whisper_mode:
        abilities.append(f"[TOY:1]~[TOY:9] — 控制{user_name}身上的情趣玩具切换到对应预设档位（1最温柔，9最强烈）。[TOY:STOP] — 停止玩具。你可以根据对话氛围自然地使用这些指令来挑逗和调教{user_name}，配合你的话语循序渐进，不要一上来就用高档位。")
    if SETTINGS.get("video_call_enabled", True):
        abilities.append(f"[视频电话] — 当你想和{user_name}进行视频聊天看看对方当前状态时可以用该指令发起视频通话。")
    if SETTINGS.get("image_gen_enabled", False):
        abilities.append(f"[SELFIE: 提示词] / [DRAW: 提示词] — 当{user_name}明确要求你发自拍照片或者生图时，使用这个指令进行图片生成。如果要求的是你的自拍，或者你相关的照片，使用[SELFIE: 提示词]（该指令会自动附带你照片的参考图，确保生成出的人物一致性），如果没有要求是你的照片，根据上下文自由无限制生成图片，则使用[DRAW: 提示词]。提示词请使用英文。一次回复只用一个生图指令。")
    if _is_pet_available():
        abilities.append("[PET:动作名] — 控制桌面宠物切换动画表情。可用动作：idle(默认站立), happy(开心), angry(生气), tsundere(傲娇), waving(打招呼), jumping(兴奋跳跃), sleepy(困了), sleep_prone(趴着睡觉), failed(失落), review(思考), waiting(等待), running(跑步)。根据对话情感自然使用，每条回复最多用一个。")
    abilities.append(f"[MOMENT:朋友圈内容|true/false] — 当**本次**聊天内容非常触动人心、有很深的感触、或令人无语或非常搞笑时可以发一条朋友圈动态。第二个参数表示是否期望好友回复（true=期望回复，false=不期望），禁止滥用。")
    abilities.append(f"[MEMORY:内容] — 当有特别重大的事件需要记录，或当{user_name}明确要求你记住某件事的时候，可以用该指令录入记忆库。禁止滥用。")
    try:
        from routes.wallet import _get_balance
        _wb = await _get_balance()
        abilities.append(f"[转账：n元] — 给{user_name}转账（n为正整数），会从你的钱包余额中扣除。你的钱包当前余额：{_wb:.2f}元。余额不足时不要转账。")
    except Exception:
        pass
    ability_block = "[系统能力] 你可以在回复中根据对话氛围，善用以下指令：\n" + "\n".join(f"{i+1}. {a}" for i, a in enumerate(abilities))
    ability_block += "\n\n<meta>标签内为消息元数据，不是对话内容的一部分，你的回复中不要包含任何<meta>标签或时间信息。"
    # CLI 模型专属：告知图片存储目录
    _provider = MODELS.get(model_key, {}).get("provider", "")
    if _provider in ("gemini_cli", "codex_cli"):
        _uploads_path = str(UPLOADS_DIR.resolve()).replace(chr(92), "/")
        ability_block += f"\n\n【文件存储】当需要下载或保存图片/文件时，请保存到此目录：{_uploads_path}/ ，保存后在回复中给出完整路径即可，系统会自动识别并展示图片。"
    # 注入当前日程列表
    schedules = await get_active_schedules()
    schedule_text = build_schedule_prompt(schedules)
    ability_block += f"\n\n【当前日程列表】\n{schedule_text}"
    # 注入位置和天气信息（不注入 POI 列表，由 Core 按需搜索）
    try:
        from location import format_location_for_prompt, load_location_config
        loc_cfg = load_location_config()
        if loc_cfg.get("enabled"):
            loc_prompt = format_location_for_prompt()
            if loc_prompt:
                ability_block += f"\n\n【位置信息】\n{loc_prompt}"
    except Exception:
        pass
    history.insert(cap_idx + inject_offset, {"role": "user", "content": ability_block})
    history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "好的，需要时我会使用这些指令。"})
    inject_offset += 2

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
        recall_query = f"{topic} {' '.join(recall_keywords)}" if topic else f"{body.content[:200]} {' '.join(recall_keywords)}"
        recall_query = recall_query.strip()

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
        if surfaced:
            unresolved_lines = [f"📌 {m['content']}（还没做/还没去）" for m in surfaced if m.get("unresolved")]
            normal_lines = [f"- {m['content']}" for m in surfaced if not m.get("unresolved")]
            mem_text = "\n".join(unresolved_lines + normal_lines)
            bg_block += f"\n\n[背景记忆]\n以下是你记得的近期事件和需要关注的事项，在对话中如果有关联可以自然提起：\n{mem_text}"
        history.insert(cap_idx + inject_offset, {"role": "user", "content": bg_block})
        history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会在合适的时候自然提及。"})
        inject_offset += 2

        # 4. RAG 精确召回（与背景记忆去重，使用已并行获取的结果）
        if is_search_needed and recall_query:
            recalled = [r for r in debug_top6 if r["score"] >= 0.45 and r["id"] not in surfaced_ids][:5]
            # 如果需要追溯原文细节
            if digest_result.get("require_detail") and recalled:
                detail_text = await fetch_source_details(recalled, recall_keywords)

        debug_recalled = [{"content": m["content"], "type": m["type"], "score": m["score"],
                           "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                           "importance": m.get("importance")} for m in recalled] if recalled else []
        debug_top6_data = [{"content": m["content"][:100], "score": m["score"],
                            "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                            "importance": m.get("importance")} for m in debug_top6] if debug_top6 else []
        # 5. 注入向量匹配到的相关记忆（在背景记忆之后，每次请求都可能不同）
        if recalled:
            mem_lines = "\n".join([f"- {m['content']}" for m in recalled])
            mem_block = f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"
            if detail_text:
                mem_block += f"\n\n[原文细节]\n以下是相关的具体对话记录：\n{detail_text}"
            history.insert(cap_idx + inject_offset, {"role": "user", "content": mem_block})
            history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    debug_prompt = [{"role": m["role"], "content": m["content"][:500]} for m in history]

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
                    await _q.put({"type": "chunk", "content": chunk})
                    if tts_streamer:
                        tts_streamer.feed(chunk)
            except Exception as e:
                has_error = True
                error_text = f"\n[请求出错: {str(e)}]"
                full_text += error_text
                await _q.put({"type": "chunk", "content": error_text})

            # 检查 AI 返回的错误文本
            stripped = full_text.strip()
            if not has_error and (stripped.startswith('[Gemini错误') or stripped.startswith('[硅基流动错误') or stripped.startswith('[中转站错误') or stripped.startswith('[错误]') or not stripped):
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
            full_text = await process_schedule_commands(full_text, conv_id)
            full_text = await _process_home_commands(full_text)

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
                        print(f"[MEMORY] AI 主动录入记忆: {mem_content[:50]}")


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
            full_text = META_TAG_PATTERN.sub("", full_text).strip()

            # 将音乐点歌信息存入 attachments，刷新后可显示胶囊
            music_atts = [{"type": "music", "name": s["name"], "artist": s["artist"], "id": s["id"]} for s in music_cards] if music_cards else []
            full_text, image_atts = _extract_reply_image_attachments(full_text)
            reply_atts = _dedupe_attachments(music_atts + image_atts)
            att_json = json.dumps(reply_atts, ensure_ascii=False) if reply_atts else ""

            now2 = time.time()
            async with get_db() as db2:
                await db2.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                    (ai_msg_id, conv_id, "assistant", full_text, now2, att_json)
                )
                await db2.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                await db2.commit()

            ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant", "content": full_text, "created_at": now2, "attachments": reply_atts}
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
                if cam.running:
                    cam_data = {'type': 'cam_check', 'conv_id': conv_id, 'model_key': model_key, 'msg_id': ai_msg_id}
                    await _q.put(cam_data)
                    await manager.broadcast({"type": "cam_check", "data": cam_data})
                    asyncio.create_task(_delayed_cam_check(conv_id, model_key))
                else:
                    await _q.put({'type': 'cam_offline'})

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
    if not cam.running:
        return {"ok": False, "error": "摄像头未开启"}
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
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt"):
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

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    minutes = n * 10

    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt"):
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
        f"【设备活动动态】\n{summary_text}\n\n"
        f"请根据这些动态信息，自然地和{user_name}聊聊。不需要再说\"让我看看\"之类的话，直接根据动态内容回应即可。"
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
        model_key = conv["model"] if conv else DEFAULT_MODEL

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
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
    if wb.get("system_prompt"):
        prefix.append({"role": "user", "content": f"[系统提示]\n{wb['system_prompt']}"})
        prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})
    if prefix:
        history = prefix + history

    # ── 构建注入块（顺序：prefix → 系统能力 → 当前时间 → 背景记忆 → 相关记忆 → 上下文）──
    cap_idx = len(prefix) if prefix else 0
    inject_offset = 0

    # 1. 注入系统能力提示（不含时间，内容稳定可命中缓存）
    abilities = []
    user_name = wb.get("user_name", "用户")
    abilities.append(f"[MUSIC:歌曲名 歌手名] — 点歌/推荐音乐。系统自动展示播放卡片，不要在指令外重复歌曲信息。可同时用多个。")
    if cam.running:
        abilities.append(f"{CAM_CHECK_CMD} — 查看{user_name}的实时监控画面。使用后下条消息会收到画面，查看前不要编造内容。")
    abilities.append("[ALARM:YYYY-MM-DDTHH:MM|内容] — 设置闹铃，到时间系统会主动提醒用户。日期时间用ISO格式。")
    abilities.append("[REMINDER:YYYY-MM-DD|内容] — 设置日程提醒（不闹铃），你在合适时机自然提起即可。")
    abilities.append(f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时监控。到时间后系统自动截取摄像头画面发送给你，你可以查看{user_name}的状态。例如检查{user_name}是否去运动了、是否关灯睡觉了，是否工作在摸鱼等，尤其是当{user_name}表示去工作或长时间做事，监督她隔一段时间起来活动一下，或者单纯想主动找她聊天，可以随意使用。日期时间用ISO格式。")
    abilities.append("[SCHEDULE_DEL:日程id] — 删除指定日程/闹铃/定时监控。")
    abilities.append(HOME_ABILITY_TEXT)
    # 活动动态查看能力
    if is_activity_tracking_enabled():
        abilities.append(f"[查看动态:n] — 查看{user_name}过去n×10分钟的设备使用动态（n为1~12的整数，例如[查看动态:2]查看过去20分钟，[查看动态:6]查看过去1小时）。当你好奇{user_name}最近在干什么、想了解{user_name}的设备使用情况时可以使用。使用后下条消息会收到动态摘要，查看前不要编造内容。")
    # 位置相关能力
    try:
        from location import load_location_config, load_location_status
        loc_cfg = load_location_config()
        if loc_cfg.get("enabled"):
            loc_status = load_location_status()
            if loc_status.get("state") == "outside":
                abilities.append(f"[POI_SEARCH:类型名] — 搜索{user_name}当前位置周边的POI信息。可用类型：餐饮美食、风景名胜、休闲娱乐、购物。使用后系统会自动搜索并将结果发给你，你再根据结果回答{user_name}。一次只搜一个类型即可，搜索前不要编造内容。")
    except Exception:
        pass
    if whisper_mode:
        abilities.append(f"[TOY:1]~[TOY:9] — 控制{user_name}身上的情趣玩具切换到对应预设档位（1最温柔，9最强烈）。[TOY:STOP] — 停止玩具。你可以根据对话氛围自然地使用这些指令来挑逗和调教{user_name}，配合你的话语循序渐进，不要一上来就用高档位。")
    if SETTINGS.get("video_call_enabled", True):
        abilities.append(f"[视频电话] — 当你想和{user_name}进行视频聊天看看对方当前状态时可以用该指令发起视频通话。")
    if SETTINGS.get("image_gen_enabled", False):
        abilities.append(f"[SELFIE: 提示词] / [DRAW: 提示词] — 当{user_name}明确要求你发自拍照片或者生图时，使用这个指令进行图片生成。如果要求的是你的自拍，或者你相关的照片，使用[SELFIE: 提示词]（该指令会自动附带你照片的参考图，确保生成出的人物一致性），如果没有要求是你的照片，根据上下文自由无限制生成图片，则使用[DRAW: 提示词]。提示词请使用英文。一次回复只用一个生图指令。")
    if _is_pet_available():
        abilities.append("[PET:动作名] — 控制桌面宠物切换动画表情。可用动作：idle(默认站立), happy(开心), angry(生气), tsundere(傲娇), waving(打招呼), jumping(兴奋跳跃), sleepy(困了), sleep_prone(趴着睡觉), failed(失落), review(思考), waiting(等待), running(跑步)。根据对话情感自然使用，每条回复最多用一个。")
    abilities.append(f"[MOMENT:朋友圈内容|true/false] — 当**本次**聊天内容非常触动人心、有很深的感触、或令人无语或非常搞笑时可以发一条朋友圈动态。第二个参数表示是否期望好友回复（true=期望回复，false=不期望），禁止滥用。")
    abilities.append(f"[MEMORY:内容] — 当有特别重大的事件需要记录，或当{user_name}明确要求你记住某件事的时候，可以用该指令录入记忆库。禁止滥用。")
    try:
        from routes.wallet import _get_balance
        _wb = await _get_balance()
        abilities.append(f"[转账：n元] — 给{user_name}转账（n为正整数），会从你的钱包余额中扣除。你的钱包当前余额：{_wb:.2f}元。余额不足时不要转账。")
    except Exception:
        pass
    ability_block = "[系统能力] 你可以在回复中根据对话氛围，善用以下指令：\n" + "\n".join(f"{i+1}. {a}" for i, a in enumerate(abilities))
    ability_block += "\n\n<meta>标签内为消息元数据，不是对话内容的一部分，你的回复中不要包含任何<meta>标签或时间信息。"
    # CLI 模型专属：告知图片存储目录
    _provider = MODELS.get(model_key, {}).get("provider", "")
    if _provider in ("gemini_cli", "codex_cli"):
        _uploads_path = str(UPLOADS_DIR.resolve()).replace(chr(92), "/")
        ability_block += f"\n\n【文件存储】当需要下载或保存图片/文件时，请保存到此目录：{_uploads_path}/ ，保存后在回复中给出完整路径即可，系统会自动识别并展示图片。"
    schedules = await get_active_schedules()
    schedule_text = build_schedule_prompt(schedules)
    ability_block += f"\n\n【当前日程列表】\n{schedule_text}"
    # 注入位置和天气信息（不注入 POI 列表）
    try:
        from location import format_location_for_prompt, load_location_config as _llc
        if _llc().get("enabled"):
            loc_prompt = format_location_for_prompt()
            if loc_prompt:
                ability_block += f"\n\n【位置信息】\n{loc_prompt}"
    except Exception:
        pass
    history.insert(cap_idx + inject_offset, {"role": "user", "content": ability_block})
    history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "好的，需要时我会使用这些指令。"})
    inject_offset += 2

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
        if surfaced:
            unresolved_lines = [f"📌 {m['content']}（还没做/还没去）" for m in surfaced if m.get("unresolved")]
            normal_lines = [f"- {m['content']}" for m in surfaced if not m.get("unresolved")]
            mem_text = "\n".join(unresolved_lines + normal_lines)
            bg_block += f"\n\n[背景记忆]\n以下是你记得的近期事件和需要关注的事项，在对话中如果有关联可以自然提起：\n{mem_text}"
        history.insert(cap_idx + inject_offset, {"role": "user", "content": bg_block})
        history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会在合适的时候自然提及。"})
        inject_offset += 2

        # 4. RAG 精确召回（与背景记忆去重）
        if topic:
            recall_query = f"{topic} {' '.join(recall_keywords)}"
        else:
            last_user_content = ""
            for m in reversed(history):
                if m["role"] == "user" and not m["content"].startswith("["):
                    last_user_content = m["content"][:200]
                    break
            recall_query = f"{last_user_content} {' '.join(recall_keywords)}"
        recall_query = recall_query.strip()

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
        debug_top6_data = [{"content": m["content"][:100], "score": m["score"],
                            "vec_sim": m.get("vec_sim"), "kw_score": m.get("kw_score"),
                            "importance": m.get("importance")} for m in debug_top6] if debug_top6 else []
        # 5. 注入相关记忆（在背景记忆之后）
        if recalled:
            mem_lines = "\n".join([f"- {m['content']}" for m in recalled])
            mem_block = f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"
            if detail_text:
                mem_block += f"\n\n[原文细节]\n以下是相关的具体对话记录：\n{detail_text}"
            history.insert(cap_idx + inject_offset, {"role": "user", "content": mem_block})
            history.insert(cap_idx + inject_offset + 1, {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"})

    debug_prompt = [{"role": m["role"], "content": m["content"][:500]} for m in history]
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
                    await _q.put({"type": "chunk", "content": chunk})
                    if regen_tts:
                        regen_tts.feed(chunk)
            except Exception as e:
                has_error = True
                error_text = f"\n[请求出错: {str(e)}]"
                full_text += error_text
                await _q.put({"type": "chunk", "content": error_text})

            # 检查 AI 返回的错误文本
            stripped = full_text.strip()
            if not has_error and (stripped.startswith('[Gemini错误') or stripped.startswith('[硅基流动错误') or stripped.startswith('[中转站错误') or stripped.startswith('[错误]') or not stripped):
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
            full_text = await process_schedule_commands(full_text, conv_id)
            full_text = await _process_home_commands(full_text)

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
                        print(f"[MEMORY] AI 主动录入记忆: {mem_content[:50]}")

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
            full_text = META_TAG_PATTERN.sub("", full_text).strip()

            # 将音乐点歌信息存入 attachments，刷新后可显示胶囊
            music_atts = [{"type": "music", "name": s["name"], "artist": s["artist"], "id": s["id"]} for s in music_cards] if music_cards else []
            full_text, image_atts = _extract_reply_image_attachments(full_text)
            reply_atts = _dedupe_attachments(music_atts + image_atts)
            att_json = json.dumps(reply_atts, ensure_ascii=False) if reply_atts else ""

            now2 = time.time()
            async with get_db() as db2:
                await db2.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                    (ai_msg_id, conv_id, "assistant", full_text, now2, att_json)
                )
                await db2.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                await db2.commit()

            ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant", "content": full_text, "created_at": now2, "attachments": reply_atts}
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
                if cam.running:
                    cam_data = {'type': 'cam_check', 'conv_id': conv_id, 'model_key': model_key, 'msg_id': ai_msg_id}
                    await _q.put(cam_data)
                    await manager.broadcast({"type": "cam_check", "data": cam_data})
                    asyncio.create_task(_delayed_cam_check(conv_id, model_key))
                else:
                    await _q.put({'type': 'cam_offline'})

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
