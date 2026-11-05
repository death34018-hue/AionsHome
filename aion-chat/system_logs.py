"""Persistent, bounded AI call diagnostics shared by background and chat calls."""

import asyncio
import inspect
import json
import logging
import re
import sqlite3
import time
import uuid
from functools import wraps
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import APIRouter
from config import BASE_DIR, DB_PATH

LOG_DB_PATH = DB_PATH.with_name("system_logs.db")
LIMIT = 30
log = logging.getLogger(__name__)
_active_call = ContextVar("system_log_call", default=None)
router = APIRouter()


@contextmanager
def _connect():
    db = sqlite3.connect(LOG_DB_PATH, timeout=5)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS logs (id TEXT PRIMARY KEY, started REAL, payload TEXT)")
            yield db
    finally:
        db.close()


def save_log(entry):
    with _connect() as db:
        db.execute("INSERT OR REPLACE INTO logs VALUES (?, ?, ?)",
                   (entry["log_id"], entry["started_at"], json.dumps(entry, ensure_ascii=False, default=str)))
        db.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY started DESC LIMIT ?)", (LIMIT,))


@router.get("/api/system-logs")
def list_logs(room_id: str | None = None):
    with _connect() as db:
        rows = db.execute("SELECT payload FROM logs WHERE (? IS NULL OR json_extract(payload,'$.room_id')=?) "
                          "ORDER BY started DESC LIMIT ?", (room_id, room_id, LIMIT))
        return [json.loads(row[0]) for row in rows]


@router.delete("/api/system-logs")
def clear_logs(room_id: str | None = None):
    with _connect() as db:
        db.execute("DELETE FROM logs WHERE (? IS NULL OR json_extract(payload,'$.room_id')=?)", (room_id, room_id))
    return {"ok": True}


def enrich_debug(data):
    """Attach the existing chat-only recall details to the underlying call."""
    log_id = data.get("log_id") or (data.get("usage") or {}).get("system_log_id")
    if not log_id:
        return data
    with _connect() as db:
        row = db.execute("SELECT payload FROM logs WHERE id = ?", (log_id,)).fetchone()
        details = {k:v for k,v in data.items() if k not in ("prompt_messages","prompt_count")}
        if not row:
            return {**details, "log_id": log_id}
        saved = json.loads(row[0])
        entry = {**saved, **details, "log_id": log_id}
        if "usage" in saved:
            entry["usage"] = saved["usage"]
        if "model" in saved:
            entry["model"] = saved["model"]
        db.execute("UPDATE logs SET payload = ? WHERE id = ?",
                   (json.dumps(entry, ensure_ascii=False, default=str), log_id))
    return entry


async def emit_chat_debug(queue, payload, manager):
    try:
        payload = enrich_debug(payload)
    except Exception:
        log.exception("Unable to attach call diagnostics")
    await queue.put(payload)
    await manager.broadcast({"type":"debug", "data":payload})


def _prompt_messages(messages):
    result = []
    for m in messages:
        content = m.get("content", m.get("parts", ""))
        if isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part,dict):
                    parts.append(str(part))
                elif isinstance(part.get("text"),str):
                    parts.append(part["text"])
                else:
                    kind = part.get("type") or (part.get("inline_data") or {}).get("mime_type") or "media"
                    parts.append(f"[附件：{kind}]")
            content = "\n".join(parts)
        result.append({"role":m.get("role",""), "content":str(content)})
    return result


async def record_sent_messages(messages):
    entry = _active_call.get()
    if entry is not None:
        entry["prompt_messages"] = _prompt_messages(messages)
        entry["prompt_count"] = len(messages)
        entry["prompt_source"] = "dispatch"
        await _publish(entry)


async def record_cli_prompt(prompt, *, instructions_file=None, developer="", images=()):
    if _active_call.get() is None:
        return
    messages = []
    if instructions_file:
        try:
            instructions = instructions_file.read_text(encoding="utf-8")
        except (OSError,UnicodeError):
            instructions = f"[无法读取指令文件：{instructions_file.name}]"
        messages.append({"role":"system","content":instructions})
    if developer:
        messages.append({"role":"developer","content":developer})
    messages.append({"role":"user","content":prompt})
    if images:
        messages.append({"role":"附件","content":"\n".join(map(str,images))})
    await record_sent_messages(messages)


async def _broadcast(entry):
    from ws import manager
    await manager.broadcast({"type": "system_log", "data": entry})


async def _publish(entry):
    # Diagnostics must never prevent the original AI call or mask its error.
    try:
        save_log(entry)
    except Exception:
        log.exception("Unable to save AI system log")
    try:
        await _broadcast(entry)
    except Exception:
        log.exception("Unable to broadcast AI system log")


