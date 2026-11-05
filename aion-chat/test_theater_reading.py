"""Small integration checks for publishing finished theater chapters (no AI calls)."""
import json
import unittest
from unittest.mock import patch

from test_theater_studio import StudioTests
import theater_studio as studio
import theater_reading as bridge


class ReadingPublishTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = StudioTests.asyncSetUp
    asyncTearDown = StudioTests.asyncTearDown

    async def prepare(self):
        # Load only reading schema, without unrelated application startup side effects.
        import ast
        import database
        from pathlib import Path
        tree = ast.parse(Path(database.__file__).read_text(encoding='utf-8'))
        async with studio.get_db() as db:
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    sql = node.value.strip()
                    if sql.startswith(('CREATE TABLE IF NOT EXISTS books (', 'CREATE TABLE IF NOT EXISTS book_chapters (',
                                       'CREATE TABLE IF NOT EXISTS book_annotations (', 'CREATE TABLE IF NOT EXISTS book_highlights (')):
                        # Match the production migration that permits both companions.
                        sql = sql.replace('ON DELETE CASCADE,\n                UNIQUE(book_id, chapter_index, segment_index)', 'ON DELETE CASCADE')
                        await db.execute(sql)
            await db.execute("ALTER TABLE book_annotations ADD COLUMN annotator TEXT DEFAULT 'aion'")
            await db.execute('CREATE UNIQUE INDEX idx_book_anno_unique ON book_annotations(book_id, chapter_index, segment_index, annotator)')
            await db.execute("ALTER TABLE book_highlights ADD COLUMN annotator TEXT DEFAULT 'aion'")
            await db.execute("ALTER TABLE book_highlights ADD COLUMN connor_answer TEXT DEFAULT ''")
            await db.commit()

    async def fetch(self, sql, args=()):
        async with studio.get_db() as db:
            db.row_factory = __import__('aiosqlite').Row
            return [dict(r) for r in await (await db.execute(sql, args)).fetchall()]

    async def test_initial_eight_chapters_then_append_only_on_completion(self):
        await self.prepare()
        template = await studio.load('chapter')
        for n in range(1, 10):
            await studio.save(dict(template, id=f'chapter{n}', number=n,
                                   content=f'正文{n}', status='ready' if n <= 8 else 'draft'))
        link = await studio.add_to_reading('book')
        again = await studio.add_to_reading('book')
        self.assertEqual(link['book_id'], again['book_id'])
        self.assertEqual(len(await self.fetch('SELECT * FROM book_chapters')), 8)
        await studio.set_chapter_completion('chapter9', studio.ChapterCompletion(completed=True))
        self.assertEqual(len(await self.fetch('SELECT * FROM book_chapters')), 9)

    async def test_inline_reading_requires_confirmation_and_reuses_saved_notes(self):
        await self.prepare()
        await studio.change('chapter', content='第一段\n第二段', status='draft')
        with self.assertRaises(studio.HTTPException) as failure:
            await studio.start_chapter_reading('chapter')
        self.assertEqual(failure.exception.status_code, 409)
        self.assertFalse(await self.fetch('SELECT * FROM books'))
        await studio.set_chapter_completion('chapter', studio.ChapterCompletion(completed=True))
        first = await studio.start_chapter_reading('chapter')
        bid = first['book_id']
        async with studio.get_db() as db:
            await db.execute("INSERT INTO book_annotations(book_id,chapter_index,segment_index,annotations,summary,created_at) VALUES(?,0,0,'[]','保留读后感',1)", (bid,))
            await db.commit()
        again = await studio.start_chapter_reading('chapter')
        self.assertEqual(first, again)
        self.assertEqual((await self.fetch('SELECT summary FROM book_annotations'))[0]['summary'], '保留读后感')
        self.assertEqual(await studio.get_chapter_reading('chapter'), first)
        await studio.edit_chapter('chapter', studio.ChapterPatch(content='还没确认的新稿'))
        self.assertIsNone(await studio.get_chapter_reading('chapter'))
        with self.assertRaises(studio.HTTPException):
            await studio.start_chapter_reading('chapter')
        self.assertEqual((await self.fetch('SELECT text_content FROM book_chapters'))[0]['text_content'], '第一段\n第二段')

    async def test_replacement_clears_only_its_notes_and_resets_position(self):
        await self.prepare()
        await studio.change('chapter', content='第一段\n第二段', status='ready')
        link = await studio.add_to_reading('book')
        bid = link['book_id']
        old = (await self.fetch('SELECT * FROM book_chapters'))[0]
        async with studio.get_db() as db:
            await db.execute("INSERT INTO book_annotations(book_id,chapter_index,segment_index,annotations,summary,created_at) VALUES(?,0,0,'[]','旧点评',1)", (bid,))
            await db.execute("INSERT INTO book_highlights(book_id,chapter_index,selected_text,start_p,start_offset,end_p,end_offset,question,answer,created_at) VALUES(?,0,'第一段',0,0,0,3,'问','答',1)", (bid,))
            await db.execute('UPDATE books SET current_paragraph=7 WHERE book_id=?', (bid,))
            await db.commit()
        await studio.edit_chapter('chapter', studio.ChapterPatch(content='新版正文'))
        self.assertEqual((await self.fetch('SELECT * FROM book_chapters'))[0]['text_content'], old['text_content'])
        await studio.set_chapter_completion('chapter', studio.ChapterCompletion(completed=True))
        new = (await self.fetch('SELECT * FROM book_chapters'))[0]
        self.assertNotEqual(old['id'], new['id'])
        self.assertEqual(new['text_content'], '新版正文')
        self.assertFalse(await self.fetch('SELECT * FROM book_annotations'))
        self.assertFalse(await self.fetch('SELECT * FROM book_highlights'))
        self.assertEqual((await self.fetch('SELECT * FROM books'))[0]['current_paragraph'], 0)
        # A retry of the completion action must not replace the same published version.
        await studio.set_chapter_completion('chapter', studio.ChapterCompletion(completed=True))
        self.assertEqual((await self.fetch('SELECT * FROM book_chapters'))[0]['id'], new['id'])

    async def test_late_images_keep_paragraphs_and_reject_an_unpublished_revision(self):
        await self.prepare()
        await studio.change('chapter', content='第一段文字\n第二段文字', status='ready')
        await studio.add_to_reading('book')
        before = (await self.fetch('SELECT * FROM book_chapters'))[0]
        await studio.change('chapter', images=[dict(status='ready', after=3, url='/uploads/pic.png', prompt='绝密生图词')])
        await bridge.sync_images('chapter')
        after = (await self.fetch('SELECT * FROM book_chapters'))[0]
        self.assertIn('/uploads/pic.png', after['html_content'])
        self.assertNotIn('绝密', after['html_content'] + after['text_content'])
        self.assertEqual((after['id'], after['paragraphs']), (before['id'], before['paragraphs']))
        await studio.change('chapter', revision=2, images=[dict(status='ready', after=3, url='/uploads/new.png')])
        await bridge.sync_images('chapter')
        self.assertEqual((await self.fetch('SELECT * FROM book_chapters'))[0]['html_content'], after['html_content'])

    async def test_late_annotation_cannot_restore_discarded_notes(self):
        await self.prepare()
        from routes import book as reading
        await studio.change('chapter', content='旧文', status='ready')
        link = await studio.add_to_reading('book')
        old = (await self.fetch('SELECT * FROM book_chapters'))[0]
        await studio.edit_chapter('chapter', studio.ChapterPatch(content='新文'))
        await studio.set_chapter_completion('chapter', studio.ChapterCompletion(completed=True))
        with patch.object(reading, 'get_db', studio.get_db):
            saved = await reading._save_annotations(link['book_id'], 0, 0,
                {'annotations':[{'p':0,'text':'旧文点评'}], 'summary':'旧摘要'}, expected_chapter_id=old['id'])
        self.assertFalse(saved)
        self.assertFalse(await self.fetch('SELECT * FROM book_annotations'))

    async def test_later_confirmation_inserts_a_previously_missing_chapter_in_order(self):
        await self.prepare()
        first = await studio.change('chapter', content='一', status='ready')
        await studio.save(dict(first, id='second', number=2, content='二', status='draft'))
        await studio.save(dict(first, id='third', number=3, content='三'))
        await studio.add_to_reading('book')
        await studio.set_chapter_completion('second', studio.ChapterCompletion(completed=True))
        rows = await self.fetch('SELECT text_content FROM book_chapters ORDER BY chapter_index')
        self.assertEqual([r['text_content'] for r in rows], ['一','二','三'])

    async def test_deleted_reading_book_stays_deleted_until_explicitly_added_again(self):
        await self.prepare()
        await studio.change('chapter', content='正文', status='ready')
        old = await studio.add_to_reading('book')
        async with studio.get_db() as db:
            await db.execute('DELETE FROM books WHERE book_id=?', (old['book_id'],))
            await db.commit()
        await studio.set_chapter_completion('chapter', studio.ChapterCompletion(completed=True))
        self.assertFalse(await self.fetch('SELECT * FROM books'))
        self.assertIsNone(await bridge.get_link('book'))
        new = await studio.add_to_reading('book')
        self.assertNotEqual(new['book_id'], old['book_id'])

    async def test_speech_starting_during_replacement_is_rejected(self):
        import asyncio
        from routes import book as reading
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_plan(*args):
            entered.set()
            await release.wait()
            return {'signature':'old'}
        with patch.object(reading, '_speech_plan', side_effect=slow_plan):
            task=asyncio.create_task(reading.start_chapter_speech('test-book', 0,
                reading.ChapterSpeechRequest(narrator='aion',signature='old')))
            await entered.wait()
            await reading.discard_chapter_speech('test-book',0)
            release.set()
            with self.assertRaises(studio.HTTPException) as failure:
                await task
            self.assertEqual(failure.exception.status_code,409)

    async def test_late_image_save_cannot_attach_to_a_rewritten_source(self):
        await studio.change('chapter',revision=2,content='新正文')
        saved=await studio.save_chapter_images('chapter',1,images=[{'url':'/uploads/old.png'}])
        self.assertFalse(saved)
        self.assertEqual((await studio.load('chapter'))['images'],[])

    async def test_both_reading_endpoints_tell_both_companions_the_saved_author(self):
        from contextlib import ExitStack
        from unittest.mock import AsyncMock
        from routes import book as reading
        await self.prepare()
        await studio.change('chapter', content='月光落在窗前。', status='ready')
        link = await studio.add_to_reading('book')
        async with studio.get_db() as db:
            await db.execute('UPDATE books SET author=? WHERE book_id=?', ('设定里的作者名',link['book_id']))
            await db.commit()
        with ExitStack() as stack:
            stack.enter_context(patch.object(reading,'get_db',studio.get_db))
            stack.enter_context(patch.object(reading,'_is_connor_available',AsyncMock(return_value=True)))
            stack.enter_context(patch.object(reading,'_get_chat_context_merged',AsyncMock(return_value=[])))
            stack.enter_context(patch.object(reading,'_save_annotations',AsyncMock(return_value=True)))
            stack.enter_context(patch.object(reading,'_update_segment_status',AsyncMock(return_value=True)))
            stack.enter_context(patch.object(reading,'load_worldbook',return_value={'ai_name':'伴侣甲','user_name':'读者'}))
            stack.enter_context(patch.object(reading,'load_chatroom_config',return_value={'connor_name':'伴侣乙'}))
            stack.enter_context(patch.object(reading,'_read_connor_persona',return_value='陪伴阅读'))
            stack.enter_context(patch.object(reading,'MODELS',{'test':{}}))
            stack.enter_context(patch.object(reading,'DEFAULT_MODEL','test'))
            result=(json.dumps({'annotations':[],'summary':'读后感'}),'')
            first=stack.enter_context(patch.object(reading,'_run_aion_annotation',AsyncMock(return_value=result)))
            second=stack.enter_context(patch.object(reading,'_run_connor_annotation',AsyncMock(return_value=result)))
            for endpoint, body in [(reading.annotate_segment,reading.AnnotateRequest(segment_index=0)),
                                   (reading.annotate_all_segments,reading.AnnotateAllRequest())]:
                response=await endpoint(link['book_id'],0,body)
                async for _ in response.body_iterator:
                    pass
                for provider in (first,second):
                    messages=provider.call_args.args[0]
                    context='\n'.join(m['content'] for m in messages)
                    self.assertIn('作者：设定里的作者名',context)
                    self.assertIn('月光落在窗前。',context)
                    self.assertIn('200字以下',context)
                    self.assertNotIn('100-300字',context)
                    self.assertNotIn('通常 3-8 条',context)
                    self.assertFalse(any(m['role']=='assistant' for m in messages))


class ReadingPromptTests(unittest.TestCase):
    def test_epub_keeps_its_reading_prompt_without_a_fabricated_reply(self):
        from routes import book as reading
        with patch.object(reading,'load_chatroom_config',return_value={'connor_name':'伴侣乙'}), \
             patch.object(reading,'_read_connor_persona',return_value='人设'):
            for build in (reading._build_annotate_messages,reading._build_connor_annotate_messages):
                messages=build({'ai_name':'伴侣甲','user_name':'读者'},'正文','第一章',[],0,0,book_author='作者')
                context='\n'.join(m['content'] for m in messages)
                self.assertIn('100-300字',context)
                self.assertNotIn('小剧场创作',context)
                self.assertFalse(any(m['role']=='assistant' for m in messages))


if __name__ == '__main__':
    unittest.main()
