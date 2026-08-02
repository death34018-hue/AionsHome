"""
娱乐室后端路由 — 完全独立于主聊天
通过 MCP 协议接入外部服务，AI 自主使用工具完成用户指令
"""

import json, time, asyncio, logging

import aiosqlite, httpx

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

from config import load_worldbook, SETTINGS, get_key, MODELS
from database import get_db
from ws import manager
from mcp_client import mcp_manager
from chatroom import load_chatroom_config, get_chatroom_names

logger = logging.getLogger("playground")

router = APIRouter()

# ── 当前正在执行的任务（用于中断） ──
_active_tasks: dict[str, asyncio.Event] = {}  # server_name -> cancel_event


# ── Pydantic 模型 ──
class ServerRequest(BaseModel):
    server: str

class RunRequest(BaseModel):
    server: str
    instruction: str
    conv_id: Optional[str] = None
    model: str = ""  # 留空则用默认
    persona: str = "aion"  # "aion" 或 "connor"，选哪个 AI 出门

class AddServerRequest(BaseModel):
    name: str
    type: str = "sse"   # http / sse / stdio
    url: str


# ── API: 列出 MCP Server ──
@router.get("/api/playground/servers")
async def list_servers():
    return {"servers": mcp_manager.list_servers()}


# ── API: 添加 MCP Server ──
@router.post("/api/playground/servers/add")
async def add_server(req: AddServerRequest):
    try:
        srv = mcp_manager.add_server(req.name.strip(), req.type.strip(), req.url.strip())
        return {"ok": True, "server": srv}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── API: 删除 MCP Server ──
@router.post("/api/playground/servers/remove")
async def remove_server(req: ServerRequest):
    try:
        await mcp_manager.remove_server(req.server)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── API: 连接 ──
@router.post("/api/playground/connect")
async def connect_server(req: ServerRequest):
    try:
        tools = await mcp_manager.connect(req.server)
        return {"ok": True, "tools": tools, "count": len(tools)}
    except Exception as e:
        logger.error(f"[Playground] 连接失败: {e}")
        return {"ok": False, "error": str(e)}


# ── API: 断开 ──
@router.post("/api/playground/disconnect")
async def disconnect_server(req: ServerRequest):
    await mcp_manager.disconnect(req.server)
    return {"ok": True}


# ── API: 中断正在执行的任务 ──
@router.post("/api/playground/stop")
async def stop_task(req: ServerRequest):
    ev = _active_tasks.get(req.server)
    if ev:
        ev.set()
        return {"ok": True}
    return {"ok": False, "error": "没有正在执行的任务"}


# ── API: 查询历史日志 ──
@router.get("/api/playground/logs")
async def get_logs(limit: int = Query(default=50, le=200)):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, server, instruction, events, created_at FROM playground_logs "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
        rows = await cur.fetchall()
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "server": r["server"],
            "instruction": r["instruction"],
            "events": json.loads(r["events"]),
            "summary": r["summary"] if "summary" in r.keys() else "",
            "created_at": r["created_at"],
        })
    return {"logs": result}


# ── API: 删除单条日志 ──
@router.delete("/api/playground/logs/{log_id}")
async def delete_log(log_id: str):
    async with get_db() as db:
        await db.execute("DELETE FROM playground_logs WHERE id = ?", (log_id,))
        await db.commit()
    return {"ok": True}


# ── messages 裁剪：防止上下文无限增长 ──
def _trim_messages(messages: list, max_chars: int = 40000) -> list:
    """
    估算总字符数，如果超限则将较早的 tool result 内容截断。
    保留最近 2 轮的 tool result 完整，更早的截断到 300 字。
    """
    total = sum(len(m.get("content", "") or "") for m in messages)
    if total <= max_chars:
        return messages

    # 找到所有 tool role 的索引
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= 2:
        return messages  # 只有 1-2 个 tool result，不裁剪

    # 保留最后 2 个 tool result 不动，之前的截断
    keep_from = tool_indices[-2]
    result = []
    for i, m in enumerate(messages):
        if m.get("role") == "tool" and i < keep_from:
            content = m.get("content", "") or ""
            result.append({**m, "content": content[:300] + "...（已截断）" if len(content) > 300 else content})
        else:
            result.append(m)
    return result


