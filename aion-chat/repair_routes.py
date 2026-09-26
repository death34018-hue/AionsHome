"""Self-contained repair UI/API with explicit pairing and plan acceptance."""
from __future__ import annotations

import hashlib
import asyncio
import hmac
import json
import os
import re
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Literal

from repair_store import default_store, Conflict, uid
from repair_files import export_file, file_record, MAX_FILE_BYTES
from repair_summary import summarize_repair


router = APIRouter(tags=["repair-room"])
COOKIE = "aion_repair_device"
_accept_locks = {}


def is_local(request):
    return (request.client and request.client.host in {"127.0.0.1", "::1"}
            and request.url.hostname in {"localhost", "127.0.0.1", "::1"}
            and not any(h in request.headers for h in ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "forwarded")))


def pairing_code(store):
    path = store.directory / "pairing-code.txt"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(8))
    except FileExistsError:
        pass
    return path.read_text(encoding="utf-8").strip()


def cookie_value(store):
    return hmac.new(pairing_code(store).encode(), b"repair-device-v1", hashlib.sha256).hexdigest()


def authorized(request, store):
    return is_local(request) or hmac.compare_digest(request.cookies.get(COOKIE, ""), cookie_value(store))


def mutation_guard(request):
    if request.method not in {"GET", "HEAD"}:
        origin = request.headers.get("origin")
        if (request.headers.get("x-repair-request") != "1"
                or (origin and urlsplit(origin).netloc != request.headers.get("host"))):
            raise HTTPException(403, "请求来源不匹配，请从维修室页面操作。")


def access(request: Request):
    mutation_guard(request)
    store = default_store()
    if not authorized(request, store):
        raise HTTPException(401, "请先在本设备配对维修室。")
    return store


def checked_task(store, task_id):
    try:
        return store.task(task_id)
    except KeyError:
        raise HTTPException(404, "任务不存在。")


def work_model(store, active=None):
    current = json.loads(store.runtime("active_model") or "{}")
    if active and current.get("job") == active["id"] and current.get("name"):
        return {"name": current["name"], "running": True}
    from repair_codex import model_name
    try:
        return {"name": model_name(store), "running": False}
    except RuntimeError:
        return {"name": "", "running": False}


@router.get("/repair")
async def repair_page():
    return FileResponse(Path(__file__).parent / "static" / "repair.html", headers={"Cache-Control": "no-store"})


@router.get("/api/repair/health")
async def health():
    return {"ok": True}


@router.get("/api/repair/model")
async def get_model(store=Depends(access)):
    from config import MODELS
    from repair_codex import DEFAULT_WORK_MODEL, model_name
    name = model_name(store)
    options = list(dict.fromkeys([DEFAULT_WORK_MODEL, name] + [
        cfg['model'] for cfg in MODELS.values() if cfg.get('provider') == 'codex_cli'
    ]))
    return {"name": name, "options": options}


class ModelBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)


@router.put("/api/repair/model")
async def set_model(body: ModelBody, store=Depends(access)):
    name = body.name.strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', name):
        raise HTTPException(400, "请填写有效的模型名称，例如 gpt-6-sol。")
    store.runtime('model_name', name)
    return {"name": name}


@router.get("/api/repair/bootstrap")
async def bootstrap(request: Request):
    from chatroom import get_chatroom_names
    store = default_store()
    ok = authorized(request, store)
    user, aion, connor = get_chatroom_names() if ok else ("用户", "AI", "第二AI")
    data = {"authorized": ok, "names": {"user": user, "aion": aion, "connor": connor, "system": "任务记录"}}
    if is_local(request):
        data["pairing_code"] = pairing_code(store)
    if ok:
        import psutil
        data["work_model"] = work_model(store)
        store.runtime("home_process", json.dumps({"pid": os.getpid(), "created": psutil.Process().create_time()}))
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


class PairBody(BaseModel):
    code: str = Field(min_length=1, max_length=100)


