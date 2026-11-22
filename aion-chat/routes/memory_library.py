"""Shared, paginated management of active memories and their original archives."""
import json
import time
import uuid
from datetime import date as Date, datetime, timedelta
from typing import Literal

import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from database import get_db
from memory import get_embedding, _pack_embedding, _memory_time_payload, memory_kind_for_type, _source_ids_for_memory
from memory_compression import resolve_source_memories

router = APIRouter(prefix='/api/memory-library')
Store = Literal['main', 'chatroom']
OCCURRED = 'COALESCE(source_end_ts,source_start_ts,created_at)'
FIELDS = ('id,content,keywords,importance,created_at,source_start_ts,source_end_ts,'
          'source_msg_id,unresolved,evidence_summary,evidence_detail_level,archive_state,'
          'compression_stage,compression_batch_id,source_memory_ids,period_kind')


def _table(store):
    return 'memories' if store == 'main' else 'chatroom_memories'


def _scope(store, alias=''):
    return f"{alias}scope='connor'" if store == 'chatroom' else '1=1'


def _fields(store):
    return FIELDS + (',type,source_conv' if store == 'main' else ',memory_kind,room_id,scope')


def _list(value):
    try:
        parsed = json.loads(value or '[]')
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _names():
    from chatroom import get_chatroom_names
    return get_chatroom_names()


def _item(row, store):
    item = {key: value for key, value in dict(row).items() if key in _fields(store).split(',')}
    item['memory_kind'] = memory_kind_for_type(item.get('type')) if store == 'main' else item.get('memory_kind') or 'long_term'
    item['is_atom'] = not item.get('compression_batch_id') and not item.get('compression_stage') and not _list(item.get('source_memory_ids'))
    item.update(_memory_time_payload(item))
    return item


async def _get(db, store, mem_id):
    row = await (await db.execute(f'SELECT {_fields(store)} FROM {_table(store)} WHERE id=? AND {_scope(store)}', (mem_id,))).fetchone()
    if not row:
        raise HTTPException(404, '这条记忆不存在或已被删除')
    return _item(row, store)


async def _related(db, store, mem_id):
    # Modern links are precise. Older batch links are shown explicitly as candidates.
    rows = await (await db.execute(
        f'SELECT {_fields(store)} FROM {_table(store)} m WHERE {_scope(store, "m.")} AND ('
        "EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(m.source_memory_ids) THEN m.source_memory_ids ELSE '[]' END) j WHERE j.value=?) "
        'OR (m.source_memory_ids IS NULL AND EXISTS (SELECT 1 FROM memory_compression_batch_inputs b '
        'WHERE b.batch_id=m.compression_batch_id AND b.store=? AND b.memory_id=?))) '
        f'ORDER BY {OCCURRED} DESC,id DESC', (mem_id, store, mem_id))).fetchall()
    return [{**_item(row, store), 'lineage_exact': row['source_memory_ids'] is not None} for row in rows]


async def _broadcast(store, event_type, item):
    from ws import manager
    from sync_events import append_sync_event, attach_sync_seq
    event = {'type': event_type if store == 'main' else 'memory_collection_changed',
             'data': {**item, 'target': store}}
    async with get_db() as db:
        seq = await append_sync_event(db, event)
        await db.commit()
    await manager.broadcast(attach_sync_seq(event, seq))


class MemoryWrite(BaseModel):
    content: str = Field(min_length=1, max_length=30000)
    memory_kind: Literal['daily', 'long_term', 'friend'] = 'long_term'
    keywords: str = Field(default='', max_length=2000)
    importance: float = Field(default=.5, ge=0, le=1)
    unresolved: bool = False
    evidence_summary: str = Field(default='', max_length=5000)
    date: Date | None = None

    @field_validator('content')
    @classmethod
    def content_not_blank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('记忆内容不能为空')
        return value