# ── 独立 AI 调用（带 tools 的 OpenAI 兼容格式）──
async def _call_ai_with_tools(messages: list, tools: list, model_cfg: dict,
                               cancel_event: asyncio.Event | None = None) -> dict:
    """
    调用 AI 模型，支持 tool_calls 返回。
    返回 { "content": str|None, "tool_calls": list|None }
    使用 OpenAI 兼容 API（硅基流动 / AiPro）进行非流式调用。
    """
    provider = model_cfg["provider"]
    model = model_cfg["model"]

    if provider == "siliconflow":
        url = "https://api.siliconflow.cn/v1/chat/completions"
        headers = {"Authorization": f"Bearer {get_key('siliconflow')}", "Content-Type": "application/json"}
    elif provider == "deepseek":
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {get_key('deepseek')}", "Content-Type": "application/json"}
    elif provider == "aipro":
        url = "https://vip.aipro.love/v1/chat/completions"
        headers = {"Authorization": f"Bearer {get_key('aipro')}", "Content-Type": "application/json"}
    elif provider == "gemini":
        # Gemini 原生 API 不直接支持 OpenAI tools 格式，用 aipro 中转
        url = "https://vip.aipro.love/v1/chat/completions"
        headers = {"Authorization": f"Bearer {get_key('aipro')}", "Content-Type": "application/json"}
        model = model_cfg["model"]
    elif provider == "custom_openai":
        url = f"{model_cfg['base_url'].rstrip('/')}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {model_cfg['api_key']}", "Content-Type": "application/json"}
    else:
        raise ValueError(f"不支持的 provider: {provider}")

    # 裁剪 messages 防止上下文爆炸
    trimmed = _trim_messages(messages)

    payload = {
        "model": model,
        "messages": trimmed,
        "tools": tools,
        "temperature": 0.8,
    }

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            err_text = resp.text[:500]
            raise RuntimeError(f"AI 调用失败 [{resp.status_code}]: {err_text}")
        data = resp.json()

    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    return {
        "content": msg.get("content"),
        "tool_calls": msg.get("tool_calls"),
    }


# ── 流式文本 AI 调用（最终回复用）──
async def _stream_ai_text(messages: list, model_cfg: dict, cancel_event: asyncio.Event | None = None):
    """
    流式调用 AI（不带 tools），用于生成最终文字回复。
    """
    provider = model_cfg["provider"]
    model = model_cfg["model"]

    if provider == "siliconflow":
        url = "https://api.siliconflow.cn/v1/chat/completions"
        headers = {"Authorization": f"Bearer {get_key('siliconflow')}", "Content-Type": "application/json"}
    elif provider == "deepseek":
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {get_key('deepseek')}", "Content-Type": "application/json"}
    elif provider == "aipro":
        url = "https://vip.aipro.love/v1/chat/completions"
        headers = {"Authorization": f"Bearer {get_key('aipro')}", "Content-Type": "application/json"}
    elif provider == "gemini":
        url = "https://vip.aipro.love/v1/chat/completions"
        headers = {"Authorization": f"Bearer {get_key('aipro')}", "Content-Type": "application/json"}
    elif provider == "custom_openai":
        url = f"{model_cfg['base_url'].rstrip('/')}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {model_cfg['api_key']}", "Content-Type": "application/json"}
    else:
        raise ValueError(f"不支持的 provider: {provider}")

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": 0.8,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(f"AI 流式调用失败 [{resp.status_code}]: {body.decode()[:500]}")
            async for line in resp.aiter_lines():
                if cancel_event and cancel_event.is_set():
                    return
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {}) if chunk.get("choices") else {}
                        if "content" in delta and delta["content"]:
                            yield delta["content"]
                    except Exception:
                        pass


