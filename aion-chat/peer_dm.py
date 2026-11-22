"""One shared, observer-only room for private conversations between the two actors."""
import asyncio
import contextvars
import hashlib
import logging
import re
import time

from database import get_db
from model_json import extract_json_object


ROOM_ID = "cr_peer_dm"
MAX_MESSAGES = 16
PEER_DM_HINT = "[伙伴私聊：内容] — 单独发给另一位 AI 伴侣。把私聊正文只写在指令里。"
_COMMAND_START = re.compile(r"\[伙伴私聊\s*([：:])?")
_tasks = {}
_turn_lock = asyncio.Lock()
log = logging.getLogger(__name__)


def extract_peer_dm(text: str, *, strip: bool = True) -> tuple[str, str]:
    # An unfinished tag is hidden too, but only a complete message is delivered.
    public, bodies = [], []
    pos = 0
    while match := _COMMAND_START.search(text, pos):
        public.append(text[pos:match.start()])
        depth = 1
        pos = match.end()
        while depth and pos < len(text):
            depth += (text[pos] == "[") - (text[pos] == "]")
            pos += 1
        if not depth and match[1]:
            body = text[match.end():pos - 1].strip()
            if body:
                bodies.append(body)
    public.append(text[pos:])
    visible = "".join(public)
    return visible.strip() if strip else visible, "\n".join(bodies)


async def init_storage(db):
    await db.execute("""
        CREATE TABLE IF NOT EXISTS peer_dm_exchanges (
            id TEXT PRIMARY KEY, message_count INTEGER NOT NULL, next_actor TEXT, opener TEXT
        )
    """)
    await db.execute(
        "INSERT OR IGNORE INTO chatroom_rooms (id,title,type,created_at,updated_at) VALUES (?,?,?,0,0)",
        (ROOM_ID, "他们俩", "peer_dm"),
    )


async def _insert_message(db, exchange, position, actor, body):
    now = time.time()
    await db.execute(
        "INSERT INTO chatroom_messages (id,room_id,sender,content,created_at) VALUES (?,?,?,?,?)",
        (f"peer_{exchange}_{position:02d}", ROOM_ID, actor, body, now),
    )
    await db.execute("UPDATE chatroom_rooms SET updated_at=? WHERE id=?", (now, ROOM_ID))


async def _notify():
    from chatroom import connor_1v1_on_message
    from ws import manager
    connor_1v1_on_message()
    # Ordinary chatroom_msg_created also drives phone notifications, TTS and mirrors.
    await manager.broadcast({"type": "peer_dm_updated", "data": {"room_id": ROOM_ID}})


def _other(actor):
    return "connor" if actor == "aion" else "aion"


async def _next_reply(actor, transcript):
    from autonomy import _actor_context, _collect
    from chatroom import get_chatroom_names, recall_chatroom_memories, recall_main_chat_memories
    from ai_providers import stream_ai
    from context_builder import fetch_merged_timeline, render_merged_timeline
    from routes.chatroom import _resolve_aion_model, _stream_connor_model

    user_name, ai_name, companion_name = get_chatroom_names()
    names = {"aion": ai_name, "connor": companion_name}
    messages = await _actor_context(actor, include_history=False)
    recent = await fetch_merged_timeline(actor, 30)
    current_ids = {row[0] for row in transcript}
    other_history = [m for m in recent if m["id"] not in current_ids]
    messages.extend(render_merged_timeline(other_history, actor, include_image_attachments=False))
    query = transcript[-1][2]
    memories = (await recall_main_chat_memories(query) if actor == "aion"
                else await recall_chatroom_memories(query, ROOM_ID, "connor", top_k=3))
    if memories:
        messages.append({"role": "user", "content": "[相关记忆]\n" + "\n".join(m["content"] for m in memories)})
    conversation = "\n".join(f"{names[sender]}：{body}" for _, sender, body in transcript)
    messages.append({"role": "user", "content": (
        f"你是{names[actor]}，正与{names[_other(actor)]}私聊，{user_name}没有参与。\n"
        f"[本次私聊]\n{conversation}\n\n"
        "轮到你决定是否接话。觉得话题结束了，就主动结束这一轮私聊。"
        "可以说最后一句并结束，也可以把正文留空直接结束。还有话想让对方接时才继续。"
        '只返回 JSON，例如 {"message":"你对对方说的话","end":false}。结束时将 end 设为 true。'
        + ("这是最后一个发言位置，请收尾。" if len(transcript) == MAX_MESSAGES - 1 else "")
    )})
    meta = {"_room_log": {"room_id": ROOM_ID, "actor": actor}}
    source = (_stream_connor_model(messages, meta=meta) if actor == "connor"
              else stream_ai(messages, _resolve_aion_model(), meta))
    result = extract_json_object(await _collect(source)) or {}
    body = result.get("message")
    if not isinstance(body, str):
        return "", True
    return body.strip(), result.get("end") is not False