@router.get('/{store}')
async def list_library(store: Store, view: Literal['memories', 'atoms', 'unresolved'] = 'memories',
                       state: Literal['all', 'active', 'cold'] = 'all',
                       kind: Literal['all', 'daily', 'long_term', 'friend', 'unresolved'] = 'all',
                       q: str = Query('', max_length=200), start: Date | None = None,
                       end: Date | None = None, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100)):
    if start and end and start > end:
        raise HTTPException(400, '开始日期不能晚于结束日期')
    where, args = [_scope(store)], []
    unresolved_only = view == 'unresolved' or kind == 'unresolved'
    if unresolved_only:
        where.append('unresolved=1')
    if view == 'memories' and not unresolved_only:
        where.append("COALESCE(archive_state,'active')='active'")
    elif view == 'atoms':
        # Durable facts distilled by compression have stage=0 too, but aren't originals.
        where.extend(["COALESCE(compression_stage,0)=0", "COALESCE(compression_batch_id,'')=''",
                      "COALESCE(source_memory_ids,'[]') IN ('[]','')"])
        if state != 'all':
            where.append("COALESCE(archive_state,'active')=?")
            args.append(state)
    if kind not in ('all', 'unresolved'):
        if store == 'main':
            if kind == 'friend':
                where.append("LOWER(type)='friend'")
            else:
                where.append("LOWER(type) " + ('IN' if kind == 'daily' else 'NOT IN') + " ('daily','digest','seeky_digest','seeky_compressed')")
                if kind == 'long_term':
                    where.append("LOWER(type)!='friend'")
        else:
            where.append("COALESCE(memory_kind,'long_term')=?")
            args.append(kind)
    if q.strip():
        # Search literals rather than treating user-entered % and _ as wildcards.
        needle = '%' + q.strip().lower().replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
        where.append("(LOWER(content) LIKE ? ESCAPE '!' OR LOWER(COALESCE(keywords,'')) LIKE ? ESCAPE '!')")
        args.extend([needle, needle])
    if start:
        where.append(f'{OCCURRED}>=?')
        args.append(datetime.combine(start, datetime.min.time()).timestamp())
    if end:
        where.append('COALESCE(source_start_ts,source_end_ts,created_at)<?')
        args.append(datetime.combine(end + timedelta(days=1), datetime.min.time()).timestamp())
    clause = ' AND '.join(where)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        total = (await (await db.execute(f'SELECT COUNT(*) FROM {_table(store)} WHERE {clause}', args)).fetchone())[0]
        page = min(page, max(1, (total + limit - 1) // limit))
        rows = await (await db.execute(f'SELECT {_fields(store)} FROM {_table(store)} WHERE {clause} '
                                     f'ORDER BY {OCCURRED} DESC,id DESC LIMIT ? OFFSET ?', (*args, limit, (page-1)*limit))).fetchall()
    return {'items': [_item(row, store) for row in rows], 'total': total, 'page': page, 'limit': limit}


@router.get('/{store}/{mem_id}')
async def memory_detail(store: Store, mem_id: str):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        item = await _get(db, store, mem_id)
        related = await _related(db, store, mem_id)
    originals = await resolve_source_memories(store, mem_id) if not item['is_atom'] else []
    return {'item': item, 'related': related,
            'originals': [{**_item(row, store), 'lineage_exact': row.get('lineage_exact', True)} for row in originals]}


@router.post('/{store}')
async def create_memory(store: Store, body: MemoryWrite):
    mem_id = 'mem_' + uuid.uuid4().hex
    vec = await get_embedding(body.content)
    occurred = datetime.combine(body.date, datetime.min.time()).timestamp() if body.date else None
    values = dict(id=mem_id, content=body.content, keywords=body.keywords, importance=body.importance,
                  unresolved=int(body.unresolved), evidence_summary=body.evidence_summary, created_at=time.time(),
                  source_start_ts=occurred, source_end_ts=occurred, source_msg_id='[]', embedding=_pack_embedding(vec) if vec else None)
    if store == 'main':
        values['type'] = {'daily':'daily','friend':'friend','long_term':'important'}[body.memory_kind]
    else:
        values.update(memory_kind=body.memory_kind, scope='connor', room_id='connor_unified')
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(f'INSERT INTO {_table(store)} ({",".join(values)}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))
        await db.commit()
        item = await _get(db, store, mem_id)
    await _broadcast(store, 'memory_added', item)
    return {'ok': True, 'id': mem_id, 'embedding_ready': bool(vec)}


@router.put('/{store}/{mem_id}')
async def update_memory(store: Store, mem_id: str, body: MemoryWrite):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        old = await _get(db, store, mem_id)
    values = dict(content=body.content, keywords=body.keywords, importance=body.importance,
                  unresolved=int(body.unresolved), evidence_summary=body.evidence_summary)
    embedding_ready = True
    if old['content'] != body.content:
        vec = await get_embedding(body.content)
        values['embedding'] = _pack_embedding(vec) if vec else None
        embedding_ready = bool(vec)  # Never leave a vector for the old content attached.
    if store == 'main':
        if body.memory_kind != old['memory_kind']:
            values['type'] = {'daily':'daily','friend':'friend','long_term':'important'}[body.memory_kind]
    else:
        values['memory_kind'] = body.memory_kind
    if body.date:
        values['source_start_ts'] = values['source_end_ts'] = datetime.combine(body.date, datetime.min.time()).timestamp()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await _get(db, store, mem_id)
        await db.execute(f'UPDATE {_table(store)} SET {",".join(k+"=?" for k in values)} WHERE id=? AND {_scope(store)}', (*values.values(), mem_id))
        await db.commit()
        item = await _get(db, store, mem_id)
    await _broadcast(store, 'memory_updated', item)
    return {'ok': True, 'embedding_ready': embedding_ready}


@router.delete('/{store}/{mem_id}')
async def delete_memory(store: Store, mem_id: str, confirm_related: bool = False):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN IMMEDIATE')
        await _get(db, store, mem_id)
        related = await _related(db, store, mem_id)
        if related and not confirm_related:
            raise HTTPException(409, '这条记忆被其他记忆引用，请查看关联后确认删除')
        for row in related:
            if row['source_memory_ids'] is not None:
                parents = [p for p in _list(row['source_memory_ids']) if p != mem_id]
                await db.execute(f'UPDATE {_table(store)} SET source_memory_ids=? WHERE id=?', (json.dumps(parents), row['id']))
        await db.execute('DELETE FROM memory_compression_batch_inputs WHERE store=? AND memory_id=?', (store, mem_id))
        await db.execute(f'DELETE FROM {_table(store)} WHERE id=? AND {_scope(store)}', (mem_id,))
        await db.commit()
    await _broadcast(store, 'memory_deleted', {'id': mem_id})
    return {'ok': True}


async def _source_rows(db, store, ids=None, start=None, end=None, page=None, limit=50):
    """Only messages belonging to the selected AI, including system events."""
    queries, all_args = [], []
    if ids is not None and not ids:
        return ([], 0) if page else []
    for namespace in (('private', 'chatroom') if store == 'main' else ('chatroom',)):
        args = []
        where = []
        if namespace == 'private':
            sql = "SELECT 'private:'||m.id AS id,m.role,m.content,m.created_at,m.conv_id AS window_id,'private' AS source FROM messages m"
        else:
            sql = "SELECT 'chatroom:'||m.id AS id,m.sender AS role,m.content,m.created_at,m.room_id AS window_id,r.type AS source FROM chatroom_messages m JOIN chatroom_rooms r ON r.id=m.room_id"
            where.append("r.type IN ('group','connor_1v1','peer_dm')" if store == 'chatroom' else "r.type IN ('group','peer_dm')")
        if ids is not None:
            raw_ids = [v.split(':', 1)[1] for v in ids if v.startswith(namespace + ':')]
            if not raw_ids:
                continue
            where.append("m.id IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(raw_ids))
        else:
            where.append('m.created_at>=? AND m.created_at<=?')
            args.extend([start, end])
        queries.append(sql + ' WHERE ' + ' AND '.join(where))
        all_args.extend(args)
    if not queries:
        return ([], 0) if page else []
    union = ' UNION ALL '.join(queries)
    total = (await (await db.execute(f'SELECT COUNT(*) FROM ({union})', all_args)).fetchone())[0] if page else 0
    sql = f'SELECT * FROM ({union}) ORDER BY created_at,id'
    if page:
        sql += ' LIMIT ? OFFSET ?'
        all_args.extend([limit, (page-1)*limit])
    rows = [dict(row) for row in await (await db.execute(sql, all_args)).fetchall()]
    return (rows, total) if page else rows


@router.get('/{store}/{mem_id}/sources')
async def memory_sources(store: Store, mem_id: str, page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=100)):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        item = await _get(db, store, mem_id)
    originals = await resolve_source_memories(store, mem_id)
    ids = []
    for original in originals:
        source = {**original, 'source_conv': 'chatroom:unified'} if store == 'chatroom' else original
        ids.extend(_source_ids_for_memory(source))
    # An explicitly empty selection must stay empty, not resurrect date candidates.
    explicit = item.get('source_msg_id') is not None
    exact = all(row.get('lineage_exact', True) for row in originals)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if ids or explicit or not item['is_atom']:
            rows, total = await _source_rows(db, store, ids=list(dict.fromkeys(ids)), page=page, limit=limit)
        elif item.get('source_start_ts') is not None and item.get('source_end_ts') is not None:
            rows, total = await _source_rows(db, store, start=item['source_start_ts'], end=item['source_end_ts'], page=page, limit=limit)
            exact = False
        else:
            rows, total = [], 0
    user, main, companion = _names()
    names = {'user': user, 'assistant': main, 'aion': main, 'connor': companion, 'system': '系统消息'}
    for row in rows:
        row['name'] = names.get(row['role'], row['role'])
    return {'messages': rows, 'total': total, 'page': page,
            'exact': exact, 'has_more': page*limit < total}


class SourceSelection(BaseModel):
    source_message_ids: list[str]


@router.put('/{store}/{mem_id}/sources')
async def save_sources(store: Store, mem_id: str, body: SourceSelection):
    ids = list(dict.fromkeys(body.source_message_ids))
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        item = await _get(db, store, mem_id)
        if not item['is_atom']:
            raise HTTPException(400, '摘要的来源请在对应原子记忆中编辑')
        rows = await _source_rows(db, store, ids=ids)
        if {row['id'] for row in rows} != set(ids):
            raise HTTPException(400, '原文不存在或不属于当前记忆库')
        await db.execute(f'UPDATE {_table(store)} SET source_msg_id=? WHERE id=? AND {_scope(store)}', (json.dumps(ids), mem_id))
        await db.commit()
    await _broadcast(store, 'memory_updated', {'id': mem_id, 'source_msg_id': json.dumps(ids)})
    return {'ok': True}


@router.get('/{store}/maintenance/anchor')
async def get_anchor(store: Store):
    if store == 'main':
        from config import load_digest_anchor
        ts = load_digest_anchor()
    else:
        async with get_db() as db:
            row = await (await db.execute("SELECT anchor_ts FROM chatroom_digest_anchors WHERE room_id='connor_unified'")).fetchone()
            ts = row[0] if row else 0
    return {'anchor_ts': ts}


class AnchorWrite(BaseModel):
    date: Date


@router.put('/{store}/maintenance/anchor')
async def set_anchor(store: Store, body: AnchorWrite):
    ts = datetime.combine(body.date, datetime.min.time()).timestamp()
    if store == 'main':
        from config import save_digest_anchor
        save_digest_anchor(ts)
    else:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO chatroom_digest_anchors (room_id,anchor_ts) VALUES ('connor_unified',?)", (ts,))
            await db.commit()
    return {'ok': True, 'anchor_ts': ts}


@router.post('/{store}/maintenance/digest')
async def digest(store: Store):
    if store == 'main':
        from memory import manual_digest
        return await manual_digest()
    from chatroom import digest_chatroom, load_chatroom_config
    return await digest_chatroom(model_key=load_chatroom_config().get('connor_model') or 'Codex')


@router.post('/{store}/maintenance/rebuild')
async def rebuild(store: Store):
    async with get_db() as db:
        rows = await (await db.execute(f'SELECT id,content FROM {_table(store)} WHERE {_scope(store)}')).fetchall()
    success = 0
    for mem_id, content in rows:
        vec = await get_embedding(content)
        if vec:
            async with get_db() as db:
                # An edit made while rebuilding must not receive the old content's vector.
                cur = await db.execute(f'UPDATE {_table(store)} SET embedding=? WHERE id=? AND content=?', (_pack_embedding(vec), mem_id, content))
                await db.commit()
                success += cur.rowcount
    return {'ok': True, 'total': len(rows), 'success': success, 'failed': len(rows)-success}
