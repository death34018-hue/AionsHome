"""One shelf choice and one segment per independent wake."""

import asyncio
import json
import random
import re

from book import SEGMENT_MAX_CHARS, build_annotate_text, segment_paragraphs
from routes import book as books


async def insert_reading_notice(db, text, reply_id, reply_time, *, room_id=None, conv_id=None):
    """Save the notice in the reply's transaction and anchor it above that reply."""
    msg_id = f'{reply_id}_reading'
    timestamp = reply_time - 0.000001
    attachments = [{'type': 'system_model_context'}, {'type': 'system_notice_order', 'before_msg_id': reply_id}]
    if room_id is not None:
        table, parent, role, target, event = 'chatroom_messages', 'room_id', 'sender', room_id, 'chatroom_msg_created'
    else:
        table, parent, role, target, event = 'messages', 'conv_id', 'role', conv_id, 'msg_created'
    await db.execute(f"INSERT INTO {table}(id,{parent},{role},content,created_at,attachments) VALUES(?,?,'system',?,?,?)",
                     (msg_id, target, text, timestamp, json.dumps(attachments)))
    return {'type': event, 'data': {'id': msg_id, parent: target, role: 'system', 'content': text,
                                    'created_at': timestamp, 'attachments': attachments}}


def _next_segment(actor, segments, notes):
    readers = books.chapter_readers(segments, notes)
    missing = [i for i, group in enumerate(readers) if actor not in group]
    return {'total': len(segments), 'done': len(segments) - len(missing),
            'segment': missing[0] if missing else None}


async def shelf(actor):
    async with books.get_db() as db:
        db.row_factory = books._row_dict
        chapters = await (await db.execute("""
            SELECT c.book_id, c.id, c.chapter_index, c.title AS chapter, c.char_count, c.segments_meta, b.title, b.author
            FROM book_chapters c JOIN books b ON b.book_id=c.book_id
            ORDER BY c.book_id, c.chapter_index
        """)).fetchall()
        notes = await (await db.execute("""
            SELECT book_id, chapter_index, segment_index, annotator, COALESCE(updated_at,created_at) AS stamp
            FROM book_annotations WHERE COALESCE(annotator,'aion')=?
        """, (actor,))).fetchall()
    by_chapter, latest = {}, {}
    for note in notes:
        bid = note['book_id']
        by_chapter.setdefault((bid, note['chapter_index']), []).append(note)
        latest[bid] = max(latest.get(bid, 0), note['stamp'])
    choices = {}
    for ch in chapters:
        bid = ch['book_id']
        if bid in choices:
            continue
        segments = json.loads(ch.pop('segments_meta'))
        progress = _next_segment(actor, segments, by_chapter.get((bid, ch['chapter_index']), []))
        if progress['segment'] is not None:
            expected = ((ch['char_count'] or 0) + SEGMENT_MAX_CHARS - 1) // SEGMENT_MAX_CHARS
            check_layout = len(segments) != expected or any(s.get('char_count', 0) > SEGMENT_MAX_CHARS for s in segments)
            choices[bid] = dict(ch, **progress, started=bid in latest, check_layout=check_layout)
    recent = max((bid for bid in choices if bid in latest), key=latest.get, default=None)
    keep = [choices.pop(recent)] if recent else []
    offered = keep + random.sample(list(choices.values()), min(3, len(choices)))
    for item in offered:
        if item.pop('check_layout') and await books._refresh_unread_segments(item['book_id'], item['chapter_index']):
            async with books.get_db() as db:
                db.row_factory = books._row_dict
                row = await (await db.execute('SELECT segments_meta FROM book_chapters WHERE id=?', (item['id'],))).fetchone()
                notes = await (await db.execute('SELECT segment_index,annotator FROM book_annotations WHERE book_id=? AND chapter_index=?',
                                                (item['book_id'], item['chapter_index']))).fetchall()
            item.update(_next_segment(actor, json.loads(row['segments_meta']) if row else [], notes))
    return [item for item in offered if item['segment'] is not None]


