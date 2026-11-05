"""Publish confirmed theater prose into the existing reading library."""
import hashlib
import html
import json
import re
import time

from fastapi import HTTPException

import theater_studio as studio
from book import _compute_segments


async def save_doc(db, doc):
    await db.execute('INSERT INTO theater_studio VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                     (doc['id'], doc['kind'], doc['conv_id'], json.dumps(doc, ensure_ascii=False)))


async def get_link(cid, db=None):
    if db is None:
        async with studio.get_db() as connection:
            return await get_link(cid, connection)
    row = await (await db.execute('SELECT data FROM theater_studio WHERE id=?', ('reading_'+cid,))).fetchone()
    if not row:
        return None
    link = json.loads(row[0])
    exists = await (await db.execute('SELECT 1 FROM books WHERE book_id=?', (link['book_id'],))).fetchone()
    return link if exists else None


def signature(chapter):
    return hashlib.sha256(json.dumps([chapter['revision'], chapter['title'], chapter['content']],
                                    ensure_ascii=False).encode()).hexdigest()


def render_chapter(chapter):
    # Paragraph boundaries depend only on prose. A late image cannot renumber annotations.
    lines = [m for m in re.finditer(r'[^\r\n]+', chapter['content']) if m.group().strip()]
    paragraphs = [m.group().strip() for m in lines]
    images = sorted((im for im in chapter.get('images', [])
                     if im.get('status') == 'ready' and im.get('url', '').startswith('/uploads/')),
                    key=lambda im: im.get('after', 0))
    rendered = []
    for i, (line, text) in enumerate(zip(lines, paragraphs)):
        rendered.append(f'<p data-p="{i}">{html.escape(text)}</p>')
        while images and (images[0].get('after', 0) <= line.end() or i == len(lines)-1):
            image = images.pop(0)
            rendered.append(f'<div class="book-img"><img src="{html.escape(image["url"], quote=True)}" alt="故事插画" loading="lazy"></div>')
    return paragraphs, ''.join(rendered)


async def publish(db, link, chapter):
    """Caller owns the transaction. Return reading indices whose speech was invalidated."""
    previous = link['chapters'].get(chapter['id'])
    if previous and previous['signature'] == signature(chapter):
        return []
    index = previous['index'] if previous else sum(p['number'] < chapter['number'] for p in link['chapters'].values())
    paragraphs, rendered = render_chapter(chapter)
    segments = _compute_segments(paragraphs)
    bid = link['book_id']
    invalidated = [index] if previous else []
    if not previous:
        # An older chapter may have been a draft on initial import. Insert in story order.
        shifted = sorted((p for p in link['chapters'].values() if p['index'] >= index),
                         key=lambda p: p['index'], reverse=True)
        for item in shifted:
            invalidated.append(item['index'])
            for table in ('book_chapters', 'book_annotations', 'book_highlights'):
                await db.execute(f'UPDATE {table} SET chapter_index=chapter_index+1 WHERE book_id=? AND chapter_index=?',
                                 (bid, item['index']))
            item['index'] += 1
        if shifted:
            await db.execute('UPDATE books SET current_chapter=current_chapter+1 WHERE book_id=? AND current_chapter>=?',
                             (bid, index))
    if previous:
        for table in ('book_annotations', 'book_highlights', 'book_chapters'):
            await db.execute(f'DELETE FROM {table} WHERE book_id=? AND chapter_index=?', (bid, index))
        await db.execute('UPDATE books SET current_paragraph=0 WHERE book_id=? AND current_chapter=?', (bid, index))
    # A fresh row ID is also the reading version, used to reject late annotation results.
    await db.execute('''INSERT INTO book_chapters
        (book_id,chapter_index,title,html_content,text_content,paragraphs,char_count,segment_count,segments_meta)
        VALUES (?,?,?,?,?,?,?,?,?)''', (bid, index, chapter['title'], rendered, '\n'.join(paragraphs),
        json.dumps(paragraphs, ensure_ascii=False), sum(map(len, paragraphs)), len(segments),
        json.dumps(segments, ensure_ascii=False)))
    link['chapters'][chapter['id']] = dict(index=index, number=chapter['number'], signature=signature(chapter))
    await db.execute('UPDATE books SET total_chapters=? WHERE book_id=?', (len(link['chapters']), bid))
    return invalidated


async def add_book(cid, chapter_id=None):
    async with studio.lock('planning_'+cid):
        title, _, _ = await studio.conversation(cid)
        book = await studio.load(cid)
        if not book or book.get('mode') != 'novel':
            raise HTTPException(409, '只有小说可以加入陪伴阅读')
        chapters = sorted((c for c in await studio.rows(cid, 'chapter')
                           if c['status'] == 'ready' and c.get('content', '').strip()), key=lambda c: c['number'])
        target = next((c for c in chapters if c['id'] == chapter_id), None)
        if chapter_id and (not target or chapter_id in studio._writing):
            raise HTTPException(409, '请先确认本章已写完，再邀请共读')
        from theater_planning import persona_for
        person = await persona_for(cid)
        async with studio.get_db() as db:
            await db.execute('BEGIN IMMEDIATE')
            link = await get_link(cid, db)
            if link:
                if target:
                    invalidated = await publish(db, link, target)
                    await save_doc(db, link)
                    await db.commit()
                    if invalidated:
                        from routes.book import discard_chapter_speech
                        for index in invalidated:
                            await discard_chapter_speech(link['book_id'], index)
                return link
            if not chapters:
                raise HTTPException(409, '完成第一章后可加入')
            link = dict(id='reading_'+cid, kind='reading_link', conv_id=cid,
                        book_id=studio.uid('theater_'), chapters={})
            await db.execute('INSERT INTO books(book_id,title,author,total_chapters,import_time) VALUES(?,?,?,?,?)',
                             (link['book_id'], title, (person or {}).get('name') or '小剧场', 0, time.time()))
            for chapter in chapters:
                await publish(db, link, chapter)
            await save_doc(db, link)
            await db.commit()
        return link


async def chapter_link(key):
    """Resolve a stable theater chapter to its confirmed reading version, without creating it."""
    chapter = await studio.load(key)
    if not chapter or chapter.get('kind') != 'chapter':
        raise HTTPException(404, '章节不存在')
    async with studio.lock('planning_'+chapter['conv_id']):
        chapter = await studio.load(key)
        if not chapter or chapter['status'] != 'ready' or key in studio._writing:
            return None
        async with studio.get_db() as db:
            link = await get_link(chapter['conv_id'], db)
            published = link and link['chapters'].get(key)
            if not published or published['signature'] != signature(chapter):
                return None
            row = await (await db.execute(
                'SELECT id FROM book_chapters WHERE book_id=? AND chapter_index=?',
                (link['book_id'], published['index']))).fetchone()
            if not row:
                return None
            return dict(book_id=link['book_id'], chapter_index=published['index'], chapter_id=row[0])


async def complete(key, completed):
    # The source status and published chapter commit together; failures remain retryable.
    async with studio.lock(key):
        chapter = await studio.load(key)
        chapter.update(status='ready' if completed else 'draft', error='')
        invalidated = []
        async with studio.get_db() as db:
            await db.execute('BEGIN IMMEDIATE')
            link = await get_link(chapter['conv_id'], db)
            if completed and link:
                invalidated = await publish(db, link, chapter)
                await save_doc(db, link)
            await save_doc(db, chapter)
            await db.commit()
        if invalidated:
            from routes.book import discard_chapter_speech
            for index in invalidated:
                await discard_chapter_speech(link['book_id'], index)
        return chapter


async def sync_images(key):
    initial = await studio.load(key)
    if not initial:
        return
    async with studio.lock('planning_'+initial['conv_id']), studio.lock(key):
        chapter = await studio.load(key)
        if not chapter or chapter['status'] != 'ready':
            return
        async with studio.get_db() as db:
            await db.execute('BEGIN IMMEDIATE')
            link = await get_link(chapter['conv_id'], db)
            published = link and link['chapters'].get(key)
            if not published or published['signature'] != signature(chapter):
                return
            _, rendered = render_chapter(chapter)
            await db.execute('UPDATE book_chapters SET html_content=? WHERE book_id=? AND chapter_index=?',
                             (rendered, link['book_id'], published['index']))
            await db.commit()