@router.post("/api/repair/pair")
async def pair(request: Request, body: PairBody):
    mutation_guard(request)
    store = default_store()
    if not hmac.compare_digest(body.code.strip(), pairing_code(store)):
        raise HTTPException(403, "配对码不正确。请在电脑本机打开维修室查看。")
    response = JSONResponse({"ok": True})
    response.set_cookie(COOKIE, cookie_value(store), max_age=365 * 24 * 3600, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", path="/api/repair")
    return response


class NewTask(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    context_minutes: int = Field(default=120, ge=0, le=1440)


@router.get("/api/repair/tasks")
async def tasks(store=Depends(access)):
    return {"tasks": store.tasks(), "worker_online": time.time() - float(store.runtime("heartbeat") or 0) < 15}


@router.post("/api/repair/tasks")
async def create(body: NewTask, store=Depends(access)):
    if not body.title.strip():
        raise HTTPException(400, "请填写任务名称。")
    return store.create(body.title.strip(), body.context_minutes)


@router.get("/api/repair/tasks/{task_id}")
async def task(task_id: str, after: int = 0, message_after: int = 0, store=Depends(access)):
    task = checked_task(store, task_id)
    active = store.active(task_id)
    if active:
        from repair_worker import ensure_worker
        ensure_worker(store)
    return {"task": task, "messages": store.messages(task_id, max(0, message_after)), "events": store.events(task_id, max(0, after)),
            "active": active, "work_model": work_model(store, active),
            "worker_online": time.time() - float(store.runtime("heartbeat") or 0) < 15}


class SendBody(BaseModel):
    text: str = Field(default="", max_length=16000)
    recipient: Literal["both", "aion", "connor"] = "connor"
    kind: Literal["discuss", "plan", "inspect"] = "discuss"
    attachments: list[str] = Field(default_factory=list, max_length=4)
    revision: int | None = None


class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=100)


@router.patch('/api/repair/tasks/{task_id}')
async def rename(task_id: str, body: RenameBody, store=Depends(access)):
    checked_task(store, task_id)
    title = body.title.strip()
    if not title:
        raise HTTPException(400, '任务名称不能为空。')
    return store.rename(task_id, title)


def launch(store, task_id, kind, payload):
    from repair_worker import ensure_worker
    checked_task(store, task_id)
    try:
        job_id = store.enqueue(task_id, kind, payload)
    except Conflict as exc:
        raise HTTPException(409, str(exc))
    ensure_worker(store)
    return {"job_id": job_id}


@router.post("/api/repair/tasks/{task_id}/send")
async def send(task_id: str, body: SendBody, store=Depends(access)):
    from repair_dialogue import is_confirmation
    current = checked_task(store, task_id)
    if (body.kind == 'discuss' and body.recipient in {'both', 'connor'} and not body.attachments
            and is_confirmation(body.text) and current['proposal_valid']):
        if body.revision != current['revision']:
            raise HTTPException(409, '方案已更新，请先看完最新回复，再确认开始。')
        return launch(store, task_id, 'execute', {'revision': body.revision, 'text': body.text.strip()})
    text = body.text.strip()
    file_ids = body.attachments
    if body.kind == 'inspect' and not text and not file_ids:
        latest = next((m for m in reversed(store.messages(task_id)) if m['who'] == 'user'), None)
        if not latest or (is_confirmation(latest['text']) and not latest['attachments']):
            raise HTTPException(400, '请先说一下要做什么或取哪个文件，再点「继续处理 / 取文件」。')
        text = latest['text']
        file_ids = [a['id'] for a in latest['attachments']]
    attachments = []
    for file_id in file_ids:
        try:
            record = file_record(store, file_id, task_id)
        except (KeyError, ValueError, OSError):
            raise HTTPException(400, "附件不属于当前任务或已不存在。")
        attachments.append({"id": file_id, "name": record["name"], "size": record["size"], "url": f"/api/repair/files/{file_id}"})
    if not text and not attachments and body.kind != "plan":
        raise HTTPException(400, "请先输入要求。")
    return launch(store, task_id, body.kind, {"text": text, "recipient": body.recipient, "attachments": attachments})


class PlanBody(BaseModel):
    plan: str = Field(min_length=1, max_length=24000)
    revision: int


@router.post("/api/repair/tasks/{task_id}/plan")
async def plan(task_id: str, body: PlanBody, store=Depends(access)):
    checked_task(store, task_id)
    if not body.plan.strip():
        raise HTTPException(400, "方案不能为空。")
    try:
        return store.save_plan(task_id, body.plan.strip(), body.revision)
    except Conflict as exc:
        raise HTTPException(409, str(exc))


class ExecuteBody(BaseModel):
    revision: int


@router.post("/api/repair/tasks/{task_id}/execute")
async def execute(task_id: str, body: ExecuteBody, store=Depends(access)):
    return launch(store, task_id, "execute", {"revision": body.revision})


@router.post("/api/repair/tasks/{task_id}/stop")
async def stop(task_id: str, store=Depends(access)):
    checked_task(store, task_id)
    store.stop(task_id)
    return {"ok": True}


class SteerBody(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


@router.post("/api/repair/tasks/{task_id}/steer")
async def steer(task_id: str, body: SteerBody, store=Depends(access)):
    try:
        store.steer(task_id, body.text)
    except Conflict as exc:
        raise HTTPException(409, str(exc))
    return {"queued": True}


@router.post("/api/repair/tasks/{task_id}/upload")
async def upload(task_id: str, file: UploadFile = File(...), store=Depends(access)):
    checked_task(store, task_id)
    folder = store.directory / "incoming" / uid()
    folder.mkdir(parents=True)
    # User-controlled names cannot introduce directories or alternate NTFS streams.
    name = Path((file.filename or "attachment").replace("\\", "/")).name.replace(":", "_")
    if name in {"", ".", ".."}:
        name = "attachment"
    path = folder / name
    try:
        size = 0
        with path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise HTTPException(413, "附件上限为 50 MB。")
                handle.write(chunk)
        return export_file(store, task_id, path)
    finally:
        path.unlink(missing_ok=True)
        folder.rmdir()


@router.get("/api/repair/files/{file_id}")
async def download(file_id: str, preview: bool = False, store=Depends(access)):
    try:
        record = file_record(store, file_id)
    except (KeyError, ValueError, OSError):
        raise HTTPException(404, "附件不存在。")
    import mimetypes
    mime = mimetypes.guess_type(record["name"])[0] or "application/octet-stream"
    inline = preview and mime in {"image/png", "image/jpeg", "image/webp", "image/gif"}
    return FileResponse(record["path"], filename=record["name"], media_type=mime,
                        content_disposition_type="inline" if inline else "attachment",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/api/repair/tasks/{task_id}/accept")
async def accept(task_id: str, store=Depends(access)):
    lock = _accept_locks.setdefault((str(store.path), task_id), asyncio.Lock())
    async with lock:
        return await _accept_summary(task_id, store)


async def _accept_summary(task_id, store):
    from config import DB_PATH
    from chatroom import get_chatroom_names
    from ws import manager
    snapshot = checked_task(store, task_id)
    user, _, connor = get_chatroom_names()
    report_id = "repair_completed_" + task_id
    if snapshot['phase'] == 'completed':
        return {'ok': True, 'message_id': report_id}
    if snapshot['phase'] != 'review' or store.active(task_id):
        raise HTTPException(409, '任务尚未进入待验收，不能标记完成。')
    with store.connect() as db:
        db.execute('ATTACH DATABASE ? AS home', (str(DB_PATH),))
        room = db.execute("SELECT id FROM home.chatroom_rooms WHERE type='group' ORDER BY updated_at DESC LIMIT 1").fetchone()
    if not room:
        raise HTTPException(409, '请先在聊天室创建一个群聊，再回传成果。')
    room_id = room[0]
    try:
        summary = await summarize_repair(store, snapshot)
    except Exception as exc:
        raise HTTPException(503, '成果摘要暂时没整理好，尚未同步到群聊，请稍后重试。') from exc
    with store.connect() as db:
        db.execute("ATTACH DATABASE ? AS home", (str(DB_PATH),))
        db.execute("BEGIN IMMEDIATE")
        task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        if task["phase"] == "completed":
            return {"ok": True, "message_id": report_id}
        if task["phase"] != "review" or db.execute("SELECT 1 FROM jobs WHERE task_id=? AND state IN ('queued','running')", (task_id,)).fetchone():
            raise HTTPException(409, "任务尚未进入待验收，不能标记完成。")
        if task['updated'] != snapshot['updated'] or task['revision'] != snapshot['revision']:
            raise HTTPException(409, '整理摘要时任务有了新内容，请检查后重新同步。')
        if not db.execute("SELECT 1 FROM home.chatroom_rooms WHERE id=? AND type='group'", (room_id,)).fetchone():
            raise HTTPException(409, "请先在聊天室创建一个群聊，再回传成果。")
        text = (f"🛠️ 维修室 · {task['title']}\n执行者：{connor}；{user}已验收通过。\n"
                f"{summary}")
        attachments = [{"type": "system_model_context"}, {"type": "repair_result", "task_id": task_id, 'summary': summary}]
        now = time.time()
        db.execute("INSERT OR IGNORE INTO home.chatroom_messages(id,room_id,sender,content,created_at,attachments) VALUES(?,?,?,?,?,?)",
                   (report_id, room_id, "system", text, now, json.dumps(attachments, ensure_ascii=False)))
        db.execute("UPDATE home.chatroom_rooms SET updated_at=? WHERE id=?", (now, room_id))
        db.execute("UPDATE tasks SET phase='completed',updated=? WHERE id=?", (now, task_id))
    from sync_events import broadcast_synced
    from chatroom import connor_1v1_on_message
    await broadcast_synced(manager, {"type": "chatroom_msg_created", "data": {"id": report_id, "room_id": room_id, "sender": "system", "content": text, "created_at": now, "attachments": attachments}})
    connor_1v1_on_message()
    store.message(task_id, "system", "已验收通过，成果已回传群聊并进入日常可读取的时间线。")
    return {"ok": True, "message_id": report_id}
