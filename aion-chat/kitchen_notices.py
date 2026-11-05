"""Persist meal decisions in the ordinary chat timeline, then broadcast after commit."""
import json
import logging
import time

from config import load_worldbook


async def record_notice(db, *, menu_id, proposal_id, meal, action, origin):
    tables = {r[0] for r in await (await db.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    room = None
    if {'chatroom_rooms','chatroom_messages'} <= tables:
        if origin.get('scope') == 'chatroom':
            room = await (await db.execute("SELECT id FROM chatroom_rooms WHERE id=? AND type='group'", (origin.get('parent_id'),))).fetchone()
        if not room:
            room = await (await db.execute("SELECT id FROM chatroom_rooms WHERE type='group' ORDER BY updated_at DESC LIMIT 1")).fetchone()
        if not room and origin.get('scope') == 'chatroom':
            room = await (await db.execute('SELECT id FROM chatroom_rooms WHERE id=?', (origin.get('parent_id'),))).fetchone()
    conv_id = origin.get('parent_id') if origin.get('scope') == 'private' else None
    if not room and not conv_id and 'messages' in tables:
        recent = await (await db.execute("SELECT conv_id FROM messages WHERE role IN ('user','assistant') ORDER BY created_at DESC LIMIT 1")).fetchone()
        conv_id = recent[0] if recent else None
    if not room and (not conv_id or 'messages' not in tables):
        return None  # A bare ledger can exist before any chat has been created.
    user = load_worldbook().get('user_name') or '用户'
    label = '午餐' if meal['meal']=='lunch' else '晚餐'
    text = f'{user}确认了{label}菜单：{meal["name"]}' if action=='confirm' else f'{user}移除了{label}菜单：{meal["name"]}，{label}尚未确定。'
    now, msg_id = time.time(), f'km_notice_{time.time_ns()}'
    attachments = [{'type':'system_model_context'}, {'type':'kitchen_decision','action':action,
                   'menu_id':menu_id,'proposal_id':proposal_id,'meal':meal['meal']}]
    if room:
        await db.execute('INSERT INTO chatroom_messages (id,room_id,sender,content,attachments,created_at) VALUES (?,?,?,?,?,?)',
                         (msg_id,room[0],'system',text,json.dumps(attachments),now))
        await db.execute('UPDATE chatroom_rooms SET updated_at=? WHERE id=?', (now,room[0]))
        event = {'type':'chatroom_msg_created','data':{'id':msg_id,'room_id':room[0],'sender':'system',
                 'content':text,'attachments':attachments,'created_at':now}}
    else:
        await db.execute('INSERT INTO messages (id,conv_id,role,content,attachments,created_at) VALUES (?,?,?,?,?,?)',
                         (msg_id,conv_id,'system',text,json.dumps(attachments),now))
        event = {'type':'msg_created','data':{'id':msg_id,'conv_id':conv_id,'role':'system',
                 'content':text,'attachments':attachments,'created_at':now}}
    if 'sync_events' in tables:
        from sync_events import append_sync_event, attach_sync_seq
        event = attach_sync_seq(event, await append_sync_event(db,event))
    return event


async def publish(events):
    from ws import manager
    for event in events:
        if event:
            try:
                await manager.broadcast(event)
            except Exception:
                logging.getLogger(__name__).warning('Kitchen decision saved; live delivery unavailable',exc_info=True)
