"""Saved Strudel scores and the small chat cards that accompany them."""

import hashlib
import json
import math
import re
import time

import aiosqlite

from database import get_db
from model_json import extract_json_object
from sync_events import append_sync_event, broadcast_synced
from ws import manager


COMPOSE_CMD_PATTERN = re.compile(r'\[COMPOSE\]([\s\S]*?)(?:\[/COMPOSE\]|$)', re.I)

COMPOSE_PROMPT = (
    '你可以写Strudel器乐小曲。用户点曲时创作，也可在合适情境偶尔主动赠曲，每次最多一首。'
    '输出[COMPOSE]块，内部JSON为{"title":"曲名","bpm":80,"cycles":8,"code":"Strudel代码"}，'
    '块外只写简短介绍。默认60—100BPM、2—4轨、8—16小节，安排旋律、和声、长短音、停顿及强弱，'
    '用重复与少量变化组织乐句。支持setcpm(BPM/4)、note、s、stack、arrange、slow、gain、clip、room。'
    '迷你谱中空格顺序演奏，[e4 g4]均分一份时值，a4@2延长，~休止，逗号同时发声，<…>每轮切换。'
    '乐器用piano、guitar、flute、bass、marimba、bd、sd、hh，也可用sine、triangle、sawtooth。'
    '音量适中，代码只含乐曲，系统已提供音色，结尾用[/COMPOSE]。'
    '示例：setcpm(80/4);stack(note("<[e4@2 g4 a4] [g4@3 ~]>").s("piano").clip(.9).gain(.5),'
    'note("<[c3,g3] [g2,d3]>").s("piano").gain(.2)).room(.3)。'
    '系统自动保存作者和谱子，展示可直接播放的卡片，动画由播放器负责。'
)


async def ensure_schema(db):
    await db.execute('''CREATE TABLE IF NOT EXISTS compositions (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, code TEXT NOT NULL,
        bpm REAL NOT NULL, cycles INTEGER NOT NULL, actor TEXT NOT NULL,
        actor_name TEXT NOT NULL, source_type TEXT NOT NULL, source_id TEXT NOT NULL,
        source_msg_id TEXT NOT NULL, favorite INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, updated_at REAL NOT NULL
    )''')


def _payload(raw):
    data = extract_json_object(raw)
    if not data or not isinstance(data.get('code'), str) or not data['code'].strip():
        return None
    code = data['code'].strip()
    if len(code) > 6000:
        return None
    try:
        bpm = data.get('bpm')
        if bpm is None:
            match = re.search(r'setcpm\(\s*([\d.]+)\s*(?:/\s*([\d.]+))?\s*\)', code, re.I)
            bpm = float(match[1]) * 4 / float(match[2] or 1) if match else 80
        bpm = float(bpm)
        cycles = int(data.get('cycles', 8))
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
    if not math.isfinite(bpm) or not 1 <= bpm <= 400 or not 1 <= cycles <= 256:
        return None
    return {'title': str(data.get('title') or '写给你的小曲').strip()[:100],
            'code': code, 'bpm': bpm, 'cycles': cycles}


def _piece(row):
    piece = dict(row)
    piece['favorite'] = bool(piece['favorite'])
    return piece


async def list_compositions():
    async with get_db() as db:
        await ensure_schema(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM compositions ORDER BY created_at DESC')
        return [_piece(row) for row in await cursor.fetchall()]


async def get_composition(piece_id):
    async with get_db() as db:
        await ensure_schema(db)
        db.row_factory = aiosqlite.Row
        row = await (await db.execute('SELECT * FROM compositions WHERE id=?', (piece_id,))).fetchone()
        return _piece(row) if row else None


async def set_favorite(piece_id, favorite):
    async with get_db() as db:
        await ensure_schema(db)
        cursor = await db.execute('UPDATE compositions SET favorite=?,updated_at=? WHERE id=?',
                                  (int(favorite), time.time(), piece_id))
        await db.commit()
        return bool(cursor.rowcount)


async def process_compositions(text, *, source_type, source_id, sender, source_msg_id, on_saved=None):
    from chatroom import get_chatroom_names

    raw = text or ''
    matches = list(COMPOSE_CMD_PATTERN.finditer(raw))
    cleaned = COMPOSE_CMD_PATTERN.sub('', raw).strip()
    if not matches or sender not in ('aion', 'connor'):
        return cleaned
    # The capability switch controls prompt injection only. Old scores and
    # commands already in flight keep working when it is turned off.
    payload = None
    for match in matches:
        if match[0].upper().endswith('[/COMPOSE]'):
            payload = _payload(match[1])
        if payload:
            break
    if not payload:
        return cleaned
    names = dict(zip(('user', 'aion', 'connor'), get_chatroom_names()))
    identity = hashlib.sha256(f'{source_type}:{source_id}:{source_msg_id}'.encode()).hexdigest()[:24]
    piece_id = 'composition_' + identity
    message_id = 'composition_card_' + identity
    private = source_type == 'private'
    table, key, role = ('messages', 'conv_id', 'role') if private else ('chatroom_messages', 'room_id', 'sender')
    parent = 'conversations' if private else 'chatroom_rooms'
    now = time.time()
    attachment = {'type': 'composition', 'id': piece_id, 'actor': sender, 'actor_name': names[sender],
                  'title': payload['title'], 'bpm': payload['bpm'], 'cycles': payload['cycles'], 'created_at': now}
    message = {
        'id': message_id, key: source_id, role: 'system',
        'content': f'{names[sender]}写了一首《{payload["title"]}》。', 'created_at': now,
        'attachments': [attachment, {'type': 'system_model_context'},
                        {'type': 'system_notice_order', 'after_msg_id': source_msg_id}],
    }
    async with get_db() as db:
        await ensure_schema(db)
        if not await (await db.execute(f'SELECT id FROM {parent} WHERE id=?', (source_id,))).fetchone():
            return cleaned
        existed = await (await db.execute(f'SELECT created_at FROM {table} WHERE id=?', (message_id,))).fetchone()
        cursor = await db.execute('''
            INSERT INTO compositions (id,title,code,bpm,cycles,actor,actor_name,source_type,source_id,source_msg_id,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,code=excluded.code,bpm=excluded.bpm,
                cycles=excluded.cycles,updated_at=excluded.updated_at
            WHERE compositions.code!=excluded.code OR compositions.title!=excluded.title
                OR compositions.bpm!=excluded.bpm OR compositions.cycles!=excluded.cycles
        ''', (piece_id, payload['title'], payload['code'], payload['bpm'], payload['cycles'], sender,
              names[sender], source_type, source_id, source_msg_id, now, now))
        if not cursor.rowcount:
            await db.commit()
            return cleaned
        if existed:
            message['created_at'] = existed[0]
        await db.execute(f'''
            INSERT INTO {table} (id,{key},{role},content,created_at,attachments) VALUES (?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET content=excluded.content,attachments=excluded.attachments
        ''', (message_id, source_id, 'system', message['content'], message['created_at'],
              json.dumps(message['attachments'], ensure_ascii=False)))
        await db.execute(f'UPDATE {parent} SET updated_at=? WHERE id=?', (now, source_id))
        event_type = ('msg_updated' if existed else 'msg_created') if private else (
            'chatroom_msg_updated' if existed else 'chatroom_msg_created')
        event = {'type': event_type, 'data': message}
        seq = await append_sync_event(db, event)
        await db.commit()
    await broadcast_synced(manager, event, seq)
    if on_saved:
        await on_saved(message)
    return cleaned
