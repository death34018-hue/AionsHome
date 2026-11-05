"""Digest/profile boundary checks with real persistence and no external model calls."""
import json
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import AsyncMock

import aiosqlite
import post_sentinel


class ProfileDigestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import profile_digest
        self.digest = profile_digest
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'chat.db'
        self.db_patch = patch.object(post_sentinel, 'get_db', self.connect)
        self.enabled_patch = patch.object(post_sentinel, 'enabled', return_value=True)
        self.db_patch.start()
        self.enabled_patch.start()
        async with self.connect() as db:
            await db.execute('CREATE TABLE messages (id TEXT,role TEXT,content TEXT,created_at REAL)')
            await db.execute('CREATE TABLE chatroom_messages (id TEXT,sender TEXT,content TEXT,created_at REAL)')
            await db.commit()
        await post_sentinel.ensure_schema()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.enabled_patch.stop()
        self.tmp.cleanup()

    def connect(self):
        return aiosqlite.connect(self.db_path)

    async def batch(self):
        ts = time.time()-3600
        text = '此前的创作经历。'*100 + '最后一条告别视频播放量达到37万。'
        async with self.connect() as db:
            await db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?)', ('one', 'user', text, ts))
            await db.commit()
        return [{'id': 'one', '_source_id': 'private:one', 'role': 'user', 'content': text, 'created_at': ts}]

    def result(self):
        return {'profile_updates': [{'category': 'recent', 'reason': '正在创作有后续', 'subcategory': 'projects', 'topic': '告别视频',
                'content': '最后一条告别视频达到37万播放', 'sources': ['private:one'],
                'importance': 0.85, 'keywords': ['告别视频']}], 'memories': []}

    async def test_persisted_result_replays_after_restart_and_shared_digest_is_idempotent(self):
        context = await self.digest.prepare(await self.batch(), 'Sol', 'Mira')
        self.assertIn('以恋人的身份', context['prompt'])
        self.assertIn('Mira', context['prompt'])
        with patch.object(self.digest, 'apply_pending', side_effect=RuntimeError('temporary failure')):
            await self.digest.store_result(context, self.result())
        self.assertEqual((await post_sentinel.snapshot())['digest_pending'], 1)
        await self.digest.apply_pending()
        data = await post_sentinel.snapshot()
        self.assertEqual(data['digest_pending'], 0)
        self.assertEqual(len(data['entries']), 1)
        self.assertIn('告别视频', data['entries'][0]['content'])
        self.assertEqual(data['entries'][0]['sources'], ['private:one'])
        self.assertLess(data['entries'][0]['source_ts'], time.time()-3500)
        await self.digest.store_result(context, self.result())
        self.assertEqual(len((await post_sentinel.snapshot())['entries']), 1)

    async def test_delayed_digest_does_not_overwrite_newer_manual_correction(self):
        data = await post_sentinel.save_manual({'entries': [
            {'key': 'event', 'category': 'recent', 'reason': '正在创作有后续', 'subcategory': 'projects', 'content': '旧描述'}]}, 0)
        context = await self.digest.prepare(await self.batch(), 'Sol', 'Mira')
        await post_sentinel.save_manual({'entries': [
            {'key': 'event', 'category': 'recent', 'reason': '正在创作有后续', 'subcategory': 'projects', 'content': '本人最新纠正'}]}, data['revision'])
        result = self.result()
        result['profile_updates'][0]['ref'] = 'P1'
        await self.digest.store_result(context, result)
        self.assertEqual((await post_sentinel.snapshot())['entries'][0]['content'], '本人最新纠正')

    async def test_invalid_result_keeps_original_evidence_for_next_digest_repair(self):
        group = await self.batch()
        context = await self.digest.prepare(group, 'Sol', 'Mira')
        await self.digest.store_result(context, {'memories': []})
        self.assertEqual((await post_sentinel.snapshot())['digest_pending'], 1)
        retry = await self.digest.prepare([], 'Sol', 'Mira')
        self.assertIn(group[0]['content'], retry['prompt'])
        await self.digest.store_result(retry, self.result())
        self.assertEqual((await post_sentinel.snapshot())['digest_pending'], 0)
        self.assertEqual(len((await post_sentinel.snapshot())['entries']), 1)

    async def test_single_profile_update_object_is_saved(self):
        context = await self.digest.prepare(await self.batch(), 'Sol', 'Mira')
        result = self.result()
        result['profile_updates'] = result['profile_updates'][0]
        await self.digest.store_result(context, result)
        data = await post_sentinel.snapshot()
        self.assertEqual(data['digest_pending'], 0)
        self.assertEqual(len(data['entries']), 1)
        self.assertEqual(data['entries'][0]['content'], '最后一条告别视频达到37万播放')

    async def test_edited_source_is_not_committed_and_ai_speech_cannot_be_evidence(self):
        context = await self.digest.prepare(await self.batch(), 'Sol', 'Mira')
        async with self.connect() as db:
            await db.execute("UPDATE messages SET content='已编辑纠正' WHERE id='one'")
            await db.commit()
        await self.digest.store_result(context, self.result())
        self.assertEqual((await post_sentinel.snapshot())['entries'], [])

    async def test_edited_failed_retry_does_not_poison_new_batch(self):
        context = await self.digest.prepare(await self.batch(), 'Sol', 'Mira')
        await self.digest.store_result(context, {})
        async with self.connect() as db:
            await db.execute("UPDATE messages SET content='已纠正' WHERE id='one'")
            await db.execute("INSERT INTO messages VALUES ('two','user','周六一起挑图',?)", (time.time(),))
            await db.commit()
        context = await self.digest.prepare([{'_source_id': 'private:two', 'role': 'user', 'content': '周六一起挑图'}], 'Sol', 'Mira')
        result = {'profile_updates': [{'category': 'recent', 'kind':'task', 'reason': '定下挑图时间', 'content': '周六一起挑图', 'sources': ['private:two']}]}
        await self.digest.store_result(context, result)
        data = await post_sentinel.snapshot()
        self.assertEqual(data['digest_pending'], 0)
        self.assertEqual(data['entries'][0]['content'], '周六一起挑图')

    async def test_digest_checks_budget_after_all_reductions(self):
        data = await post_sentinel.save_manual({'entries': [
            {'key':'a','category':'recent','content':'正在做甲事'},
            {'key':'b','category':'recent','content':'正在做乙事'}]},0)
        group = await self.batch()
        async with self.connect() as db:
            await db.execute('UPDATE messages SET created_at=? WHERE id=?',(time.time()+1,'one'))
            await db.commit()
        context = await self.digest.prepare(group, 'Sol','Mira')
        result = {'profile_updates':[
            {'ref':'P1','content':'甲的新阶段','sources':['private:one']},
            {'ref':'P2','content':'乙已结束','status':'done','sources':['private:one']} ]}
        def check(document):
            # The intermediate first delta is too large; final completion frees the budget.
            if document['entries']['a']['content']=='甲的新阶段' and document['entries']['b'].get('status')!='done':
                raise ValueError('intermediate budget')
        with patch.object(post_sentinel,'_check_budget',side_effect=check):
            await self.digest.store_result(context,result)
        data = await post_sentinel.snapshot()
        self.assertEqual(data['digest_pending'],0)
        self.assertEqual(data['entries'][0]['content'],'甲的新阶段')

    async def test_initialization_only_reads_last_week(self):
        import profile_initialize
        now = time.time()
        async with self.connect() as db:
            await db.executemany('INSERT INTO messages VALUES (?,?,?,?)', [
                ('old','user','旧消息',now-8*86400), ('new','user','新消息全文',now-100),
                ('future','user','未来消息',now+100), ('system','system','系统',now-10)])
            await db.commit()
        self.assertEqual(await profile_initialize._collect_sources(now-7*86400,now), ['private:new'])

    async def test_initialization_resumes_original_evidence_after_model_failure(self):
        import profile_initialize
        group = await self.batch()
        async with self.connect() as db:
            await db.execute("ALTER TABLE messages ADD COLUMN conv_id TEXT DEFAULT 'room'")
            await db.execute('CREATE TABLE memories(type TEXT,source_msg_id TEXT,source_conv TEXT,source_start_ts REAL,source_end_ts REAL,keywords TEXT)')
            await db.execute('INSERT INTO memories(type,source_msg_id) VALUES (?,?)', ('important', '["private:one"]'))
            await db.commit()
        model = AsyncMock(side_effect=[RuntimeError('offline'), json.dumps(self.result(), ensure_ascii=False)])
        with patch.object(post_sentinel, 'load_worldbook', return_value={'user_name': 'Mira'}), patch(
            'chatroom.get_chatroom_names', return_value=('Mira', 'Sol', 'Nova')
        ), patch('chatroom.load_chatroom_config', return_value={'connor_model': 'configured-strong'}), patch(
            'ai_providers.simple_ai_call', model
        ):
            await profile_initialize.run()
            data = await post_sentinel.snapshot()
            self.assertEqual(data['initialization']['cursor'], 0)
            self.assertEqual(data['initialization']['status'], 'error')
            await profile_initialize.run()
        data = await post_sentinel.snapshot()
        self.assertEqual(data['initialization']['status'], 'done')
        self.assertFalse(data['needs_initialization'])
        self.assertIn(group[0]['content'], model.await_args.args[0][0]['content'])
        self.assertEqual(model.await_args.args[1], 'Codex-5.6-Sol')
        self.assertEqual(data['initialization']['calls'], 1)
        self.assertIn('告别视频', data['entries'][0]['content'])


