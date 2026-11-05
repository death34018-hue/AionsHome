"""Independent, single-instance repair worker; survives the home web server."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from repair_store import default_store

IDLE_EXIT_SECONDS = 60
HEARTBEAT_SECONDS = 5


def ensure_worker(store):
    if time.time() - float(store.runtime("heartbeat") or 0) < 15:
        return
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    from repair_codex import work_environment
    with (store.directory / "worker.log").open("ab") as log:
        subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve())],
                         cwd=Path(__file__).parent, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=flags, start_new_session=os.name != "nt", close_fds=True,
                         env=work_environment())


async def restart_home(store, task_id):
    import httpx
    import psutil
    value = store.runtime("home_process")
    if not value:
        raise RuntimeError("还没有主服务进程信息，请从小家打开维修室后再试。")
    info = json.loads(value)
    process = psutil.Process(info["pid"])
    base = Path(__file__).parent.resolve()
    args = process.cmdline()
    if abs(process.create_time() - info["created"]) > 0.01 or Path(process.cwd()).resolve() != base:
        raise RuntimeError("主服务进程已变化，已取消重启。请刷新页面后重试。")
    if "--reload" in args or not any(Path(a).name == "main.py" for a in args):
        raise RuntimeError("当前启动方式不支持自动重启，请在电脑端重启。")
    store.event(task_id, "status", {"text": "正在重启小家，页面会暂时断开；维修记录会保留。"})
    process.terminate()
    await asyncio.to_thread(process.wait, 15)
    launched = None
    if "--scheduled" not in args:
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        from repair_codex import work_environment
        with (store.directory / "home-restart.log").open("ab") as log:
            launched = subprocess.Popen([sys.executable, "-u", str(base / "main.py")], cwd=base,
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        creationflags=flags, start_new_session=os.name != "nt", close_fds=True,
                                        env=work_environment())
        store.runtime("home_process", json.dumps({"pid": launched.pid, "created": psutil.Process(launched.pid).create_time()}))
    async with httpx.AsyncClient(timeout=3) as client:
        for _ in range(45):
            if launched is not None and launched.poll() is not None:
                raise RuntimeError("小家启动失败，详见维修目录 home-restart.log；需要电脑端检查。")
            await asyncio.sleep(2)
            try:
                response = await client.get("http://127.0.0.1:8080/api/repair/health")
                if response.status_code == 200:
                    store.event(task_id, "status", {"text": "小家已恢复连接。"})
                    return {"restarted": True, "health": "ok"}
            except httpx.HTTPError:
                pass
    raise RuntimeError("启动检查超过 90 秒，尚未确认小家恢复。")


AION_DISCUSSION_TIMEOUT = 45


async def reply_aion(store, job):
    from config import DEFAULT_MODEL
    from chatroom import load_chatroom_config
    from ai_providers import stream_ai, CLI_STATUS_PREFIX
    from repair_context import build_context

    task_id, actor = job['task_id'], 'aion'
    instructions, context = await build_context(store, task_id, actor)
    extra = "\n当前由你参与讨论，无本机工具。提出你的分析和建议，不要冒充执行者或声称查过文件；发给大家时执行者会在你之后自行调查，不要让用户再按按钮。"
    messages = [{"role": "system", "content": instructions}, {"role": "user", "content": context + extra}]
    from repair_files import file_record
    image_parts = []
    for msg in store.messages(task_id)[-6:]:
        for attachment in msg["attachments"]:
            suffix = Path(attachment["name"]).suffix.lower()
            if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
                import base64
                record = file_record(store, attachment["id"], task_id)
                mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/" + suffix[1:]
                encoded = base64.b64encode(Path(record["path"]).read_bytes()).decode()
                image_parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    if image_parts:
        messages[-1]["content"] = [{"type": "text", "text": context + extra}, *image_parts[-4:]]
    cfg = load_chatroom_config()
    key = cfg.get("aion_model") or DEFAULT_MODEL
    result = ""
    meta = {}
    stream_id = job["id"] + actor
    store.event(task_id, "status", {"who": actor, "text": "正在整理方案" if job["kind"] == "plan" else "正在回复"})
    try:
        async for chunk in stream_ai(messages, key, meta, include_device_context=False):
            if chunk.startswith(CLI_STATUS_PREFIX):
                continue
            result += chunk
            store.event(task_id, "text", {"who": actor, "delta": chunk, "item": stream_id})
        if not result.strip() or result.lstrip().startswith(("[错误]", "[CodexCLI错误]", "[API错误]")):
            raise RuntimeError(result or "模型没有返回内容。")
    except BaseException:
        if result:
            store.message(task_id, actor, result + "\n\n（本次回复中断，尚未完成。）")
        raise
    store.message(task_id, actor, result)
    if meta.get("reasoning_content"):
        store.event(task_id, "reasoning", {"delta": str(meta["reasoning_content"]), "item": stream_id})


async def discuss(store, job):
    from chatroom import get_chatroom_names
    from repair_context import build_context
    from repair_codex import run_codex

    task_id = job['task_id']
    recipient = job['payload'].get('recipient', 'connor')
    actors = ['aion', 'connor'] if recipient == 'both' else [recipient]
    if job['kind'] == 'plan':
        actors = ['connor']
    _, aion_name, connor_name = get_chatroom_names()
    for actor in actors:
        if store.cancelled(job['id']):
            raise asyncio.CancelledError()
        if actor == 'aion':
            store.event(task_id, 'status', {'who': actor, 'text': f'正在连接讨论线路，最多等待 {AION_DISCUSSION_TIMEOUT} 秒。'})
            try:
                async with asyncio.timeout(AION_DISCUSSION_TIMEOUT):
                    await reply_aion(store, job)
            except Exception as exc:
                reason = '回复等待超时' if isinstance(exc, TimeoutError) else '讨论线路连接或回复失败'
                detail = f'{type(exc).__name__}: {exc}'
                if recipient != 'both':
                    raise RuntimeError(f'{aion_name}{reason}，请稍后重试；需要查文件也可以直接发给{connor_name}。原错误：{detail}') from exc
                notice = f'{aion_name}{reason}，本轮未完成他的讨论回复。{connor_name}会继续处理你的要求。'
                store.message(task_id, 'system', notice)
                store.event(task_id, 'status', {'text': notice, 'detail': detail})
            finally:
                store.event(task_id, 'message_done', {'item': job['id'] + actor})
        else:
            store.event(task_id, 'status', {'who': actor, 'text': '正在连接工作会话，接下来会显示调查或回复进度。'})
            instructions, context = await build_context(store, task_id, actor)
            await run_codex(store, job, instructions, context)


async def handle(store, job):
    if job["kind"] in ("discuss", "plan"):
        await discuss(store, job)
    else:
        from repair_context import build_context
        from repair_codex import run_codex
        instructions, context = await build_context(store, job["task_id"], "connor")
        await run_codex(store, job, instructions, context)


async def run(store):
    store.recover()
    idle_since = time.monotonic()
    last_heartbeat = -float('inf')
    def heartbeat():
        nonlocal last_heartbeat
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            store.runtime('heartbeat', time.time())
            last_heartbeat = now
    while True:
        heartbeat()
        job = store.claim()
        if not job:
            if time.monotonic() - idle_since >= IDLE_EXIT_SECONDS:
                return
            await asyncio.sleep(1)
            continue
        work = asyncio.create_task(handle(store, job))
        state = "done"
        start = time.monotonic()
        try:
            while not work.done():
                heartbeat()
                if store.cancelled(job["id"]) or time.monotonic() - start > 3600:
                    work.cancel()
                    break
                await asyncio.wait({work}, timeout=0.4)
            await work
        except asyncio.CancelledError:
            state = "interrupted"
            store.event(job["task_id"], "status", {"text": "已停止；已发生的修改没有自动撤销。"})
        except Exception as exc:
            state = "failed"
            store.event(job["task_id"], "error", {"text": str(exc)})
            store.message(job["task_id"], "system", "本轮未完成：" + str(exc))
        finally:
            store.finish(job, state)
            store.event(job["task_id"], "status", {"text": "本轮结束，等待验收。" if state == "done" and job["kind"] == "execute" else "本轮已结束。"})
            idle_since = time.monotonic()


def main():
    store = default_store()
    lock = (store.directory / "worker.lock").open("a+b")
    lock.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return
    idle_exit = False
    try:
        asyncio.run(run(store))
        idle_exit = True
    finally:
        store.runtime('heartbeat', 0)
        lock.close()
    if idle_exit:
        # A submission may arrive between the final empty claim and releasing
        # the process lock. Recheck after release so it cannot be stranded.
        with store.connect() as db:
            queued = db.execute("SELECT 1 FROM jobs WHERE state='queued' LIMIT 1").fetchone()
        if queued:
            ensure_worker(store)


if __name__ == "__main__":
    main()