def _new_entry(arguments, provider):
    cfg = arguments.get("cfg") or arguments.get("scfg") or arguments.get("model_cfg") or {}
    model = arguments.get("model") or cfg.get("model") or provider
    messages = arguments.get("messages")
    if messages is None:
        messages = [{"role": "user", "content": arguments.get("prompt") or arguments.get("text") or ""}]
    source = provider
    frame = inspect.currentframe()
    try:
        while frame:
            module = frame.f_globals.get("__name__", "")
            if module not in (__name__, "ai_providers") and (
                frame.f_code.co_filename.replace("\\", "/").startswith(str(BASE_DIR).replace("\\", "/"))
                or module.startswith("routes.") or module in ("camera", "schedule", "memory", "chatroom", "autonomy", "post_sentinel")
            ):
                source = f"{module}.{frame.f_code.co_name}"
                break
            frame = frame.f_back
    finally:
        del frame
    return {
        "log_id": uuid.uuid4().hex, "started_at": time.time(),
        "model": model, "provider": provider, "source": source,
        "status": "running", "has_error": False,
        "prompt_messages": _prompt_messages(messages),
        "prompt_count": len(messages), "usage": None,
    }


def logged_ai_call(provider, *, model=None):
    """Instrument provider boundaries, including callers that omit usage meta."""
    def decorate(func):
        signature = inspect.signature(func)

        def prepare(args, kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            entry = _new_entry(bound.arguments, provider)
            if model is not None:
                entry["model"] = model() if callable(model) else model
            meta = bound.arguments.get("meta")
            from generation_control import current_generation
            generation = current_generation()
            if generation and generation.surface == "chatroom":
                entry["room_id"] = generation.target
            if "meta" in signature.parameters:
                if meta is None:
                    meta = {}
                    bound.arguments["meta"] = meta
                meta["system_log_id"] = entry["log_id"]
                if meta.get("_room_log"):
                    meta["_call_number"] = meta.get("_call_number",0) + 1
                    entry.update(meta["_room_log"], call_number=meta["_call_number"])
            return bound, entry, meta

        async def finish(entry, meta, status, error=""):
            entry.update(status=status, has_error=status in ("error", "cancelled"),
                         error_text=error or None, finished_at=time.time())
            entry["duration_seconds"] = round(entry["finished_at"] - entry["started_at"], 3)
            if meta is not None:
                entry["usage"] = {k: v for k, v in meta.items() if not k.startswith("_") and k not in ("system_log_id", "reasoning_content")} or None
            try:
                await _publish(entry)
            except asyncio.CancelledError:
                # Saving happens before broadcasting, even during cancellation.
                pass

        if inspect.isasyncgenfunction(func):
            @wraps(func)
            async def stream(*args, **kwargs):
                bound, entry, meta = prepare(args, kwargs)
                status, error = "success", ""
                generator = func(*bound.args, **bound.kwargs)
                try:
                    await _publish(entry)
                    while True:
                        token = _active_call.set(entry)
                        try:
                            chunk = await anext(generator)
                        except StopAsyncIteration:
                            break
                        finally:
                            _active_call.reset(token)
                        if isinstance(chunk, str) and re.match(r"^\[[^\]\n]*(?:错误|error)[^\]\n]*\]", chunk, re.I):
                            status, error = "error", chunk
                        yield chunk
                except (asyncio.CancelledError, GeneratorExit):
                    status, error = "cancelled", "调用已中断"
                    raise
                except Exception as exc:
                    status, error = "error", f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    try:
                        await generator.aclose()
                    finally:
                        await finish(entry, meta, status, error)
            return stream

        @wraps(func)
        async def call(*args, **kwargs):
            bound, entry, meta = prepare(args, kwargs)
            # The Codex sentinel logs at its own boundary, also used directly.
            if (bound.arguments.get("scfg") or {}).get("provider") == "codex":
                return await func(*bound.args, **bound.kwargs)
            status, error = "success", ""
            token = _active_call.set(entry)
            try:
                await _publish(entry)
                result = await func(*bound.args, **bound.kwargs)
                if result is None:
                    status, error = "error", "模型未返回结果"
                return result
            except asyncio.CancelledError:
                status, error = "cancelled", "调用已中断"
                raise
            except Exception as exc:
                status, error = "error", f"{type(exc).__name__}: {exc}"
                raise
            finally:
                _active_call.reset(token)
                await finish(entry, meta, status, error)
        return call
    return decorate
