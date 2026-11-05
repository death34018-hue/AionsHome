"""Only a settled result card enters daily chat; table dialogue stays local."""
import json
import time


async def last_active_window():
    from database import get_db
    async with get_db() as db:
        candidates = []
        # Compare actual user sends. Startup can re-stamp older inferred routes,
        # and AI replies/result cards must never steal the user's last window.
        cur = await db.execute("""
            SELECT m.conv_id,m.created_at FROM messages m JOIN conversations c ON c.id=m.conv_id
            WHERE m.role='user' AND c.id NOT GLOB 'billiards_*' ORDER BY m.created_at DESC LIMIT 1
        """)
        row = await cur.fetchone()
        if row:
            candidates.append((row[1], {'type': 'private', 'id': row[0]}))
        cur = await db.execute("""
            SELECT m.room_id,m.created_at FROM chatroom_messages m JOIN chatroom_rooms r ON r.id=m.room_id
            WHERE m.sender='user' AND r.type IN ('group','connor_1v1') AND r.id NOT GLOB 'billiards_*'
            ORDER BY m.created_at DESC LIMIT 1
        """)
        row = await cur.fetchone()
        if row:
            candidates.append((row[1], {'type': 'chatroom', 'id': row[0]}))
        if candidates:
            return max(candidates, key=lambda c: c[0])[1]
        # No user transcript yet: use the existing persisted window routes.
        cur = await db.execute("SELECT id FROM conversations WHERE id NOT GLOB 'billiards_*' ORDER BY updated_at DESC LIMIT 1")
        private = await cur.fetchone()
        cur = await db.execute("SELECT key,value,updated_at FROM runtime_state WHERE key IN ('aion_last_active','connor_last_active')")
        for key, value, updated in await cur.fetchall():
            if key == 'aion_last_active' and value == 'private':
                if private:
                    candidates.append((updated, {'type': 'private', 'id': private[0]}))
                continue
            room_id = value.split(':', 1)[1] if value.startswith('chatroom:') else value
            cur = await db.execute("SELECT id FROM chatroom_rooms WHERE id=? AND type IN ('group','connor_1v1') AND id NOT GLOB 'billiards_*'", (room_id,))
            if await cur.fetchone():
                candidates.append((updated, {'type': 'chatroom', 'id': room_id}))
        return max(candidates, key=lambda c: c[0])[1] if candidates else None


def result_card(game, result, labels):
    players = [{'actor': actor, 'name': labels[actor]} for actor in game['players']]
    winner, loser = (players[result['winner']]['name'], players[1-result['winner']]['name'])
    score = result['score']
    content = (f"【台球战报 · 第{result['rack']}局】\n"
               f"{winner}获胜，{loser}落败。\n"
               f"累计胜局：{players[0]['name']} {score[0]} : {score[1]} {players[1]['name']}。\n"
               f"本局共 {result['shots']} 杆；{result['reason']}。")
    if result.get('match_complete'):
        content += f"\n本场结束：{winner}以 {score[result['winner']]}:{score[1-result['winner']]} 赢得本场比赛。"
    card = {'type': 'billiards_result', 'game_id': game['id'], 'rack': result['rack'],
            'players': players, 'winner': result['winner'], 'score': score,
            'shots': result['shots'], 'reason': result['reason'], 'finished_at': result.get('finished_at'),
            'match_complete': result.get('match_complete', False)}
    return content, [card, {'type': 'system_model_context'}]


async def save_result_card(game, result, target, labels):
    content, attachments = result_card(game, result, labels)
    return await save_chat_card(f"pool_result_{game['id']}_{result['rack']}", content, attachments, target)


async def save_chat_card(message_id, content, attachments, target):
    """Persist a compact game report using the same routing and sync as daily chat."""
    from database import get_db
    from sync_events import append_sync_event, broadcast_synced
    from ws import manager
    now = time.time()
    message = {'id': message_id, 'content': content, 'created_at': now, 'attachments': attachments}
    async with get_db() as db:
        # Stable IDs keep recovery/retries from duplicating a result, even if JSON saving failed.
        for table in ('messages', 'chatroom_messages'):
            cur = await db.execute(f'SELECT id FROM {table} WHERE id=?', (message_id,))
            if await cur.fetchone():
                return True
        if target['type'] == 'private':
            cur = await db.execute('SELECT id FROM conversations WHERE id=?', (target['id'],))
            if not await cur.fetchone():
                return False
            message.update(conv_id=target['id'], role='system')
            await db.execute('INSERT INTO messages (id,conv_id,role,content,created_at,attachments) VALUES (?,?,?,?,?,?)',
                             (message_id, target['id'], 'system', content, now, json.dumps(attachments, ensure_ascii=False)))
            await db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (now, target['id']))
            event_type = 'msg_created'
        else:
            cur = await db.execute('SELECT id FROM chatroom_rooms WHERE id=?', (target['id'],))
            if not await cur.fetchone():
                return False
            message.update(room_id=target['id'], sender='system')
            await db.execute('INSERT INTO chatroom_messages (id,room_id,sender,content,created_at,attachments) VALUES (?,?,?,?,?,?)',
                             (message_id, target['id'], 'system', content, now, json.dumps(attachments, ensure_ascii=False)))
            await db.execute('UPDATE chatroom_rooms SET updated_at=? WHERE id=?', (now, target['id']))
            event_type = 'chatroom_msg_created'
        event = {'type': event_type, 'data': message}
        seq = await append_sync_event(db, event)
        await db.commit()
    await broadcast_synced(manager, event, seq)
    return True
