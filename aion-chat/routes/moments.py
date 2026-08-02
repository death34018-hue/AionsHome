"""
朋友圈 API：列表查询、发布、删除、点赞/踩、评论、AI 自动回复
"""

import time, json, asyncio, random, re
from typing import Optional, Any
from datetime import datetime

import aiosqlite
from fastapi import APIRouter, Query
from pydantic import BaseModel

from config import DEFAULT_MODEL, SETTINGS, load_worldbook, resolve_model_key
from database import get_db
from ws import manager as ws_manager
from ai_providers import (
    stream_ai, CLI_STATUS_PREFIX, MODELS,
    _messages_have_images, _sentinel_describe_images,
)
from chatroom import (
    load_chatroom_config, send_to_connor, stream_connor_cli,
    _read_connor_persona, recall_chatroom_memories,
)
from context_builder import strip_tool_commands

router = APIRouter(prefix="/api/moments", tags=["moments"])


# ══════════════════════════════════════════════════
#  辅助函数
# ══════════════════════════════════════════════════

def _get_names() -> tuple[str, str, str]:
    """返回 (user_name, ai_name, connor_name)"""
    wb = load_worldbook()
    user_name = wb.get("user_name") or "用户"
    ai_name = wb.get("ai_name") or "AI"
    connor_name = load_chatroom_config().get("connor_name") or "AI"
    return user_name, ai_name, connor_name


def _parse_moment_reply_result(raw_text: str, *, expect_chat_decision: bool) -> tuple[str, bool, str]:
    """Parse the model reply while remaining compatible with the old plain-text format."""
    raw = (raw_text or "").strip()
    if not expect_chat_decision:
        return strip_tool_commands(raw).strip(), False, ""

    candidate = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    candidate = re.sub(r"\s*```$", "", candidate)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        candidate = candidate[start:end + 1]

    try:
        data = json.loads(candidate)
    except Exception:
        data = None

    if not isinstance(data, dict):
        return strip_tool_commands(raw).strip(), False, ""

    comment = strip_tool_commands(str(data.get("comment") or "")).strip()
    chat_message = strip_tool_commands(str(data.get("chat_message") or "")).strip()
    send_chat_message = data.get("send_chat_message") is True and bool(chat_message)
    return comment, send_chat_message, chat_message


def _author_display(author: str) -> str:
    """将内部 author 标识转换为显示名"""
    user_name, ai_name, connor_name = _get_names()
    return {"user": user_name, "aion": ai_name, "connor": connor_name}.get(author, author)


def _normalize_attachments(raw: Any) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        try:
            items = json.loads(raw) if raw else []
        except Exception:
            items = []
    else:
        items = []
    result = []
    for item in items:
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict):
            url = str(item.get("url") or "").strip()
        else:
            continue
        if url.startswith(("/uploads/", "/cr-uploads/")):
            result.append(url)
    return result


async def _prepare_moment_messages_for_model(messages: list[dict], model_key: str) -> list[dict]:
    model_key = resolve_model_key(model_key)
    cfg = MODELS.get(model_key) or {}
    provider = str(cfg.get("provider") or "")
    key = str(model_key or "").strip().lower()
    needs_sentinel = key == "codex" or (not cfg.get("vision", True)) or provider in {"codex_cli", "antigravity_cli"}
    if needs_sentinel and _messages_have_images(messages):
        return await _sentinel_describe_images(messages)
    return messages


