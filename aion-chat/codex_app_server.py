"""Request-scoped Codex App Server client over newline-delimited stdio JSON-RPC."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable


_TEXT_BATCH_CHARS = 24
_TEXT_BATCH_SECONDS = 0.05


class CodexAppServerError(RuntimeError):
    """A bounded, user-safe App Server transport or turn failure."""


@dataclass(frozen=True)
class CodexAppServerEvent:
    kind: str
    text: str = ""
    usage: dict | None = None


def build_codex_app_server_command(
    node: str,
    script: str,
    overrides: list[str] | tuple[str, ...] = (),
) -> list[str]:
    command = [node, script]
    for override in overrides:
        command.extend(["-c", override])
    command.extend(["app-server", "--stdio"])
    return command


async def _write_json(stdin, message: dict) -> None:
    stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    await stdin.drain()


async def _read_json_line(stdout) -> dict:
    while True:
        raw = await stdout.readline()
        if not raw:
            raise EOFError("Codex App Server closed its output stream")
        try:
            message = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            return message


async def _request(
    process,
    request_id: int,
    method: str,
    params: dict,
    pending: list[dict],
) -> dict:
    await _write_json(
        process.stdin,
        {"method": method, "id": request_id, "params": params},
    )
    while True:
        message = await _read_json_line(process.stdout)
        if message.get("id") != request_id:
            pending.append(message)
            continue
        error = message.get("error")
        if error:
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise CodexAppServerError(f"{method} failed: {detail}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}


async def _capture_stderr(stderr, limit: int = 64 * 1024) -> str:
    collected = bytearray()
    while len(collected) < limit:
        chunk = await stderr.read(min(8192, limit - len(collected)))
        if not chunk:
            break
        collected.extend(chunk)
    return collected.decode("utf-8", errors="replace").strip()


async def _close_process(process, *, interrupt_turn: tuple[str, str] | None) -> None:
    if interrupt_turn and process.returncode is None:
        thread_id, turn_id = interrupt_turn
        with contextlib.suppress(Exception):
            await _write_json(
                process.stdin,
                {
                    "method": "turn/interrupt",
                    "id": 99_999,
                    "params": {"threadId": thread_id, "turnId": turn_id},
                },
            )

    if getattr(process, "stdin", None) is not None:
        with contextlib.suppress(Exception):
            process.stdin.close()
            wait_closed = getattr(process.stdin, "wait_closed", None)
            if wait_closed is not None:
                await wait_closed()

    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=1.5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1.5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
    else:
        with contextlib.suppress(Exception):
            await process.wait()


def _final_agent_text(message: dict) -> str:
    if message.get("method") != "item/completed":
        return ""
    item = (message.get("params") or {}).get("item") or {}
    if item.get("type") != "agentMessage":
        return ""
    return str(item.get("text") or "")


async def stream_codex_app_server(
    command: list[str],
    *,
    env: dict,
    cwd: str,
    model: str,
    prompt: str,
    image_paths: list[str] | tuple[str, ...],
    reasoning_summary: str,
    spawn: Callable[..., Awaitable[object]] | None = None,
) -> AsyncIterator[CodexAppServerEvent]:
    """Start one ephemeral App Server turn and stream normalized events."""

    spawn_process = spawn or asyncio.create_subprocess_exec
    process = await spawn_process(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        limit=8 * 1024 * 1024,
    )
    stderr_task = asyncio.create_task(_capture_stderr(process.stderr))
    pending: list[dict] = []
    thread_id = ""
    turn_id = ""
    turn_completed = False
    assembled_text = ""
    text_buffer = ""
    last_text_flush = time.monotonic()
    final_text = ""
    reasoning_started = False

    try:
        await _request(
            process,
            1,
            "initialize",
            {
                "clientInfo": {
                    "name": "aionshome_chat",
                    "title": "AionsHome Chat",
                    "version": "1.0.0",
                }
            },
            pending,
        )
        await _write_json(process.stdin, {"method": "initialized", "params": {}})

        thread_result = await _request(
            process,
            2,
            "thread/start",
            {
                "model": model,
                "cwd": cwd,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
            },
            pending,
        )
        thread_id = str((thread_result.get("thread") or {}).get("id") or "")
        if not thread_id:
            raise CodexAppServerError("thread/start returned no thread id")

        inputs = [{"type": "text", "text": prompt}]
        inputs.extend(
            {"type": "localImage", "path": str(path)}
            for path in image_paths
        )
        turn_result = await _request(
            process,
            3,
            "turn/start",
            {
                "threadId": thread_id,
                "input": inputs,
                "summary": reasoning_summary,
            },
            pending,
        )
        turn_id = str((turn_result.get("turn") or {}).get("id") or "")
        if not turn_id:
            raise CodexAppServerError("turn/start returned no turn id")

        while True:
            message = pending.pop(0) if pending else await _read_json_line(process.stdout)
            method = str(message.get("method") or "")
            params = message.get("params") or {}

            if method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                if delta:
                    assembled_text += delta
                    text_buffer += delta
                    now = time.monotonic()
                    if (
                        len(text_buffer) >= _TEXT_BATCH_CHARS
                        or now - last_text_flush >= _TEXT_BATCH_SECONDS
                    ):
                        yield CodexAppServerEvent("text_delta", text=text_buffer)
                        text_buffer = ""
                        last_text_flush = now
                    else:
                        # Keep the idle watchdog alive while tiny fragments are
                        # coalesced into fewer downstream UI/TTS updates.
                        yield CodexAppServerEvent("activity")
                continue

            if method == "item/reasoning/summaryPartAdded":
                if reasoning_started:
                    yield CodexAppServerEvent("reasoning_delta", text="\n\n")
                yield CodexAppServerEvent("activity")
                continue

            if method == "item/reasoning/summaryTextDelta":
                delta = str(params.get("delta") or "")
                if delta:
                    reasoning_started = True
                    yield CodexAppServerEvent("reasoning_delta", text=delta)
                continue

            if method == "thread/tokenUsage/updated":
                token_usage = params.get("tokenUsage") or {}
                usage = token_usage.get("total") or token_usage
                if isinstance(usage, dict):
                    yield CodexAppServerEvent("usage", usage=usage)
                yield CodexAppServerEvent("activity")
                continue

            completed_text = _final_agent_text(message)
            if completed_text:
                final_text = completed_text

            if method == "error":
                error = params.get("error") or params
                detail = error.get("message") if isinstance(error, dict) else str(error)
                raise CodexAppServerError(detail or "Codex App Server error")

            if method == "turn/completed":
                turn = params.get("turn") or {}
                status = str(turn.get("status") or "")
                if status != "completed":
                    error = turn.get("error") or {}
                    detail = error.get("message") if isinstance(error, dict) else str(error)
                    raise CodexAppServerError(detail or f"Codex turn {status or 'failed'}")
                if final_text.startswith(assembled_text):
                    suffix = final_text[len(assembled_text):]
                    if suffix:
                        assembled_text += suffix
                        text_buffer += suffix
                if text_buffer:
                    yield CodexAppServerEvent("text_delta", text=text_buffer)
                    text_buffer = ""
                turn_completed = True
                yield CodexAppServerEvent("completed")
                break

            if method:
                yield CodexAppServerEvent("activity")

    except EOFError as error:
        detail = ""
        if stderr_task.done():
            with contextlib.suppress(Exception):
                detail = stderr_task.result()
        raise CodexAppServerError(detail or str(error)) from error
    finally:
        await _close_process(
            process,
            interrupt_turn=(thread_id, turn_id) if turn_id and not turn_completed else None,
        )
        if not stderr_task.done():
            stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stderr_task