def shelf_prompt(choices):
    lines = ['自主阅读可选的书（选择 book_read 时，在 JSON 的 book 字段填书前的编号）：']
    for number, item in enumerate(choices, 1):
        progress = '续读' if item['started'] else '初读'
        lines.append(f"{number}. {progress}《{item['title']}》｜{item['author'] or '未知作者'}｜"
                     f"第{item['chapter_index'] + 1}章 {item['chapter']}｜已读{item['done']}/{item['total']}段，"
                     f"这次读第{item['segment'] + 1}段")
    return '\n'.join(lines)


async def choose_book(actor, choices):
    """Pick one book from the shelf; skip the model call when only one book is available."""
    if not choices:
        return None
    if len(choices) == 1:
        return choices[0]
    from autonomy import _ask_actor_json

    prompt = (
        '[自主阅读：选一本书]\n'
        + shelf_prompt(choices)
        + '\n\n请选一本现在想读的书，只返回 JSON：{"book":1,"reason":"一句话理由"}'
    )
    data = await _ask_actor_json(actor, prompt, limit=12)
    number = data.get('book') if isinstance(data, dict) else None
    if isinstance(number, str) and number.strip().isdigit():
        number = int(number.strip())
    if type(number) is int and 1 <= number <= len(choices):
        return choices[number - 1]
    return choices[0]


async def _previous_notes(actor, choice, index):
    async with books.get_db() as db:
        db.row_factory = books._row_dict
        notes = await (await db.execute("""
            SELECT a.chapter_index, a.segment_index, a.summary, a.annotations, c.title, c.paragraphs, c.segments_meta
            FROM book_annotations a JOIN book_chapters c USING (book_id,chapter_index)
            WHERE a.book_id=? AND COALESCE(a.annotator,'aion')=?
              AND (a.chapter_index < ? OR (a.chapter_index=? AND a.segment_index < ?))
            ORDER BY a.chapter_index DESC, a.segment_index DESC LIMIT 3
        """, (choice['book_id'], actor, choice['chapter_index'], choice['chapter_index'], index))).fetchall()
    memories, excerpts = [], []
    for note in reversed(notes):
        segments = json.loads(note['segments_meta'])
        if not 0 <= note['segment_index'] < len(segments):
            continue
        seg = segments[note['segment_index']]
        if actor in seg.get('pending_annotators', []):
            continue
        memories.append(f"{note['title']} 第{note['segment_index'] + 1}段：{note['summary']}")
        body = segment_paragraphs(json.loads(note['paragraphs']), seg)
        for anno in json.loads(note['annotations'] or '[]')[:2]:
            p = anno.get('p')
            if isinstance(p, int) and seg['start_p'] <= p <= seg['end_p']:
                excerpts.append(body[p - seg['start_p']][:300])
    return memories, excerpts


def _excerpt_context(choice, result, body, start, previous_text, peer_note):
    # Only verified source passages are passed to the other reader.
    excerpts = []
    quotes = result.get('quotes')
    if not isinstance(quotes, list):
        quotes = []
    for quote in quotes:
        if isinstance(quote, str) and 0 < len(quote) <= 400 and any(quote in p for p in body + previous_text):
            excerpts.append(quote)
        if len(excerpts) == 2:
            break
    if not excerpts:
        for anno in result['annotations']:
            excerpt = body[anno['p'] - start][:300]
            if excerpt and excerpt not in excerpts:
                excerpts.append(excerpt)
            if len(excerpts) == 2:
                break
    if not excerpts:
        excerpts.append(body[0][:300])
    context = f"群里提到的《{choice['title']}》｜{choice['author'] or '未知作者'}｜{choice['chapter']}\n相关原文：\n" + '\n'.join(excerpts)
    if peer_note:
        context += '\n你读这段时留下的感想：' + peer_note
    return context