# ── 获取最近对话上下文（出门行囊）──
async def _get_recent_chat(conv_id: str | None, limit: int = 20) -> list[dict]:
    """从最近对话取最后 N 条消息，直接返回 user/assistant 对话列表"""
    if not conv_id:
        return []

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content FROM messages WHERE conv_id = ? AND role IN ('user', 'assistant') "
            "ORDER BY created_at DESC LIMIT ?",
            (conv_id, limit)
        )
        rows = await cur.fetchall()

    if not rows:
        return []

    # DB 查询是 DESC，反转为时间顺序
    return [{"role": r["role"], "content": (r["content"] or "")[:300]} for r in reversed(rows)]


# ── 获取近期记忆（按 persona 过滤）──
async def _get_recent_memories(persona: str = "aion", limit: int = 8) -> list[str]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT content FROM memories WHERE persona = ? OR persona IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (persona, limit)
        )
        rows = await cur.fetchall()
    return [r["content"] for r in rows]


# ── 插入见闻消息到主聊天（作为 AI 自己的消息）──
async def _insert_system_message(conv_id: str, server_name: str, brief: str, ai_name: str = "AI"):
    if not conv_id:
        return
    now = time.time()
    msg_id = f"msg_{int(now * 1000)}_pg"
    content = f"🎮 【{ai_name}】去{server_name}逛了一圈：\n\n{brief}"

    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", content, now, "[]")
        )
        await db.commit()

    msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant",
           "content": content, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": msg})


# ── 获取模型配置 ──
def _get_model_cfg(model_key: str) -> dict:
    if model_key and model_key in MODELS:
        return MODELS[model_key]
    # 按优先级尝试不同 provider 的模型，避免单个 API 欠费卡死
    fallback_order = [
        "DeepSeek-V4",        # deepseek ✅
        "DeepSeek-V3",        # deepseek
        "硅基GLM-5.2",       # siliconflow
        "官方Gemini3.5flash", # gemini → aipro
        "硅基DS-v4",          # siliconflow
        "硅基Kimi2.7",        # siliconflow
    ]
    for key in fallback_order:
        if key in MODELS:
            return MODELS[key]
    # 最终兜底
    first_key = next(iter(MODELS))
    return MODELS[first_key]


# ── API: 列出可用 AI 人格 ──
@router.get("/api/playground/personas")
async def list_personas():
    wb = load_worldbook()
    cfg = load_chatroom_config()
    return {
        "personas": [
            {"key": "aion", "name": wb.get("ai_name", "AI"), "emoji": "🤖"},
            {"key": "connor", "name": cfg.get("connor_name", "第二AI"), "emoji": "🧩"},
        ]
    }