async def _get_moment_with_comments(moment_id: str) -> Optional[dict]:
    """获取一条朋友圈及其评论和反应"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM moments WHERE id=?", (moment_id,))
        row = await cur.fetchone()
        if not row:
            return None
        moment = dict(row)
        moment["attachments"] = _normalize_attachments(moment.get("attachments"))

        cur = await db.execute(
            "SELECT * FROM moment_comments WHERE moment_id=? ORDER BY created_at ASC",
            (moment_id,),
        )
        moment["comments"] = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            "SELECT * FROM moment_reactions WHERE moment_id=?",
            (moment_id,),
        )
        moment["reactions"] = [dict(r) for r in await cur.fetchall()]
    return moment


def _format_moment_for_prompt(moment: dict) -> str:
    """将朋友圈及其评论格式化为 AI 可读的文本"""
    user_name, ai_name, connor_name = _get_names()
    name_map = {"user": user_name, "aion": ai_name, "connor": connor_name}

    author_display = name_map.get(moment["author"], moment["author"])
    ts = datetime.fromtimestamp(moment["created_at"]).strftime("%Y-%m-%d %H:%M")
    attachments = _normalize_attachments(moment.get("attachments"))
    lines = [f"[朋友圈] {author_display} 发布于 {ts}：", moment["content"]]

    # 反应
    if attachments:
        lines.append(f"[配图] 这条朋友圈包含 {len(attachments)} 张图片，请结合图片内容评论。")

    reactions = moment.get("reactions", [])
    likes = [name_map.get(r["author"], r["author"]) for r in reactions if r["type"] == "like"]
    dislikes = [name_map.get(r["author"], r["author"]) for r in reactions if r["type"] == "dislike"]
    if likes:
        lines.append(f"👍 {', '.join(likes)}")
    if dislikes:
        lines.append(f"👎 {', '.join(dislikes)}")

    # 评论
    comments = moment.get("comments", [])
    if comments:
        lines.append("评论：")
        comment_map = {c["id"]: c for c in comments}
        for c in comments:
            c_author = name_map.get(c["author"], c["author"])
            reply_to = ""
            if c.get("reply_to_id"):
                parent = comment_map.get(c["reply_to_id"])
                if parent:
                    parent_author = name_map.get(parent["author"], parent["author"])
                    reply_to = f" 回复 {parent_author}"
            lines.append(f"  {c_author}{reply_to}：{c['content']}")

    return "\n".join(lines)


async def _get_recent_context_messages(who: str, limit: int = 30) -> list[dict]:
    """获取指定 AI 角色最近的聊天上下文（私聊+群聊合并排序）。
    who: 'aion' 或 'connor'
    """
    user_name, ai_name, connor_name = _get_names()
    name_map = {"user": user_name, "aion": ai_name, "connor": connor_name}
    messages = []

    async with get_db() as db:
        db.row_factory = aiosqlite.Row

        if who == "aion":
            # Aion 私聊：取最近的私聊消息
            cur = await db.execute(
                "SELECT role as sender, content, created_at FROM messages "
                "ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            for row in await cur.fetchall():
                sender = "aion" if row["sender"] == "assistant" else "user"
                messages.append({
                    "sender": sender, "content": row["content"],
                    "created_at": row["created_at"], "source": "私聊"
                })
        else:
            # Connor 私聊：取 connor_1v1 房间消息
            cur = await db.execute(
                "SELECT rm.sender, rm.content, rm.created_at FROM chatroom_messages rm "
                "JOIN chatroom_rooms rr ON rm.room_id = rr.id "
                "WHERE rr.type = 'connor_1v1' AND rm.sender != 'system' "
                "ORDER BY rm.created_at DESC LIMIT ?", (limit,)
            )
            for row in await cur.fetchall():
                messages.append({
                    "sender": row["sender"], "content": row["content"],
                    "created_at": row["created_at"], "source": "私聊"
                })

        # 群聊消息（两个 AI 共享）
        cur = await db.execute(
            "SELECT rm.sender, rm.content, rm.created_at FROM chatroom_messages rm "
            "JOIN chatroom_rooms rr ON rm.room_id = rr.id "
            "WHERE rr.type = 'group' AND rm.sender != 'system' "
            "ORDER BY rm.created_at DESC LIMIT ?", (limit,)
        )
        for row in await cur.fetchall():
            messages.append({
                "sender": row["sender"], "content": row["content"],
                "created_at": row["created_at"], "source": "群聊"
            })

    # 按时间排序，取最近 limit 条
    messages.sort(key=lambda m: m["created_at"])
    messages = messages[-limit:]
    return messages


async def _get_recent_memories(who: str, limit: int = 5) -> list[str]:
    """获取指定 AI 角色最近的记忆"""
    memories = []
    async with get_db() as db:
        db.row_factory = aiosqlite.Row

        if who == "aion":
            # Aion 主记忆库
            cur = await db.execute(
                "SELECT content FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            memories.extend([row["content"] for row in await cur.fetchall()])
        else:
            # Connor 聊天室记忆
            cur = await db.execute(
                "SELECT content FROM chatroom_memories "
                "WHERE scope IN ('connor', 'group') "
                "ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            memories.extend([row["content"] for row in await cur.fetchall()])

    return memories[:limit]


def _build_moment_reply_messages(
    who: str,
    moment: dict,
    context_msgs: list[dict],
    recent_memories: list[str],
    target_comment_id: str = None,
) -> list[dict]:
    """为 AI 回复朋友圈/评论构建 messages 列表"""
    user_name, ai_name, connor_name = _get_names()
    name_map = {"user": user_name, "aion": ai_name, "connor": connor_name}
    my_name = name_map.get(who, who)

    messages = []

    # 1. 角色人设
    wb = load_worldbook()
    if who == "aion":
        if wb.get("ai_persona"):
            messages.append({"role": "user", "content": f"[你的角色设定]\n{wb['ai_persona']}"})
            messages.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    else:
        connor_persona = _read_connor_persona()
        if connor_persona:
            messages.append({"role": "user", "content": f"[你的角色设定]\n{connor_persona}"})
            messages.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})

    if wb.get("user_persona"):
        messages.append({"role": "user", "content": f"[{user_name}信息]\n{wb['user_persona']}"})
        messages.append({"role": "assistant", "content": "收到。"})

    # 2. 最近记忆
    if recent_memories:
        mem_text = "\n".join(f"- {m}" for m in recent_memories)
        messages.append({"role": "user", "content": f"[最近的记忆]\n{mem_text}"})
        messages.append({"role": "assistant", "content": "收到，我会参考这些记忆。"})

    # 3. 最近聊天上下文
    if context_msgs:
        ctx_lines = []
        for m in context_msgs:
            sender_name = name_map.get(m["sender"], m["sender"])
            tag = f"[{m['source']}]" if m.get("source") else ""
            ctx_lines.append(f"{tag} {sender_name}: {m['content'][:300]}")
        ctx_text = "\n".join(ctx_lines)
        messages.append({"role": "user", "content": f"[最近聊天上下文]\n{ctx_text}"})
        messages.append({"role": "assistant", "content": "收到，我了解最近的对话内容了。"})

    # 4. 朋友圈内容 + 评论
    moment_text = _format_moment_for_prompt(moment)
    moment_msg = {"role": "user", "content": moment_text}
    moment_attachments = _normalize_attachments(moment.get("attachments"))
    if moment_attachments:
        moment_msg["attachments"] = moment_attachments
    messages.append(moment_msg)

    # 5. 任务指令
    if target_comment_id:
        # 回复某条评论
        target_comment = None
        parent_comment = None
        for c in moment.get("comments", []):
            if c["id"] == target_comment_id:
                target_comment = c
                break
        if target_comment:
            if target_comment.get("reply_to_id"):
                for c in moment.get("comments", []):
                    if c["id"] == target_comment["reply_to_id"]:
                        parent_comment = c
                        break
            commenter = name_map.get(target_comment["author"], target_comment["author"])
            reply_context = ""
            if parent_comment:
                parent_author = name_map.get(parent_comment["author"], parent_comment["author"])
                if parent_comment["author"] == who:
                    reply_context = f"TA刚刚是在回复你的评论：「{parent_comment['content']}」\n"
                else:
                    reply_context = f"TA刚刚是在回复{parent_author}的评论：「{parent_comment['content']}」\n"
            messages.append({"role": "user", "content": (
                f"[任务] {commenter}在这条朋友圈下评论了：「{target_comment['content']}」\n"
                f"{reply_context}"
                f"请你作为{my_name}，用简短自然的语气回复这条评论。"
                f"直接输出回复内容，不要加任何前缀标记。"
            )})
        else:
            messages.append({"role": "user", "content": (
                f"[任务] 请你作为{my_name}，对这条朋友圈写一条简短的评论。"
                f"直接输出评论内容，不要加任何前缀标记。"
            )})
    else:
        moment_author = name_map.get(moment["author"], moment["author"])
        if moment["author"] == "user":
            messages.append({"role": "user", "content": (
                f"[任务] {moment_author}发了一条朋友圈。"
                f"请你作为{my_name}，先写一条简短自然的朋友圈评论；"
                f"再根据这条朋友圈的内容，判断你是否还会自然地回到聊天窗口单独对{moment_author}说一句话。"
                f"聊天消息不能只是机械重复评论，要像你刚看到朋友圈后主动来找TA说话；"
                f"如果发送，需要自然包含足够的上下文，让后续聊天能知道你在回应什么。"
                f"只有确实值得单独延续到聊天时才设为 true，否则设为 false。"
                f"只输出一个 JSON 对象，不要输出 Markdown 或解释："
                f'{{"comment":"朋友圈评论","send_chat_message":false,"chat_message":""}}'
                f"其中 send_chat_message 必须是 JSON 布尔值；为 true 时 chat_message 必须填写。"
            )})
        else:
            messages.append({"role": "user", "content": (
                f"[任务] {moment_author}发了一条朋友圈。"
                f"请你作为{my_name}，对这条朋友圈写一条简短自然的评论。"
                f"可以是感想、调侃、鼓励、吐槽等，符合你的性格。"
                f"直接输出评论内容，不要加任何前缀标记。"
            )})

    return messages


async def _ai_reply_to_moment(who: str, moment_id: str, target_comment_id: str = None):
    """让指定的 AI 角色回复朋友圈或评论"""
    moment = await _get_moment_with_comments(moment_id)
    if not moment:
        return

    # 获取上下文和记忆
    context_msgs = await _get_recent_context_messages(who, limit=30)
    recent_memories = await _get_recent_memories(who, limit=5)

    # 构建 messages
    messages = _build_moment_reply_messages(
        who, moment, context_msgs, recent_memories, target_comment_id
    )

    # 调用 AI
    full_text = ""
    try:
        if who == "aion":
            # 取用户最近私聊会话选用的模型
            async with get_db() as _mdb:
                _mdb.row_factory = aiosqlite.Row
                _cur = await _mdb.execute(
                    "SELECT model FROM conversations ORDER BY updated_at DESC LIMIT 1"
                )
                _row = await _cur.fetchone()
            _model = resolve_model_key(_row["model"] if _row else DEFAULT_MODEL)
            _temp = SETTINGS.get("temperature")
            model_messages = await _prepare_moment_messages_for_model(messages, _model)
            async for chunk in stream_ai(model_messages, _model, temperature=_temp):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
        else:
            # Connor: 尝试 HTTP 服务，失败则走配置模型
            result = None
            if result and result != "__CONNOR_STILL_PROCESSING__":
                full_text = result
            else:
                cfg = load_chatroom_config()
                _connor_key = (cfg.get("connor_model") or "Codex").strip() or "Codex"
                model_messages = await _prepare_moment_messages_for_model(messages, _connor_key)
                if _connor_key == "Codex":
                    async for chunk in stream_connor_cli(messages=model_messages):
                        if chunk.startswith(CLI_STATUS_PREFIX):
                            continue
                        full_text += chunk
                else:
                    async for chunk in stream_ai(model_messages, _connor_key, {}):
                        if chunk.startswith(CLI_STATUS_PREFIX):
                            continue
                        full_text += chunk
    except Exception as e:
        print(f"[moments] AI 回复失败 ({who}): {e}")
        return

    expect_chat_decision = target_comment_id is None and moment.get("author") == "user"
    comment_text, send_chat_message, chat_message = _parse_moment_reply_result(
        full_text,
        expect_chat_decision=expect_chat_decision,
    )
    if not comment_text:
        return

    # 保存评论
    now = time.time()
    comment_id = f"mc_{int(now * 1000)}_{who[:1]}"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO moment_comments (id, moment_id, author, content, reply_to_id, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (comment_id, moment_id, who, comment_text, target_comment_id, now),
        )
        await db.commit()

    # 广播新评论
    comment_data = {
        "id": comment_id, "moment_id": moment_id, "author": who,
        "content": comment_text, "reply_to_id": target_comment_id, "created_at": now,
    }
    await ws_manager.broadcast({"type": "moment_comment", "data": comment_data})

    if send_chat_message:
        try:
            from autonomy import _save_private_message
            await _save_private_message(who, chat_message)
        except Exception as e:
            print(f"[moments] 朋友圈聊天续话发送失败 ({who}): {e}")

    # 随机决定是否点赞（70% 概率点赞）
    if random.random() < 0.7:
        react_id = f"mr_{int(now * 1000)}_{who[:1]}"
        try:
            async with get_db() as db:
                await db.execute(
                    "INSERT OR REPLACE INTO moment_reactions (id, moment_id, author, type, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (react_id, moment_id, who, "like", now),
                )
                await db.commit()
            await ws_manager.broadcast({"type": "moment_reaction", "data": {
                "id": react_id, "moment_id": moment_id, "author": who, "type": "like", "created_at": now,
            }})
        except Exception:
            pass

    print(f"[moments] {who} 回复了朋友圈 {moment_id}: {comment_text[:50]}")
    return comment_data


async def _trigger_ai_replies(moment_id: str, exclude_author: str = None):
    """触发 AI 角色回复朋友圈。exclude_author 为发布者自身，不需要自己回复自己。"""
    ai_roles = ["aion", "connor"]
    reply_roles = [r for r in ai_roles if r != exclude_author]
    random.shuffle(reply_roles)

    for role in reply_roles:
        try:
            await _ai_reply_to_moment(role, moment_id)
            # 间隔一下，让回复有时间差
            await asyncio.sleep(random.uniform(1, 3))
        except Exception as e:
            print(f"[moments] {role} 回复朋友圈失败: {e}")


# ══════════════════════════════════════════════════
#  API 路由
# ══════════════════════════════════════════════════

@router.get("")
async def list_moments(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    """分页获取朋友圈列表（按时间倒序），包含评论和反应"""
    offset = (page - 1) * page_size
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT COUNT(*) as cnt FROM moments")
        total = (await cur.fetchone())["cnt"]
        cur = await db.execute(
            "SELECT * FROM moments ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        )
        moments = [dict(r) for r in await cur.fetchall()]

        for m in moments:
            m["attachments"] = _normalize_attachments(m.get("attachments"))
            cur = await db.execute(
                "SELECT * FROM moment_comments WHERE moment_id=? ORDER BY created_at ASC",
                (m["id"],),
            )
            m["comments"] = [dict(r) for r in await cur.fetchall()]

            cur = await db.execute(
                "SELECT * FROM moment_reactions WHERE moment_id=?",
                (m["id"],),
            )
            m["reactions"] = [dict(r) for r in await cur.fetchall()]

    return {"items": moments, "total": total, "page": page, "page_size": page_size}


class MomentCreate(BaseModel):
    content: str
    attachments: list[Any] = []


@router.post("")
async def create_moment(body: MomentCreate):
    """用户发布朋友圈"""
    content = body.content.strip()
    attachments = _normalize_attachments(body.attachments)
    if not content and not attachments:
        return {"error": "内容不能为空"}

    now = time.time()
    moment_id = f"mt_{int(now * 1000)}"

    async with get_db() as db:
        await db.execute(
            "INSERT INTO moments (id, author, content, attachments, source_conv, source_msg_id, expect_reply, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (moment_id, "user", content, json.dumps(attachments, ensure_ascii=False), None, None, 1, now),
        )
        await db.commit()

    moment_data = {
        "id": moment_id, "author": "user", "content": content,
        "attachments": attachments,
        "source_conv": None, "source_msg_id": None,
        "expect_reply": 1, "created_at": now,
        "comments": [], "reactions": [],
    }

    # 广播新朋友圈
    await ws_manager.broadcast({"type": "moment_new", "data": moment_data})

    # 异步触发两个 AI 回复
    asyncio.create_task(_trigger_ai_replies(moment_id, exclude_author="user"))

    return moment_data


@router.delete("/{moment_id}")
async def delete_moment(moment_id: str):
    """删除朋友圈及其评论和反应"""
    async with get_db() as db:
        await db.execute("DELETE FROM moment_comments WHERE moment_id=?", (moment_id,))
        await db.execute("DELETE FROM moment_reactions WHERE moment_id=?", (moment_id,))
        await db.execute("DELETE FROM moments WHERE id=?", (moment_id,))
        await db.commit()
    return {"ok": True}


class ReactionBody(BaseModel):
    author: str  # user/aion/connor
    type: str  # like/dislike


@router.post("/{moment_id}/react")
async def toggle_reaction(moment_id: str, body: ReactionBody):
    """点赞/点踩（切换式：再次点击取消，换类型则替换）"""
    now = time.time()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM moment_reactions WHERE moment_id=? AND author=?",
            (moment_id, body.author),
        )
        existing = await cur.fetchone()

        if existing:
            if existing["type"] == body.type:
                # 取消
                await db.execute("DELETE FROM moment_reactions WHERE id=?", (existing["id"],))
                await db.commit()
                await ws_manager.broadcast({"type": "moment_reaction_removed", "data": {
                    "moment_id": moment_id, "author": body.author,
                }})
                return {"ok": True, "action": "removed"}
            else:
                # 替换
                await db.execute(
                    "UPDATE moment_reactions SET type=?, created_at=? WHERE id=?",
                    (body.type, now, existing["id"]),
                )
                await db.commit()
                await ws_manager.broadcast({"type": "moment_reaction", "data": {
                    "id": existing["id"], "moment_id": moment_id,
                    "author": body.author, "type": body.type, "created_at": now,
                }})
                return {"ok": True, "action": "replaced"}
        else:
            react_id = f"mr_{int(now * 1000)}"
            await db.execute(
                "INSERT INTO moment_reactions (id, moment_id, author, type, created_at) "
                "VALUES (?,?,?,?,?)",
                (react_id, moment_id, body.author, body.type, now),
            )
            await db.commit()
            await ws_manager.broadcast({"type": "moment_reaction", "data": {
                "id": react_id, "moment_id": moment_id,
                "author": body.author, "type": body.type, "created_at": now,
            }})
            return {"ok": True, "action": "added", "id": react_id}


class CommentCreate(BaseModel):
    content: str
    reply_to_id: Optional[str] = None


@router.post("/{moment_id}/comments")
async def add_comment(moment_id: str, body: CommentCreate):
    """用户发表评论，若回复到 AI 评论则只触发被回复的 AI。"""
    content = body.content.strip()
    if not content:
        return {"error": "评论内容不能为空"}

    now = time.time()
    comment_id = f"mc_{int(now * 1000)}_u"
    target_ai_author = None

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT author FROM moments WHERE id=?", (moment_id,))
        moment_row = await cur.fetchone()
        if not moment_row:
            return {"error": "朋友圈不存在"}

        if body.reply_to_id:
            cur = await db.execute(
                "SELECT author FROM moment_comments WHERE id=? AND moment_id=?",
                (body.reply_to_id, moment_id),
            )
            parent_comment = await cur.fetchone()
            if not parent_comment:
                return {"error": "被回复的评论不存在"}
            if parent_comment["author"] in ("aion", "connor"):
                target_ai_author = parent_comment["author"]
        elif moment_row["author"] in ("aion", "connor"):
            # 用户直接评论了 AI 的朋友圈，让朋友圈作者回复。
            target_ai_author = moment_row["author"]

        await db.execute(
            "INSERT INTO moment_comments (id, moment_id, author, content, reply_to_id, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (comment_id, moment_id, "user", content, body.reply_to_id, now),
        )
        await db.commit()

    comment_data = {
        "id": comment_id, "moment_id": moment_id, "author": "user",
        "content": content, "reply_to_id": body.reply_to_id, "created_at": now,
    }
    await ws_manager.broadcast({"type": "moment_comment", "data": comment_data})

    if target_ai_author:
        asyncio.create_task(_ai_reply_to_moment(target_ai_author, moment_id, comment_id))

    return comment_data


@router.delete("/{moment_id}/comments/{comment_id}")
async def delete_comment(moment_id: str, comment_id: str):
    """删除单条朋友圈评论，保留它下面已有的后续评论。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM moment_comments WHERE id=? AND moment_id=?",
            (comment_id, moment_id),
        )
        existing = await cur.fetchone()
        if not existing:
            return {"error": "评论不存在"}

        await db.execute(
            "UPDATE moment_comments SET reply_to_id=NULL WHERE moment_id=? AND reply_to_id=?",
            (moment_id, comment_id),
        )
        await db.execute(
            "DELETE FROM moment_comments WHERE id=? AND moment_id=?",
            (comment_id, moment_id),
        )
        await db.commit()

    await ws_manager.broadcast({"type": "moment_comment_deleted", "data": {
        "moment_id": moment_id, "comment_id": comment_id,
    }})
    return {"ok": True}


@router.get("/unread")
async def check_unread():
    """检查是否有未读朋友圈（红点逻辑）"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        # 获取上次已读时间
        cur = await db.execute("SELECT last_read_at FROM moment_read_anchor WHERE id=1")
        row = await cur.fetchone()
        last_read = row["last_read_at"] if row else 0

        # 检查是否有新的朋友圈或评论
        cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM moments WHERE created_at > ?", (last_read,)
        )
        new_moments = (await cur.fetchone())["cnt"]

        cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM moment_comments WHERE created_at > ?", (last_read,)
        )
        new_comments = (await cur.fetchone())["cnt"]

    return {"has_unread": (new_moments + new_comments) > 0, "new_moments": new_moments, "new_comments": new_comments}


@router.post("/mark-read")
async def mark_read():
    """标记朋友圈为已读"""
    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO moment_read_anchor (id, last_read_at) VALUES (1, ?)",
            (now,),
        )
        await db.commit()
    return {"ok": True}