async def read_one(actor, choice):
    from autonomy import _actor_label, _ask_actor_json, _latest_group_room_id, _run_role_chat, _save_private_message

    bid, ch, index = choice['book_id'], choice['chapter_index'], choice['segment']
    peer = 'connor' if actor == 'aion' else 'aion'
    await books._refresh_unread_segments(bid, ch)
    lock = books._annotating_locks.setdefault(f'{bid}:{ch}:{index}', asyncio.Lock())
    async with lock:
        async with books.get_db() as db:
            db.row_factory = books._row_dict
            row = await (await db.execute("""
                SELECT paragraphs,segments_meta FROM book_chapters WHERE id=? AND book_id=? AND chapter_index=?
            """, (choice['id'], bid, ch))).fetchone()
            notes = await (await db.execute("""
                SELECT segment_index,annotator,summary FROM book_annotations WHERE book_id=? AND chapter_index=?
            """, (bid, ch))).fetchall()
        if not row:
            return {'skipped': '所选章节已更新'}
        segments = json.loads(row['segments_meta'])
        readers = books.chapter_readers(segments, notes)
        if index >= len(segments) or actor in readers[index]:
            return {'skipped': '这一段已经读过'}
        seg = segments[index]
        paragraphs = json.loads(row['paragraphs'])
        start, end = seg['start_p'], seg['end_p']
        memories, previous_text = await _previous_notes(actor, choice, index)
        peer_name = _actor_label(peer)
        group_room = await _latest_group_room_id()
        prompt = (
            f"你独自翻开《{choice['title']}》，作者 {choice['author'] or '未知作者'}。"
            f"这是第{ch+1}章《{choice['chapter']}》第{index+1}/{len(segments)}段。\n"
            + ('你之前留下的读后感：\n' + '\n'.join(memories) + '\n' if memories else '')
            + ('上文你批注过的原文：\n' + '\n'.join(previous_text) + '\n' if previous_text else '')
            + build_annotate_text(paragraphs, start, end, seg) + '\n'
            + '有感触的地方可以留批注，用自己的口吻写一小段读后感。读完可以安静收书（none）、'
            + f"在最后活跃的聊天窗口对{_actor_label('user')}说句话（private）"
            + (f"或在家庭群里 @{peer_name}（group）" if group_room else '')
            + '。想聊就写下要发的话和对应的一两句原文。只返回 JSON：'
              '{"annotations":[{"p":段落编号,"type":"批注","text":"感想"}],"summary":"读后感",'
              '"share":"none","message":"","quotes":[]}'
        )
        response = await _ask_actor_json(actor, prompt, limit=12)
        if not isinstance(response.get('summary'), str) or not response['summary'].strip():
            raise ValueError('本段没有返回读后感，保留原书签')
        result = books._annotation_fields(response, start, end)
        if not await books._save_annotations(bid, ch, index, result, actor, expected_chapter_id=choice['id']):
            return {'skipped': '所选章节已更新'}
        count = sum(actor in group for group in readers) + 1
        reading = {'title': choice['title'], 'chapter': choice['chapter'], 'index': index,
                   'done': count, 'total': len(segments), 'summary': result['summary']}

    message = str(response.get('message') or '').strip()
    share = response.get('share')
    if not message or share not in ('group', 'private'):
        return reading
    chapter_title = re.sub(r'^\s*第[\d零〇一二两三四五六七八九十百千]+章[\s：:、.．—-]*', '', choice['chapter'] or '').strip()
    chapter_label = f'第{ch + 1}章' + (f'「{chapter_title}」' if chapter_title else '')
    notice = f"{_actor_label(actor)} 刚读完《{choice['title']}》{chapter_label}（第{index + 1}/{len(segments)}段）"
    try:
        if share == 'private':
            sent = await _save_private_message(actor, message, reading_notice=notice)
        elif group_room:
            if f'@{peer_name}' not in message:
                message = f'@{peer_name} {message}'
            peer_note = next((n['summary'] for n in notes if n['segment_index'] == index
                              and (n['annotator'] or 'aion') == peer and peer in readers[index]), '')
            context = _excerpt_context(choice, {**result, 'quotes': response.get('quotes')},
                                       segment_paragraphs(paragraphs, seg), start, previous_text, peer_note)
            sent = await _run_role_chat(actor, {'message': message, 'reading_context': context,
                                                'reading_notice': notice, 'room_id': group_room})
        else:
            sent = None
        reading['shared'] = bool(sent)
    except Exception as exc:
        reading['share_error'] = f'已保存阅读，分享未完成：{exc}'
    return reading
