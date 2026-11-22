"""Balanced batches and resumable co-reading, using a temporary DB and fake AI."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import book
import test_theater_reading as fixtures
from routes import book as api


class SegmentTests(unittest.TestCase):
    def test_balanced_natural_boundaries(self):
        for size, count in ((10900, 2), (12000, 2), (12900, 2), (15000, 3), (18000, 3)):
            paragraphs = ['x' * 100] * (size // 100)
            segments = book._compute_segments(paragraphs)
            self.assertEqual(len(segments), count)
            self.assertLessEqual(max(s['char_count'] for s in segments) - min(s['char_count'] for s in segments), 100)
            self.assertEqual([i for s in segments for i in range(s['start_p'], s['end_p'] + 1)], list(range(len(paragraphs))))
        self.assertEqual(book._compute_segments([]), [])
        paragraphs = ['甲。' * 7500]
        pieces = book._compute_segments(paragraphs)
        self.assertEqual(len(pieces), 3)
        self.assertEqual(''.join(book.segment_paragraphs(paragraphs, p)[0] for p in pieces), paragraphs[0])
        self.assertTrue(all(p['char_count'] <= 6500 for p in pieces))


class CoreadingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ReadingPublishTests.asyncSetUp
    asyncTearDown = fixtures.ReadingPublishTests.asyncTearDown
    prepare = fixtures.ReadingPublishTests.prepare
    fetch = fixtures.ReadingPublishTests.fetch

    async def setup_reading(self):
        await self.prepare()
        await fixtures.studio.change('chapter', content='\n'.join(['x' * 100] * 109), status='ready')
        self.bid = (await fixtures.studio.add_to_reading('book'))['book_id']
        for p in (patch.object(api, 'get_db', fixtures.studio.get_db),
                  patch.object(api, 'load_worldbook', return_value={}),
                  patch.object(api, 'load_chatroom_config', return_value={}),
                  patch.object(api, '_get_chat_context_merged', AsyncMock(return_value=[])),
                  patch.object(api, '_get_prev_summaries', AsyncMock(return_value=[])),
                  patch.object(api, '_is_connor_available', AsyncMock(return_value=True)),
                  patch.object(api, 'MODELS', {'test': {}}),
                  patch.object(api, 'DEFAULT_MODEL', 'test')):
            p.start(); self.patches.append(p)

    async def set_segments(self, segments):
        async with fixtures.studio.get_db() as db:
            await db.execute('UPDATE book_chapters SET segments_meta=?', (json.dumps(segments),))
            await db.commit()

    async def test_night_reading_fills_one_gap_for_only_the_waking_reader(self):
        import autonomy
        await self.setup_reading()
        await api._save_annotations(self.bid, 0, 1, dict(annotations=[], summary='已经共读过'))
        decision = AsyncMock(side_effect=[
            {'action': 'book_read'},
            {'annotations': [{'p': 0, 'text': '这句有意思'}], 'summary': '留在书页里',
             'share': 'group', 'message': '开场就埋伏笔？', 'quotes': ['x' * 55]},
        ])
        with patch.object(autonomy, 'get_actor_config', AsyncMock(return_value={'actions': {'book_read': True}})), \
             patch.object(autonomy, 'recent_niche_index', AsyncMock(return_value=[])), \
             patch.object(autonomy, '_recent_actor_actions', AsyncMock(return_value=[])), \
             patch.object(autonomy, '_ask_actor_json', decision), \
             patch.object(autonomy, '_latest_group_room_id', AsyncMock(return_value='group')), \
             patch.object(autonomy, '_save_autonomy_chatroom_message', AsyncMock()) as group, \
             patch('routes.chatroom._load_room_and_messages', AsyncMock(return_value=({}, []))), \
             patch('routes.chatroom._reply_connor', AsyncMock()) as reply, \
             patch.object(autonomy, 'append_idle_event', AsyncMock()):
            result = await autonomy._run_actor_once('aion', manual=True)
            self.assertEqual(result['action'], 'book_read')
            self.assertTrue(result['ok'], result)
            self.assertEqual(decision.await_count, 2)
            self.assertIn('自主阅读（书架上有 1 本可读', decision.await_args_list[0].args[1])
            self.assertNotIn('Test', decision.await_args_list[0].args[1])
            self.assertIn('@', group.await_args.args[2])
            self.assertNotIn('x', group.await_args.args[2])
            self.assertIn('刚读完《Test》第1章', group.await_args.kwargs['reading_notice'])
            self.assertIn('x' * 55, reply.await_args.kwargs['reading_context'])
            self.assertNotIn('x' * 56, reply.await_args.kwargs['reading_context'])
        detail = await api.get_book(self.bid)
        self.assertEqual(detail['chapters'][0]['read_counts'], {'aion': 2, 'connor': 0})
        self.assertEqual(detail['book']['current_chapter'], 0)
        self.assertEqual(detail['book']['current_paragraph'], 0)
        self.assertFalse(any(s['status'] == 'done' for s in (await api.get_chapter(self.bid, 0))['chapter']['segments_meta']))
        ok = AsyncMock(return_value=('{"annotations":[],"summary":"一起读"}', ''))
        with patch.object(api, '_run_aion_annotation', AsyncMock()) as already_read, patch.object(api, '_run_connor_annotation', ok):
            response = await api.annotate_all_segments(self.bid, 0, api.AnnotateAllRequest())
            _ = [chunk async for chunk in response.body_iterator]
            already_read.assert_not_awaited()
            self.assertEqual(ok.await_count, 2)
        self.assertEqual((await api.get_book(self.bid))['chapters'][0]['read_counts'], {'aion': 2, 'connor': 2})

    async def test_multi_book_shelf_uses_two_step_book_choice(self):
        import autonomy
        await self.setup_reading()
        paragraphs = ['第二本书内容']
        segments = book._compute_segments(paragraphs)
        async with fixtures.studio.get_db() as db:
            await db.execute(
                "INSERT INTO books(book_id,title,author,total_chapters,import_time) VALUES('book_two','另一本书','作者乙',1,2)"
            )
            await db.execute(
                "INSERT INTO book_chapters(book_id,chapter_index,title,html_content,text_content,paragraphs,char_count,segment_count,segments_meta) "
                "VALUES('book_two',0,'序章','','第二本书内容',?,?,?,?)",
                (json.dumps(paragraphs, ensure_ascii=False), 6, len(segments), json.dumps(segments)),
            )
            await db.commit()
        decision = AsyncMock(side_effect=[
            {'action': 'book_read', 'reason': '想看点别的'},
            {'book': 2, 'reason': '挑第二本'},
            {'annotations': [], 'summary': '读完了第二本', 'share': 'none'},
        ])
        with patch.object(autonomy, 'get_actor_config', AsyncMock(return_value={'actions': {'book_read': True}})), \
             patch.object(autonomy, 'recent_niche_index', AsyncMock(return_value=[])), \
             patch.object(autonomy, '_recent_actor_actions', AsyncMock(return_value=[])), \
             patch.object(autonomy, '_ask_actor_json', decision), \
             patch.object(autonomy, '_latest_group_room_id', AsyncMock(return_value=None)), \
             patch.object(autonomy, 'append_idle_event', AsyncMock()):
            result = await autonomy._run_actor_once('aion', manual=True)
        self.assertTrue(result['ok'])
        self.assertEqual(3, decision.await_count)
        self.assertNotIn('另一本书', decision.await_args_list[0].args[1])
        self.assertIn('自主阅读可选的书', decision.await_args_list[1].args[1])
        self.assertIn('另一本书', decision.await_args_list[1].args[1])
        self.assertIn(result['result']['title'], {'Test', '另一本书'})

    async def test_separate_bookmarks_resume_and_private_share(self):
        import autonomy
        from autonomy_reading import shelf, read_one
        await self.setup_reading()
        first = (await shelf('connor'))[0]
        with patch.object(autonomy, '_latest_group_room_id', AsyncMock(return_value=None)), \
             patch.object(autonomy, '_ask_actor_json', AsyncMock(side_effect=[
                 {'summary': '前半段的伏笔', 'share': 'none'},
                 {'summary': '原来如此', 'share': 'private', 'message': '我找到伏笔了'},
             ])) as read, patch.object(autonomy, '_save_private_message', AsyncMock(return_value={'id':'pm'})) as dm:
            await read_one('connor', first)
            dm.assert_not_awaited()
            next_part = (await shelf('connor'))[0]
            self.assertEqual((next_part['segment'], next_part['done']), (1, 1))
            self.assertTrue(next_part['started'])
            self.assertEqual((await shelf('aion'))[0]['segment'], 0)
            result = await read_one('connor', next_part)
            self.assertIn('前半段的伏笔', read.await_args.args[1])
            dm.assert_awaited_once()
            self.assertEqual(dm.await_args.args, ('connor', '我找到伏笔了'))
            self.assertFalse(dm.await_args.kwargs.get('force_private'))
            self.assertIn('刚读完《Test》第1章', dm.await_args.kwargs['reading_notice'])
            self.assertIn('第2/2段', dm.await_args.kwargs['reading_notice'])
            self.assertTrue(result['shared'])
            self.assertFalse(await shelf('connor'))
            self.assertEqual((await read_one('connor', first))['skipped'], '这一段已经读过')
            self.assertEqual(read.await_count, 2)

    async def test_reading_notice_follows_the_reply_into_chat_and_model_history(self):
        import autonomy
        import context_builder
        from routes import chatroom
        db_factory = fixtures.studio.get_db
        async with db_factory() as db:
            await db.executescript("""
                CREATE TABLE conversations(id TEXT, model TEXT, updated_at REAL);
                CREATE TABLE messages(id TEXT PRIMARY KEY,conv_id TEXT,role TEXT,content TEXT,created_at REAL,attachments TEXT);
                CREATE TABLE chatroom_rooms(id TEXT,type TEXT,updated_at REAL);
                CREATE TABLE chatroom_messages(id TEXT PRIMARY KEY,room_id TEXT,sender TEXT,content TEXT,created_at REAL,attachments TEXT,reasoning_content TEXT);
                INSERT INTO conversations VALUES('active','model',1),('newer','model',9);
                INSERT INTO messages VALUES('user1','active','user','在这里聊天',2,'[]');
                INSERT INTO chatroom_rooms VALUES('group1','group',3),('solo','connor_1v1',1);
            """)
        with patch.object(autonomy, 'get_db', db_factory), patch.object(chatroom, 'get_db', db_factory), \
             patch.object(context_builder, 'get_db', db_factory), patch('routes.files.export_conversation', AsyncMock()), \
             patch.object(chatroom, 'with_band_vibration_attachment', AsyncMock(return_value=[])), \
             patch.object(chatroom, 'connor_1v1_on_message'), patch.object(chatroom, 'broadcast_synced', AsyncMock()), \
             patch.object(autonomy.manager, 'broadcast', AsyncMock()), \
             patch.object(autonomy.manager, 'get_aion_last_active', side_effect=['private', 'chatroom:group1']), \
             patch.object(autonomy.manager, 'get_connor_last_active', side_effect=['group1', 'solo']):
            for actor, channel in [('aion','active'), ('aion','group1'), ('connor','group1'), ('connor','solo')]:
                notice = f'{actor} 刚读完《来信》第5章「窗边」（第2/3段）'
                sent = await autonomy._save_private_message(actor, '看到窗边那封信了', reading_notice=notice, auto_tts=False)
                self.assertEqual(sent.get('room_id') or sent.get('conv_id'), channel)
                rows = await context_builder.fetch_merged_timeline(actor, 2)
                self.assertEqual([r['content'] for r in rows], [notice, sent['content']])
                self.assertEqual(rows[0]['sender'], 'system')
                self.assertIn({'type':'system_notice_order','before_msg_id':sent['id']}, json.loads(rows[0]['attachments']))
                rendered = context_builder.render_merged_timeline(rows, actor)
                history = '\n'.join(item['content'] for item in rendered)
                self.assertLess(history.index(notice), history.index(sent['content']))

    async def test_upgrade_only_unread_chapters(self):
        await self.setup_reading()
        old = [dict(start_p=0,end_p=49,char_count=5000,status='not_sent'),
               dict(start_p=50,end_p=99,char_count=5000,status='not_sent'),
               dict(start_p=100,end_p=108,char_count=900,status='not_sent')]
        await self.set_segments(old)
        self.assertEqual(len((await api.get_chapter(self.bid, 0))['chapter']['segments_meta']), 2)
        await self.set_segments(old)
        await api._save_annotations(self.bid, 0, 0, dict(annotations=[],summary='keep'))
        self.assertEqual(json.loads((await self.fetch('SELECT segments_meta FROM book_chapters'))[0]['segments_meta']), old)
        self.assertEqual((await api.get_chapter(self.bid, 0))['chapter']['segments_meta'][0]['status'], 'partial')

    async def test_legacy_long_paragraph_preserves_the_other_readers_completed_note(self):
        await self.setup_reading()
        old = [dict(start_p=0, end_p=0, char_count=12000, status='done')]
        async with fixtures.studio.get_db() as db:
            await db.execute('UPDATE book_chapters SET paragraphs=?,segments_meta=?',
                             (json.dumps(['甲。' * 6000]), json.dumps(old)))
            await db.commit()
        await api._save_annotations(self.bid, 0, 0, dict(annotations=[{'p':0,'text':'原批注'}], summary='原感想'))
        chapter = await api.get_chapter(self.bid, 0)
        self.assertEqual(chapter['chapter']['read_counts'], {'aion': 2, 'connor': 0})
        self.assertEqual([n['summary'] for n in chapter['summaries']], ['原感想'])
        self.assertEqual(chapter['annotations'][0]['text'], '原批注')

    async def test_disconnect_saves_each_companion_and_finishes_in_background(self):
        await self.setup_reading()
        release = asyncio.Event()
        async def slow(*args):
            await release.wait()
            return json.dumps(dict(annotations=[], summary='slow')), ''
        fast = AsyncMock(return_value=(json.dumps(dict(annotations=[],summary='fast')), ''))
        slow_mock = AsyncMock(side_effect=slow)
        with patch.object(api, '_run_aion_annotation', slow_mock), patch.object(api, '_run_connor_annotation', fast):
            response = await api.annotate_all_segments(self.bid, 0, api.AnnotateAllRequest())
            stream = response.body_iterator
            try:
                await anext(stream)
                await anext(stream)
                pending = asyncio.create_task(anext(stream))
                for _ in range(100):
                    if await self.fetch("SELECT * FROM book_annotations WHERE annotator='connor'"): break
                    await asyncio.sleep(.01)
                self.assertTrue(await self.fetch("SELECT * FROM book_annotations WHERE annotator='connor'"), 'Fast companion must save without waiting for the other')
                reconnected = await api.annotate_all_segments(self.bid, 0, api.AnnotateAllRequest())
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                await stream.aclose()
                release.set()
                jobs = [j['task'] for j in getattr(api, '_annotation_jobs', {}).values()]
                if jobs: await asyncio.wait_for(asyncio.gather(*jobs), 3)
            self.assertEqual(len(await self.fetch('SELECT * FROM book_annotations')), 4)
            events = [chunk async for chunk in reconnected.body_iterator]
            self.assertIn('"type": "done"', events[-1])
            self.assertEqual(slow_mock.await_count, 2)
            self.assertEqual(fast.await_count, 2, 'Reconnecting must reuse the running job')

    async def test_resume_only_missing_companion_and_keep_failure_unfinished(self):
        await self.setup_reading()
        await api._save_annotations(self.bid, 0, 0, dict(annotations=[], summary='keep'))
        ok = AsyncMock(return_value=(json.dumps(dict(annotations=[],summary='new')), ''))
        bad = AsyncMock(return_value=('', 'test unavailable'))
        async def consume(reread=False):
            response = await api.annotate_all_segments(self.bid, 0, api.AnnotateAllRequest(reread=reread))
            return [chunk async for chunk in response.body_iterator]
        with patch.object(api, '_run_aion_annotation', ok), patch.object(api, '_run_connor_annotation', bad):
            await consume()
        self.assertEqual(ok.await_count, 1)
        chapter = await api.get_chapter(self.bid, 0)
        self.assertTrue(all(s['status'] != 'done' for s in chapter['chapter']['segments_meta']))
        ok.reset_mock()
        with patch.object(api, '_run_aion_annotation', bad), patch.object(api, '_run_connor_annotation', ok):
            await consume()
        self.assertEqual(ok.await_count, 2)
        self.assertEqual((await self.fetch("SELECT summary FROM book_annotations WHERE segment_index=0 AND annotator='aion'"))[0]['summary'], 'keep')
        self.assertTrue(all(s['status'] == 'done' for s in (await api.get_chapter(self.bid, 0))['chapter']['segments_meta']))
        # A failed reread must not mistake the older saved result for this attempt.
        with patch.object(api, '_run_aion_annotation', bad), patch.object(api, '_run_connor_annotation', ok):
            await consume(reread=True)
        ok.reset_mock(); bad.reset_mock()
        with patch.object(api, '_run_aion_annotation', ok), patch.object(api, '_run_connor_annotation', bad):
            await consume()
        self.assertEqual(ok.await_count, 2)
        self.assertEqual(bad.await_count, 0)
        self.assertTrue(all(s['status'] == 'done' for s in (await api.get_chapter(self.bid, 0))['chapter']['segments_meta']))
        with patch.object(api, '_run_aion_annotation', bad), patch.object(api, '_run_connor_annotation', bad):
            await consume(reread=True)
        with patch.object(api, '_is_connor_available', AsyncMock(return_value=False)), patch.object(api, '_run_aion_annotation', ok):
            await consume()
        self.assertTrue(all(s['pending_annotators'] == ['connor'] for s in (await api.get_chapter(self.bid, 0))['chapter']['segments_meta']))
        ok.reset_mock(); bad.reset_mock()
        with patch.object(api, '_run_aion_annotation', bad), patch.object(api, '_run_connor_annotation', ok):
            await consume()
        self.assertEqual(bad.await_count, 0)
        self.assertEqual(ok.await_count, 2)


if __name__ == '__main__':
    unittest.main()
