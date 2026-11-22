"""Read an existing chat attachment only when editing or renaming it."""
import json
import re
import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from chat_web_cards import EDIT_PROMPT, EDIT_REQUEST, split_reply
from database import get_db

router = APIRouter(prefix='/api/web-cards', tags=['web-cards'])
TABLES = {'private': ('messages', 'conv_id', 'role'), 'chatroom': ('chatroom_messages', 'room_id', 'sender')}


async def revision_context(scope, parent_id, query, actor):
    if scope not in TABLES or not EDIT_REQUEST.search(query or ''):
        return ''
    from context_builder import _parse_timeline_attachments

    table, parent, role = TABLES[scope]
    author = 'assistant' if scope == 'private' else actor
    async with get_db() as db:
        rows = await (await db.execute(
            f'SELECT attachments FROM {table} WHERE {parent}=? AND {role}=? ORDER BY created_at DESC LIMIT 12',
            (parent_id, author))).fetchall()
    for index, (raw,) in enumerate(rows):
        for card in _parse_timeline_attachments(raw):
            if not isinstance(card, dict) or card.get('type') != 'web_card' or card.get('format') != 'svg':
                continue
            named = card['title'] in query or re.search(r'动画|小画|卡片|这个互动', query)
            followup = index == 0 and re.search(r'点不动|没反应|打不开|把.{0,25}(改|换|变)成|^(?:再)?(?:改一下|换个颜色|加个按钮)', query)
            if named or followup:
                return '本轮提到的原动画如下，请按刚才的要求修改，在回复中给出完整的新版 <svg>。\n' + card['html']
    return ''


class CardChange(BaseModel):
    revision: int = Field(ge=1)
    instruction: str = Field(default='', max_length=600)
    title: str | None = Field(default=None, max_length=60)


@router.post('/{scope}/{msg_id}/{card_id}')
async def change_card(scope: Literal['private', 'chatroom'], msg_id: str, card_id: str, body: CardChange):
    from ai_providers import simple_ai_call
    from capabilities import is_capability_enabled
    from config import DEFAULT_MODEL
    from chatroom import load_chatroom_config
    from sync_events import append_sync_event, broadcast_synced
    from ws import manager

    rename = body.title is not None
    if not rename and not is_capability_enabled('web_cards'):
        raise HTTPException(409, '即兴小动画已关闭')
    if rename and not body.title.strip():
        raise HTTPException(422, '请填写标题')
    if not rename and not body.instruction.strip():
        raise HTTPException(422, '请填写修改内容')
    table, _, role = TABLES[scope]
    async def read(db):
        row = await (await db.execute(
            f'SELECT attachments,{role} FROM {table} WHERE id=?', (msg_id,))).fetchone()
        if row and row[1] in ('assistant','aion','connor'):
            attachments = json.loads(row[0] or '[]')
            for card in attachments:
                if isinstance(card, dict) and card.get('type') == 'web_card' and card.get('id') == card_id:
                    return attachments, card, row[1]
        raise HTTPException(404, '找不到这张卡片')

    async def save(db, attachments, card):
        card['revision'] += 1
        await db.execute(f'UPDATE {table} SET attachments=? WHERE id=?',
                         (json.dumps(attachments, ensure_ascii=False), msg_id))
        db.row_factory = sqlite3.Row
        message = dict(await (await db.execute(f'SELECT * FROM {table} WHERE id=?', (msg_id,))).fetchone())
        message['attachments'] = attachments
        event = {'type': 'msg_updated' if scope == 'private' else 'chatroom_msg_updated', 'data': message}
        seq = await append_sync_event(db, event)
        await db.commit()
        await broadcast_synced(manager, event, seq)
        return card

    async with get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        attachments, card, author = await read(db)
        if card['revision'] != body.revision:
            raise HTTPException(409, '作品已有新版本，请重新打开后再修改')
        if rename:
            card.update(title=body.title.strip(), title_edited=True)
            return await save(db, attachments, card)
        if card.get('format') != 'svg':
            raise HTTPException(409, '旧网页卡片只能改标题')
    config = load_chatroom_config()
    model = card.get('model') or (config.get('connor_model') if author == 'connor' else config.get('aion_model')) or DEFAULT_MODEL
    if author == 'connor':
        from routes.chatroom import _resolve_connor_model
        model = _resolve_connor_model(model)
    prompt = [{'role':'system', 'content':EDIT_PROMPT},
              {'role':'user', 'content':json.dumps({'原作品':card['html'], '要求':body.instruction},
                                                    ensure_ascii=False)}]
    answer = await simple_ai_call(prompt, model, trace_label='chat_web_card_edit')
    _, updated = split_reply(answer)
    if not updated or updated['type'] != 'web_card':
        raise HTTPException(422, '这次没画出完整的动画，已保留原版')
    async with get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        attachments, current, _ = await read(db)
        if current['revision'] != body.revision:
            raise HTTPException(409, '作品已有新版本，请重新打开后再修改')
        if not current.get('title_edited'):
            current['title'] = updated['title']
        current['html'] = updated['html']
        return await save(db, attachments, current)
