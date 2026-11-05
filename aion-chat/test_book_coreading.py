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
        self.assertEqual(book._compute_segments(['x' * 15000])[0]['char_count'], 15000)


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

    async def test_upgrade_only_unread_chapters(self):
        await self.setup_reading()
        old = [dict(start_p=0,end_p=49,char_count=5000,status='not_sent'),
               dict(start_p=50,end_p=99,char_count=5000,status='not_sent'),
               dict(start_p=100,end_p=108,char_count=900,status='not_sent')]
        await self.set_segments(old)
        self.assertEqual(len((await api.get_chapter(self.bid, 0))['chapter']['segments_meta']), 2)
        await self.set_segments(old)
        await api._save_annotations(self.bid, 0, 0, dict(annotations=[],summary='keep'))
        self.assertEqual((await api.get_chapter(self.bid, 0))['chapter']['segments_meta'], old)

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