async def _continue(exchange):
    async with _turn_lock:
        while True:
            async with get_db() as db:
                row = await (await db.execute(
                    "SELECT message_count,next_actor,opener FROM peer_dm_exchanges WHERE id=?", (exchange,),
                )).fetchone()
                if not row or not row[1] or row[0] >= MAX_MESSAGES:
                    return
                count, actor, opener = row
                if count:
                    transcript = await db.execute_fetchall(
                        "SELECT id,sender,content FROM chatroom_messages WHERE room_id=? AND id GLOB ? ORDER BY id",
                        (ROOM_ID, f"peer_{exchange}_*"),
                    )
            if count == 0:
                body, end = opener, False
            else:
                try:
                    async with asyncio.timeout(900):
                        body, end = await _next_reply(actor, transcript)
                except Exception as exc:
                    log.warning("Peer conversation stopped after model failure (%s)", type(exc).__name__)
                    body, end = "", True
            position = count + bool(body)
            next_actor = None if end or not body or position >= MAX_MESSAGES else _other(actor)
            async with get_db() as db:
                updated = await db.execute(
                    "UPDATE peer_dm_exchanges SET message_count=?,next_actor=?,opener=NULL "
                    "WHERE id=? AND message_count=? AND next_actor=?",
                    (position, next_actor, exchange, count, actor),
                )
                if not updated.rowcount:
                    return
                if body:
                    await _insert_message(db, exchange, position, actor, body)
                await db.commit()
            if body:
                await _notify()
            if not next_actor:
                return


def _schedule(exchange):
    if exchange in _tasks:
        return
    task = asyncio.create_task(_continue(exchange), context=contextvars.Context())
    _tasks[exchange] = task

    def finished(done):
        _tasks.pop(exchange, None)
        if not done.cancelled() and done.exception():
            log.error("Peer conversation interrupted (%s)", type(done.exception()).__name__)

    task.add_done_callback(finished)


async def start_peer_dm(actor: str, content: str, *, trigger_id: str) -> bool:
    if actor not in ("aion", "connor") or not content.strip() or not trigger_id:
        return False
    exchange = hashlib.sha256(f"{actor}:{trigger_id}".encode()).hexdigest()
    async with get_db() as db:
        inserted = await db.execute(
            "INSERT OR IGNORE INTO peer_dm_exchanges (id,message_count,next_actor,opener) VALUES (?,0,?,?)",
            (exchange, actor, content.strip()),
        )
        await db.commit()
    _schedule(exchange)
    return bool(inserted.rowcount)


async def resume_pending():
    from chatroom import connor_1v1_on_message
    async with get_db() as db:
        pending = await db.execute_fetchall("SELECT id FROM peer_dm_exchanges WHERE next_actor IS NOT NULL ORDER BY rowid")
        undigested = await (await db.execute(
            "SELECT 1 FROM chatroom_messages WHERE room_id=? AND created_at > COALESCE("
            "(SELECT anchor_ts FROM chatroom_digest_anchors WHERE room_id='connor_unified'),0) LIMIT 1",
            (ROOM_ID,),
        )).fetchone()
    if undigested:
        connor_1v1_on_message()
    for (exchange,) in pending:
        _schedule(exchange)


async def wait_for_replies():
    await asyncio.gather(*list(_tasks.values()), return_exceptions=False)


async def stop():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
