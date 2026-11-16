"""Read an existing chat attachment only when editing or repairing it."""
import json
import re
import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from chat_web_cards import EDIT_REQUEST, PAGE_GUIDE, make_card, split_reply
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
            if not isinstance(card, dict) or card.get('type') != 'web_card':
                continue
            named = card['title'] in query or re.search(r'网页|页面|卡片|这个动画|这个互动', query)
            followup = index == 0 and re.search(r'点不动|没反应|打不开|把.{0,25}(改|换|变)成|^(?:再)?(?:改一下|换个颜色|加个按钮)', query)
            if named or followup:
                return '本轮提到的原网页如下，请按刚才的要求修改并返回完整新版。\n' + card['html']
    return ''


class CardChange(BaseModel):
    revision: int = Field(ge=1)
    error: str = Field(default='', max_length=400)
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
        raise HTTPException(409, '即兴网页创作已关闭')
    if rename and not body.title.strip():
        raise HTTPException(422, '请填写标题')
    if not rename and not (body.error.strip() or body.instruction.strip()):
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
        raise HTTPException(404, '找不到这张网页卡片')

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

    automatic = not rename and not body.instruction.strip()
    async with get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        attachments, card, author = await read(db)
        if card['revision'] != body.revision:
            if not automatic:
                raise HTTPException(409, '作品已有新版本，请重新打开后再修改')
            return card
        if rename:
            card.update(title=body.title.strip(), title_edited=True)
            return await save(db, attachments, card)
        if automatic and card.get('auto_repaired'):
            raise HTTPException(409, '这张网页已尝试自动修复，可填写修改要求')
        if automatic:
            card['auto_repaired'] = True
            await db.execute(f'UPDATE {table} SET attachments=? WHERE id=?',
                             (json.dumps(attachments, ensure_ascii=False), msg_id))
        await db.commit()
    config = load_chatroom_config()
    model = card.get('model') or (config.get('connor_model') if author == 'connor' else config.get('aion_model')) or DEFAULT_MODEL
    if author == 'connor':
        from routes.chatroom import _resolve_connor_model
        model = _resolve_connor_model(model)
    prompt = [{'role':'system', 'content':'按要求修改，保留原玩法。仅返回[WEB]和[/WEB]包围的完整自含HTML。' + PAGE_GUIDE},
              {'role':'user', 'content':json.dumps({'原网页':card['html'], '要求':body.instruction or '修复运行错误',
                                                    '运行错误':body.error}, ensure_ascii=False)}]
    answer = await simple_ai_call(prompt, model, trace_label='chat_web_card_edit')
    _, updated = split_reply(answer)
    if not updated:
        updated = make_card(answer) if (answer or '').lstrip().startswith('<') else None
    if not updated or updated['type'] != 'web_card':
        raise HTTPException(422, '这次没生成完整网页，已保留原版')
    async with get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        attachments, current, _ = await read(db)
        if current['revision'] != body.revision:
            if not automatic:
                raise HTTPException(409, '作品已有新版本，请重新打开后再修改')
            return current
        if not current.get('title_edited'):
            current['title'] = updated['title']
        current.update(html=updated['html'], auto_repaired=automatic)
        return await save(db, attachments, current)