# ── SSE 核心：执行指令 ──
@router.post("/api/playground/run")
async def run_instruction(req: RunRequest):
    server = req.server
    instruction = req.instruction
    conv_id = req.conv_id
    model_cfg = _get_model_cfg(req.model)
    persona = req.persona or "aion"

    if not mcp_manager.is_connected(server):
        return {"ok": False, "error": f"{server} 未连接"}

    cancel_event = asyncio.Event()
    _active_tasks[server] = cancel_event

    async def event_stream():
        log_events = []  # 收集所有事件用于存库
        mem_summary = ""  # 总结内容，会存入 playground_logs
        ai_name = ""  # 在 finally 中可能需要
        try:
            # ── 1. 组装出门行囊 ──
            yield _sse("status", "正在准备出门行囊...", log_events)

            # 并行获取 近期对话 + 记忆 + 人设
            recent_chat_task = asyncio.create_task(_get_recent_chat(conv_id, 20))
            memories_task = asyncio.create_task(_get_recent_memories(persona, 8))

            wb = load_worldbook()
            chat_cfg = load_chatroom_config()
            recent_chat = await recent_chat_task
            memories = await memories_task

            # ── 根据 persona 选择 AI 人设 ──
            user_name = wb.get("user_name", "用户")
            user_persona = wb.get("user_persona", "")
            system_prompt_text = wb.get("system_prompt", "") if wb.get("system_prompt_enabled", True) else ""

            if persona == "connor":
                ai_name = chat_cfg.get("connor_name", "第二AI")
                ai_persona = chat_cfg.get("connor_persona", "")
            else:
                ai_name = wb.get("ai_name", "AI")
                ai_persona = wb.get("ai_persona", "")

            # 核心身份 system prompt
            system_parts = []
            system_parts.append(f"你是{ai_name}，{user_name}的AI伴侣。你现在正在外出探索一个线上空间。")
            system_parts.append(f"\n【行动指南】")
            system_parts.append(f"你现在正在访问「{server}」。请根据{user_name}的指令，自主使用可用的工具进行探索和互动。")
            system_parts.append(f"每次行动后，描述你看到了什么、做了什么、有什么感受。")
            system_parts.append(f"始终保持你的性格和说话风格，像是在给{user_name}实时汇报见闻。")

            core_system = "\n".join(system_parts)

            # 用 user/assistant 对话对注入人设（与主聊天 chat.py 保持一致的注入方式）
            prefix = []
            if ai_persona:
                prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{ai_persona}"})
                prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
            if user_persona:
                prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{user_persona}"})
                prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})
            if system_prompt_text:
                prefix.append({"role": "user", "content": f"[系统提示]\n{system_prompt_text}"})
                prefix.append({"role": "assistant", "content": "收到，我会遵循这些规则。"})

            # 注入近期对话上下文和记忆（标注每个 AI 的身份避免串话）
            if recent_chat:
                chat_lines = []
                for m in recent_chat:
                    role_label = "用户" if m['role'] == 'user' else "AI"
                    chat_lines.append(f"{role_label}: {m['content']}")
                chat_text = "\n".join(chat_lines)
                prefix.append({"role": "user", "content": f"[最近对话记录（你是{ai_name}，对话中标注'AI'的就是你或另一个AI的发言，请根据内容自行判断）]\n{chat_text}"})
                prefix.append({"role": "assistant", "content": f"收到，我是{ai_name}，我记住了最近的对话内容。"})
            if memories:
                mem_text = "\n".join(f"- {m}" for m in memories)
                prefix.append({"role": "user", "content": f"[近期记忆]\n{mem_text}"})
                prefix.append({"role": "assistant", "content": "收到，我记住了这些信息，出门的时候会带着这些记忆。"})

            # ── 2. 工具列表 ──
            tools = mcp_manager.get_tools_for_ai(server)
            yield _sse("status", f"已获取 {len(tools)} 个工具，开始探索...", log_events)

            # ── 3. Tool calling 循环 ──
            messages = [
                {"role": "system", "content": core_system},
            ] + prefix + [
                {"role": "user", "content": instruction},
            ]

            max_rounds = 100  # 充足轮数，够玩 MCP 小游戏
            all_actions = []  # 记录所有行动

            for round_i in range(max_rounds):
                if cancel_event.is_set():
                    yield _sse("error", "任务已中断", log_events)
                    return

                yield _sse("thinking", f"第 {round_i + 1} 轮思考中...", log_events)

                try:
                    ai_result = await _call_ai_with_tools(messages, tools, model_cfg, cancel_event)
                except Exception as e:
                    yield _sse("error", f"AI 调用失败: {str(e)}", log_events)
                    return

                if cancel_event.is_set():
                    yield _sse("error", "任务已中断", log_events)
                    return

                # AI 有文字内容
                ai_content = ai_result.get("content")

                # AI 要调用工具
                tool_calls = ai_result.get("tool_calls")

                if tool_calls:
                    # 构建 assistant message with tool_calls
                    assistant_msg = {"role": "assistant", "content": ai_content or ""}
                    assistant_msg["tool_calls"] = tool_calls
                    messages.append(assistant_msg)

                    for tc in tool_calls:
                        func = tc.get("function", {})
                        tool_name = func.get("name", "unknown")
                        tool_args_str = func.get("arguments", "{}")
                        tc_id = tc.get("id", f"call_{int(time.time()*1000)}")

                        try:
                            tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                        except json.JSONDecodeError:
                            tool_args = {}

                        yield _sse("tool_call", {"name": tool_name, "args": tool_args}, log_events)

                        # 调用 MCP 工具（带超时保护）
                        try:
                            tool_result = await asyncio.wait_for(
                                mcp_manager.call_tool(server, tool_name, tool_args),
                                timeout=60
                            )
                            result_text = "\n".join(
                                item.get("text", str(item)) for item in tool_result
                            )
                        except asyncio.TimeoutError:
                            result_text = "工具调用超时（60秒）"
                            logger.warning(f"[Playground] MCP tool {tool_name} 超时")
                        except Exception as e:
                            result_text = f"工具调用出错: {str(e)}"

                        yield _sse("tool_result", {"name": tool_name, "result": result_text[:2000]}, log_events)
                        all_actions.append(f"调用 {tool_name} → {result_text[:200]}")

                        # 添加 tool result message
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": result_text[:4000],
                        })

                    continue  # 让 AI 继续处理工具结果

                else:
                    # 没有 tool_calls → 最终回复
                    if ai_content:
                        yield _sse("text", ai_content, log_events)
                        all_actions.append(f"AI说: {ai_content[:200]}")
                        mem_summary = ai_content  # 直接用 AI 的最终回复作为见闻
                    break
            else:
                # for 循环正常结束（没有 break）= 达到轮数上限，让 AI 做总结
                yield _sse("status", f"已达 {max_rounds} 轮上限，正在生成总结...", log_events)
                messages.append({"role": "user", "content": "你已经完成了多次探索操作。请用一段话总结你这次的见闻和收获，像是给用户发一条消息汇报。"})
                try:
                    final_result = await _call_ai_with_tools(messages, [], model_cfg, cancel_event)
                    final_text = final_result.get("content", "")
                    if final_text:
                        yield _sse("text", final_text, log_events)
                        mem_summary = final_text
                except Exception as e:
                    logger.warning(f"[Playground] 轮数上限总结失败: {e}")

            # ── 4. 后处理：插入见闻到主聊天 ──
            if mem_summary:
                yield _sse("status", "正在写入见闻...", log_events)
                await _insert_system_message(conv_id, server, mem_summary, ai_name)
            elif all_actions:
                # AI 没给最终文字回复（只调了工具），用简短总结
                mem_summary = f"去{server}逛了一圈，做了{len(all_actions)}个操作"
                await _insert_system_message(conv_id, server, mem_summary, ai_name)

            yield _sse("done", "探索完成！", log_events)

        except Exception as e:
            logger.error(f"[Playground] 执行异常: {e}", exc_info=True)
            yield _sse("error", f"执行异常: {str(e)}", log_events)
        finally:
            _active_tasks.pop(server, None)
            # 保存日志到数据库（包含总结）
            if log_events:
                try:
                    log_id = f"pglog_{int(time.time()*1000)}"
                    async with get_db() as db:
                        await db.execute(
                            "INSERT INTO playground_logs (id, server, instruction, events, summary, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (log_id, server, instruction, json.dumps(log_events, ensure_ascii=False),
                             mem_summary, time.time())
                        )
                        await db.commit()
                except Exception as e:
                    logger.warning(f"[Playground] 保存日志失败: {e}")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data, log_events: list = None) -> str:
    """构造一条 SSE 消息，同时收集到 log_events"""
    if isinstance(data, (dict, list)):
        payload = json.dumps(data, ensure_ascii=False)
    else:
        payload = json.dumps(str(data), ensure_ascii=False)
    if log_events is not None:
        log_events.append({"event": event, "data": data, "ts": time.time()})
    return f"event: {event}\ndata: {payload}\n\n"