class ProfileMergeTests(unittest.TestCase):
    def test_undated_persona_has_no_fabricated_epoch_confirmation(self):
        from profile_prompts import profile_material
        material = profile_material({'entries': {'identity': {'source_ts': 0}}})
        self.assertIsNone(material[0]['last_confirmed'])

    def fixtures(self):
        entries = {k: {'key': k, 'category': 'recent', 'kind': 'project', 'reason': '正在创作有后续', 'topic': '告别视频', 'content': k,
                       'source_ts': 10, 'sources': ['private:' + k]} for k in ('a', 'b')}
        document = {'entries': copy.deepcopy(entries)}
        job = {'digest': True, 'consolidation': True, 'source_ts': 30,
               '_refs': {'P1': entries['a'], 'P2': entries['b']}, '_base_entries': entries}
        update = {'ref': 'P1', 'merge_from': ['P2'], 'content': '合并保留关键背景', 'sources': ['U1']}
        return document, job, update

    def test_merge_preserves_all_sources_with_new_evidence(self):
        doc, job, update = self.fixtures()
        post_sentinel._apply_analysis(doc, {'updates': [update]}, {'U1': 'private:c'}, job)
        self.assertEqual(set(doc['entries']['a']['sources']), {'private:a', 'private:b', 'private:c'})
        self.assertNotIn('b', doc['entries'])

    def test_merge_does_not_absorb_concurrently_edited_entry(self):
        doc, job, update = self.fixtures()
        doc['entries']['b']['content'] = '本人纠正'
        expected = copy.deepcopy(doc['entries'])
        post_sentinel._apply_analysis(doc, {'updates': [update]}, {'U1': 'private:c'}, job)
        self.assertEqual(doc['entries'], expected)

    def test_sentinel_fact_topic_match_resolves_source_labels(self):
        doc, job, _ = self.fixtures()
        job = {'source_ts': 30}
        post_sentinel._apply_analysis(doc, {'updates': [{'category': 'recent', 'reason': '正在创作有后续', 'topic': '告别视频',
            'content': '补齐告别的意义', 'sources': ['U1']}]}, {'U1': 'private:a'}, job)
        self.assertEqual(len(doc['entries']), 2)
        self.assertEqual(doc['entries']['a']['content'], '补齐告别的意义')


if __name__ == '__main__':
    unittest.main()
