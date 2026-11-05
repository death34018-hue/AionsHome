import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite


class PostSentinelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import post_sentinel as service
        self.service = service
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'chat.db'
        self.db_patch = patch.object(service, 'get_db', self.connect)
        self.enabled_patch = patch.object(service, 'enabled', return_value=True)
        self.db_patch.start()
        self.enabled_patch.start()
        async with self.connect() as db:
            await db.execute('CREATE TABLE messages (id TEXT,conv_id TEXT,role TEXT,content TEXT,created_at REAL)')
            await db.execute('CREATE TABLE chatroom_messages (id TEXT,room_id TEXT,sender TEXT,content TEXT,created_at REAL)')
            await db.commit()
        await service.ensure_schema()

    async def asyncTearDown(self):
        await self.service.stop()
        self.db_patch.stop()
        self.enabled_patch.stop()
        self.tmp.cleanup()

    def connect(self):
        return aiosqlite.connect(self.db_path)

    async def message(self, msg_id='one', *, text='明天要取药', scope='private', ts=None):
        ts = ts or time.time()
        private = scope == 'private'
        async with self.connect() as db:
            await db.execute(f'INSERT INTO {"messages" if private else "chatroom_messages"} VALUES (?,?,?,?,?)',
                             (msg_id, 'room', 'user', text, ts))
            await db.commit()
        return {'type': 'msg_created' if private else 'chatroom_msg_created', 'data': {
            'id': msg_id, 'conv_id' if private else 'room_id': 'room',
            'role' if private else 'sender': 'user', 'content': text, 'created_at': ts,
        }}

    def analysis(self, content='明天要取药', source='U1'):
        return {'updates': [{'key': 'communication.reminders', 'category': 'recent', 'kind':'task', 'reason': '近期变化有后续价值',
                             'content': content, 'sources': [source]}], 'state': None}

    async def test_resolved_budget_warning_disappears_without_erasing_failed_job(self):
        warning = '近期清单超过约2000 tokens或24项，请先合并同一事项、精简正文；未完成待办不能省略'
        await self.service.enqueue_event(await self.message())
        async with self.connect() as db:
            await db.execute("UPDATE post_sentinel_jobs SET status='failed',error=?", ('状态：' + warning,))
            await db.commit()
        data = await self.service.snapshot()
        self.assertEqual(data['last_error'], '')
        self.assertEqual(data['failed'], 1)
        async with self.connect() as db:
            error = (await (await db.execute('SELECT error FROM post_sentinel_jobs')).fetchone())[0]
        self.assertEqual(error, '状态：' + warning)

    async def test_budget_warning_tracks_current_capacity_and_preserves_other_errors(self):
        warning = '近期清单超过约2000 tokens或24项，请先合并同一事项、精简正文；未完成待办不能省略'
        document = {'entries': {str(i): {'key': str(i), 'category': 'recent', 'kind': 'task',
                     'content': '待处理', 'source_ts': time.time()} for i in range(25)}}
        async with self.connect() as db:
            await db.execute('UPDATE post_sentinel_document SET payload=?,last_error=?',
                             (json.dumps(document), '画像：' + warning + '；网络连接失败'))
            await db.execute("INSERT INTO post_sentinel_digest_jobs VALUES ('batch','{}','pending',?,0)",
                             (warning,))
            await db.commit()
        # Even with the feature disabled, capacity is measured from the saved document.
        with patch.object(self.service, 'enabled', return_value=False):
            self.assertIn(warning, (await self.service.snapshot())['last_error'])
        document['entries'] = {}
        async with self.connect() as db:
            await db.execute('UPDATE post_sentinel_document SET payload=?', (json.dumps(document),))
            await db.commit()
        self.assertEqual((await self.service.snapshot())['last_error'], '网络连接失败')

    async def test_profile_subject_uses_configured_name_for_facts_and_activity(self):
        for name in ('小星', 'Mira'):
            with self.subTest(name=name), patch.object(self.service, 'load_worldbook', return_value={'user_name': name}):
                prompt = self.service._analysis_prompt({'entries': {}, 'current_state': None}, [], {}, '')
                self.assertIn(f'本人姓名为“{name}”', prompt)
                document = {'entries': {}, 'current_state': None}
                response = {'updates': [{'category': 'recent', 'kind':'task', 'reason': '近期变化有后续价值', 'content': '用户明天去取药',
                                         'topic': '用户的取药安排', 'sources': ['U1']}],
                            'state': {'text': '用户正在洗菜', 'phase': 'ongoing', 'sources': ['U1']}}
                self.service._apply_analysis(document, response, {'U1': 'private:one'}, {'source_ts': time.time()})
                entry = next(iter(document['entries'].values()))
                self.assertEqual(entry['content'], name + '明天去取药')
                self.assertEqual(entry['topic'], name + '的取药安排')
                self.assertEqual(document['current_state']['text'], name + '正在洗菜')

    async def test_subject_rename_preserves_technical_terms_and_other_people(self):
        from user_profile_policy import personalize_text, personalize_document
        self.assertEqual(personalize_text('用户在做用户画像功能，给其他用户改善用户体验', 'Mira'),
                         'Mira在做用户画像功能，给其他用户改善用户体验')
        self.assertEqual(personalize_text('用户喜欢写字', '名字用户'), '名字用户喜欢写字')
        entry = {'key': 'manual', 'content': '用户喜欢自然聊天', 'topic': '', 'locked': True,
                 'sources': ['manual'], 'source_ts': 123.456, 'updated_at': 789.012}
        state = {'text': '用户正在洗菜', 'event_at': 234.567, 'expires_at': 800.123, 'locked': True}
        document = {'entries': {'manual': dict(entry)}, 'current_state': dict(state)}
        self.assertEqual(personalize_document(document, 'Mira'), (1, True))
        self.assertEqual(personalize_document(document, 'Mira'), (0, False))
        for key in entry.keys() - {'content'}:
            self.assertEqual(document['entries']['manual'][key], entry[key])
        for key in state.keys() - {'text'}:
            self.assertEqual(document['current_state'][key], state[key])

    async def test_new_incidental_records_need_reason_and_completed_events_are_skipped(self):
        document = {'entries': {}, 'current_state': None}
        parsed = {'updates': [
            {'category': 'recent', 'topic': '晚餐', 'kind': 'task', 'content': '已吃完晚餐', 'sources': ['U1']},
            {'category': 'recent', 'topic': '遛狗', 'kind': 'task', 'status': 'done',
             'content': '已遛狗回来', 'sources': ['U1']},
            {'category': 'recent', 'topic': '取药', 'kind': 'task', 'content': '明天去医院取药',
             'reason': '尚未完成且有明确安排，需要后续记住', 'sources': ['U1']},
            {'category': 'recent', 'reason': '近期变化有后续价值', 'topic': '角色喜好', 'content': '喜欢人外角色', 'sources': ['U1']},
        ], 'state': None}
        self.service._apply_analysis(document, parsed, {'U1': 'private:one'}, {'source_ts': time.time()})
        values = list(document['entries'].values())
        self.assertEqual([e['topic'] for e in values], ['取药'])
        self.assertEqual({e['category'] for e in values}, {'recent'})
        self.assertTrue(document['_analysis_errors'])

    async def test_finished_activity_updates_state_without_creating_a_fact(self):
        now = time.time()
        document = {'entries': {}, 'current_state': {'text': '正在遛狗', 'phase': 'ongoing',
                    'event_at': now-100, 'expires_at': now+600}}
        response = {'updates': [], 'state': {'text': '遛狗结束，已回家', 'phase': 'finished',
                    'sources': ['U1']}}
        self.service._apply_analysis(document, response, {'U1': 'private:one'}, {'source_ts': now})
        from user_profile_policy import render_context
        self.assertEqual(document['entries'], {})
        self.assertEqual(document['current_state']['phase'], 'finished')
        self.assertEqual(render_context(document, now=now)[0], '')

    async def test_activity_accepts_content_alias_and_optional_fields(self):
        now = time.time()
        for state in (
            {'content': '准备去睡觉', 'phase': '进行中', 'sources': 'U1', 'ttl_minutes': None},
            {'text': '准备去睡觉', 'phase': 'ONGOING', 'sources': ['private:one']},
            {'content': '准备去睡觉', 'sources': ['U1'], 'ttl_minutes': 'bad'},
        ):
            with self.subTest(state=state):
                document = {'entries': {}, 'current_state': None}
                self.assertTrue(self.service._apply_analysis(
                    document, {'current_state': state}, {'U1': 'private:one'},
                    {'source_id': 'private:one', 'source_ts': now}))
                self.assertEqual(document['_analysis_errors'], [])
                self.assertEqual(document['current_state']['text'], '准备去睡觉')
                self.assertEqual(document['current_state']['sources'], ['private:one'])
                self.assertEqual(document['current_state']['expires_at'], now + 3600)
        self.assertEqual(document['current_state']['phase'], 'unknown')

    async def test_single_update_accepts_text_and_null_optional_metadata(self):
        document = {'entries': {}, 'current_state': None}
        response = {'updates': {'text': '明天去取药', 'kind': 'task', 'topic': None,
                               'reason': '明确的未完成安排', 'sources': 'U1',
                               'importance': None, 'certainty': None, 'keywords': None,
                               'next_step': None, 'status': None}}
        self.assertTrue(self.service._apply_analysis(
            document, response, {'U1': 'private:one'}, {'source_ts': time.time()}))
        self.assertEqual(document['_analysis_errors'], [])
        entry = next(iter(document['entries'].values()))
        self.assertEqual(entry['content'], '明天去取药')
        self.assertEqual(entry['kind'], 'task')
        self.assertEqual(entry['status'], 'active')

    async def test_activity_accepts_sentence_over_old_limit_without_truncation(self):
        text = '准备整理项目文档，' * 9 + '整理完以后再休息。'
        document = {'entries': {}, 'current_state': None}
        self.assertTrue(self.service._apply_analysis(
            document, {'updates': None, 'state': {'text': text, 'phase': 'planned', 'sources': ['U1']}},
            {'U1': 'private:one'}, {'source_ts': time.time()}))
        self.assertEqual(document['current_state']['text'], text)
        self.assertEqual(document['_analysis_errors'], [])
        from routes.user_profile import CurrentState
        state = CurrentState(text=text, phase='planned', expires_at=time.time() + 3600)
        saved = await self.service.save_manual({'entries': [], 'current_state': state.model_dump()}, 0)
        self.assertEqual(saved['current_state']['text'], text)

    async def test_compatibility_never_invents_sources_or_accepts_unknown_phase(self):
        for state in ({'content': '准备睡觉', 'phase': 'ongoing'},
                      {'content': '准备睡觉', 'phase': 'made-up', 'sources': 'U1'}):
            with self.subTest(state=state):
                document = {'entries': {}, 'current_state': None}
                self.service._apply_analysis(document, {'updates': [], 'state': state},
                                             {'U1': 'private:one'}, {'source_ts': time.time()})
                self.assertIsNone(document['current_state'])
                self.assertTrue(document['_analysis_errors'])

    async def test_deduplicates_user_events_and_ignores_ai_and_system_messages(self):
        event = await self.message()
        await self.service.enqueue_event(event)
        await self.service.enqueue_event(event)
        for role in ('assistant', 'system'):
            await self.service.enqueue_event({'type': 'msg_created', 'data': {**event['data'], 'role': role}})
        model = AsyncMock(return_value=self.analysis())
        with patch.object(self.service, '_call_model', model):
            self.assertTrue(await self.service.run_pending_once())
            self.assertFalse(await self.service.run_pending_once())
        model.assert_awaited_once()
        data = await self.service.snapshot()
        self.assertEqual(data['entries'][0]['sources'], ['private:one'])
        self.assertIn('明天要取药', await self.service.build_context())

    async def test_shared_chatroom_uses_same_profile_and_short_user_sources(self):
        await self.service.enqueue_event(await self.message(scope='chatroom'))
        with patch.object(self.service, '_call_model', AsyncMock(return_value=self.analysis())) as model:
            await self.service.run_pending_once()
        data = await self.service.snapshot()
        self.assertEqual(data['entries'][0]['sources'], ['chatroom:one'])
        self.assertIn('U1', model.await_args.args[0])

    async def test_manual_edit_allows_future_updates_but_stale_analysis_cannot_overwrite_it(self):
        await self.service.enqueue_event(await self.message())
        async def during_call(prompt):
            data = await self.service.snapshot()
            await self.service.save_manual({'entries': [{
                'key': 'communication.reminders', 'category': 'recent', 'reason': '近期变化有后续价值',
                'content': '我自己写的提醒偏好', 'locked': True,
            }], 'current_state': None}, data['revision'])
            return self.analysis()
        with patch.object(self.service, '_call_model', side_effect=during_call):
            await self.service.run_pending_once()
        data = await self.service.snapshot()
        self.assertEqual(data['entries'][0]['content'], '我自己写的提醒偏好')
        self.assertFalse(data['entries'][0]['locked'])
        await self.service.enqueue_event(await self.message('two'))
        with patch.object(self.service, '_call_model', AsyncMock(return_value=self.analysis('自动改写', 'U2'))):
            await self.service.run_pending_once()
        self.assertEqual((await self.service.snapshot())['entries'][0]['content'], '自动改写')
        self.assertEqual((await self.service.snapshot())['last_error'], '')

    async def test_off_disables_analysis_and_context_and_invalidates_inflight_result(self):
        event = await self.message()
        with patch.object(self.service, 'enabled', return_value=False), patch.object(self.service, '_call_model', AsyncMock()) as model:
            await self.service.enqueue_event(event)
            await self.service.run_pending_once()
            self.assertEqual(await self.service.build_context(), '')
            model.assert_not_awaited()
        await self.service.enqueue_event(event)
        async def during_call(prompt):
            await self.service.invalidate_pending()
            return self.analysis()
        with patch.object(self.service, '_call_model', side_effect=during_call):
            await self.service.run_pending_once()
        self.assertEqual((await self.service.snapshot())['entries'], [])

    async def test_invalid_source_and_model_failure_leave_document_unchanged(self):
        event = await self.message()
        await self.service.enqueue_event(event)
        invalid = self.analysis()
        invalid['updates'][0]['sources'] = ['made-up']
        with patch.object(self.service, '_call_model', AsyncMock(return_value=invalid)):
            await self.service.run_pending_once()
        self.assertEqual((await self.service.snapshot())['entries'], [])
        await self.service.enqueue_event(await self.message('two'))
        with patch.object(self.service, '_call_model', AsyncMock(side_effect=RuntimeError('model offline'))):
            await self.service.run_pending_once()
        data = await self.service.snapshot()
        self.assertEqual(data['entries'], [])
        self.assertTrue(data['last_error'])

    async def test_state_expiry_noop_and_older_events(self):
        now = time.time()
        await self.service.enqueue_event(await self.message(text='我已经泡上澡了', ts=now))
        response = {'updates': [], 'state': {'text': '正在泡澡', 'phase': 'ongoing',
                                           'ttl_minutes': 30, 'sources': ['U1']}}
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        first = (await self.service.snapshot())['current_state']
        self.assertIn('正在泡澡', await self.service.build_context(now=now + 60))
        self.assertNotIn('正在泡澡', await self.service.build_context(now=now + 3600))
        await self.service.enqueue_event(await self.message('two', text='哈哈', ts=now + 120))
        with patch.object(self.service, '_call_model', AsyncMock(return_value={'updates': [], 'state': None})):
            await self.service.run_pending_once()
        self.assertEqual((await self.service.snapshot())['current_state']['event_at'], first['event_at'])
        await self.service.enqueue_event(await self.message('old', ts=now - 100))
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        self.assertEqual((await self.service.snapshot())['current_state']['event_at'], first['event_at'])

    async def test_manual_revision_and_deleted_key_protection(self):
        data = await self.service.snapshot()
        content = {'entries': [{'key': 'food.preference', 'category': 'recent', 'reason': '近期变化有后续价值',
                               'content': '喜欢清淡', 'locked': True}], 'current_state': None}
        await self.service.save_manual(content, data['revision'])
        with self.assertRaises(ValueError):
            await self.service.save_manual(content, data['revision'])
        data = await self.service.snapshot()
        await self.service.save_manual({'entries': [], 'current_state': None}, data['revision'])
        await self.service.enqueue_event(await self.message())
        response = {'updates': [{'key': 'food.preference', 'category': 'recent', 'reason': '近期变化有后续价值',
                                 'content': '喜欢重口味', 'sources': ['U1']}], 'state': None}
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        self.assertEqual((await self.service.snapshot())['entries'], [])

    async def test_api_manual_edit_and_disabled_context_in_shared_prompt(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from routes.user_profile import router
        app = FastAPI()
        app.include_router(router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            data = (await client.get('/api/user-profile')).json()
            body = {'revision': data['revision'], 'entries': [{
                'key': 'preferences.food', 'category': 'recent', 'reason': '近期变化有后续价值', 'content': '手写偏好', 'locked': True,
            }], 'current_state': None}
            self.assertEqual((await client.put('/api/user-profile', json=body)).status_code, 200)
            self.assertEqual((await client.put('/api/user-profile', json=body)).status_code, 409)
        import context_builder
        with patch.object(context_builder, 'build_capability_prompt_items', AsyncMock(return_value=[])), patch.object(
            context_builder, 'is_capability_enabled', return_value=False
        ), patch.object(context_builder, 'get_device_context_for_prompt', return_value=''), patch.object(
            context_builder, 'build_cli_file_storage_text', return_value=''
        ):
            self.assertIn('手写偏好', await context_builder.build_ability_block('用户'))
            with patch.object(self.service, 'enabled', return_value=False):
                self.assertNotIn('手写偏好', await context_builder.build_ability_block('用户'))

    async def test_real_sentinel_adapter_uses_config_and_parses_output(self):
        import memory
        with patch.object(self.service, 'get_sentinel_config', return_value={
            'ready': True, 'model': 'configured-luna'
        }), patch.object(memory, '_call_sentinel_text', AsyncMock(return_value='{"updates":[],"state":null}')) as call:
            self.assertEqual(await self.service._call_model('test'), {'updates': [], 'state': None})
        self.assertEqual(call.await_args.args[0]['model'], 'configured-luna')

    async def test_fenced_model_reply_with_content_alias_is_persisted(self):
        import memory
        await self.service.enqueue_event(await self.message(text='准备去睡觉'))
        raw = '整理好了：\n```json\n' + json.dumps({
            'state': {'content': '准备去睡觉', 'phase': 'ONGOING', 'sources': 'U1'}
        }, ensure_ascii=False) + '\n```'
        with patch.object(self.service, 'get_sentinel_config', return_value={
            'ready': True, 'model': 'fixture'
        }), patch.object(memory, '_call_sentinel_text', AsyncMock(return_value=raw)):
            await self.service.run_pending_once()
        data = await self.service.snapshot()
        self.assertEqual(data['current_state']['text'], '准备去睡觉')
        self.assertEqual(data['pending'], 0)
        self.assertEqual(data['failed'], 0)
        self.assertEqual(data['last_error'], '')

    async def test_background_luna_does_not_wait_for_busy_chat_slots(self):
        import ai_providers
        from types import SimpleNamespace
        async def stream(*args, **kwargs):
            yield SimpleNamespace(kind='text_delta', text='{"updates":[],"state":null}')
        with patch.object(self.service, 'get_sentinel_config', return_value={
            'ready': True, 'provider': 'codex', 'model': 'configured-luna'
        }), patch.object(ai_providers, '_CODEX_SCRIPT', 'fixture-script'), patch.object(
            ai_providers, '_codex_semaphore', return_value=asyncio.Semaphore(0)
        ), patch.object(ai_providers, '_build_codex_chat_environment', return_value={}), patch.object(
            ai_providers, 'stream_codex_app_server', side_effect=stream
        ):
            self.assertEqual(await asyncio.wait_for(self.service._call_model('test'), timeout=2),
                             {'updates': [], 'state': None})

    async def test_cleared_state_does_not_return_from_old_queued_message(self):
        now = time.time()
        first = await self.message(text='我在泡澡', ts=now - 20)
        second = await self.message('two', text='还在泡澡', ts=now - 10)
        response = {'updates': [], 'state': {'text': '正在泡澡', 'phase': 'ongoing',
                                           'ttl_minutes': 30, 'sources': ['U1']}}
        await self.service.enqueue_event(first)
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        await self.service.enqueue_event(second)
        data = await self.service.snapshot()
        await self.service.save_manual({'entries': [], 'current_state': None}, data['revision'])
        response['state']['sources'] = ['U2']
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        self.assertIsNone((await self.service.snapshot())['current_state'])

    async def test_source_edit_during_model_call_discards_extraction(self):
        await self.message('old', text='我喜欢苹果', ts=time.time()-20)
        await self.service.enqueue_event(await self.message(text='对，就是那种水果'))
        async def during_call(prompt):
            async with self.connect() as db:
                await db.execute('UPDATE messages SET content=? WHERE id=?', ('我喜欢香蕉', 'old'))
                await db.commit()
            response = self.analysis('喜欢苹果')
            response['updates'][0]['sources'] = ['U1', 'U2']
            return response
        with patch.object(self.service, '_call_model', side_effect=during_call):
            await self.service.run_pending_once()
        self.assertEqual((await self.service.snapshot())['entries'], [])

    async def test_off_before_dispatch_prevents_model_call(self):
        await self.service.enqueue_event(await self.message())
        context = self.service._context_for_job
        active = True
        async def disable_during_context(job):
            nonlocal active
            result = await context(job)
            active = False
            await self.service.invalidate_pending()
            return result
        with patch.object(self.service, 'enabled', side_effect=lambda: active), patch.object(
            self.service, '_context_for_job', side_effect=disable_during_context
        ), patch.object(self.service, '_call_model', AsyncMock()) as model:
            await self.service.run_pending_once()
            model.assert_not_awaited()

    async def test_clear_empty_state_fences_older_queued_activity(self):
        await self.service.enqueue_event(await self.message(text='我在泡澡', ts=time.time()-10))
        data = await self.service.snapshot()
        await self.service.save_manual({'entries': [], 'current_state': None}, data['revision'])
        response = {'updates': [], 'state': {'text': '正在泡澡', 'phase': 'ongoing',
                                           'ttl_minutes': 30, 'sources': ['U1']}}
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        self.assertIsNone((await self.service.snapshot())['current_state'])

    async def test_valid_pickup_survives_invalid_state_and_partial_retry(self):
        await self.service.enqueue_event(await self.message(text='明天十点半之后取药'))
        response = {'updates': [{'category': 'recent', 'topic': '取药', 'kind': 'task',
                                'content': '明天10:30之后取药，尚未完成', 'reason': '明确的未完成取药安排', 'sources': ['U1']}],
                    'state': {'text': '', 'phase': 'planned', 'sources': ['U1']}}
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        data = await self.service.snapshot()
        self.assertEqual(len(data['entries']), 1)
        self.assertIn('内容', data['last_error'])
        self.assertEqual(data['pending'], 1)
        self.assertIn('尚未完成', await self.service.build_context(now=time.time() + 45*86400))

    async def test_same_topic_updates_and_completed_task_leaves_prompt(self):
        available = time.time() + 86400
        await self.service.enqueue_event(await self.message(text='准备去医院取药'))
        response = {'updates': [{'category': 'recent', 'topic': '取药', 'kind': 'task',
                                'content': '准备去医院取药', 'available_at': available,
                                'reason': '明确的未完成取药安排', 'sources': ['U1']}], 'state': None}
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        await self.service.enqueue_event(await self.message('two', text='先吃早饭，晚点再出门'))
        response['updates'][0].update(ref='P1', content='早饭后出门取药', available_at=None, sources=['U2'])
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        delayed = (await self.service.snapshot())['entries'][0]
        self.assertEqual(delayed['available_at'], available)
        self.assertEqual(delayed['status'], 'active')
        await self.service.enqueue_event(await self.message('three', text='已经取到了'))
        response['updates'][0].update(content='已经取到药', status='done', sources=['U3'])
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        data = await self.service.snapshot()
        self.assertEqual(len(data['entries']), 1)
        self.assertEqual(data['entries'][0]['status'], 'done')
        self.assertNotIn('取药', await self.service.build_context())

    async def test_background_only_fact_cannot_be_extracted_from_unrelated_latest_message(self):
        old_time = time.time()-3600
        await self.message('earlier', text='我喜欢安静', ts=old_time)
        await self.service.enqueue_event(await self.message(text='今天在看电影'))
        response = {'updates': [{'category': 'recent', 'reason': '近期变化有后续价值', 'topic': '交流', 'content': '喜欢安静',
                                'sources': ['U1']}], 'state': None}
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        data = await self.service.snapshot()
        self.assertEqual(data['entries'], [])
        self.assertIn('当前用户消息', data['last_error'])

    async def test_failed_job_retries_with_exact_error_and_recovers(self):
        await self.service.enqueue_event(await self.message())
        with patch.object(self.service, '_call_model', AsyncMock(side_effect=RuntimeError('model offline'))):
            await self.service.run_pending_once()
        data = await self.service.snapshot()
        self.assertIn('model offline', data['last_error'])
        self.assertEqual(data['pending'], 1)
        async with self.connect() as db:
            await db.execute('UPDATE post_sentinel_jobs SET next_attempt_at=0')
            await db.commit()
        with patch.object(self.service, '_call_model', AsyncMock(return_value=self.analysis())):
            await self.service.run_pending_once()
        self.assertEqual((await self.service.snapshot())['pending'], 0)
        self.assertEqual((await self.service.snapshot())['last_error'], '')

    async def test_consolidation_cannot_retire_or_merge_away_unfinished_task(self):
        import copy
        now = time.time()
        original = {'entries': {'task': {'key': 'task', 'category': 'recent', 'kind': 'task',
                    'status': 'stale', 'content': '明天取药', 'source_ts': now, 'sources': ['private:one']}}}
        for update in ({'ref': 'P1', 'status': 'retired'}, {'ref': 'P1', 'status': 'done'},
                       {'ref': 'P1', 'kind': 'context'}, {'merge_from': ['P1'], 'kind': 'context'}):
            with self.subTest(update=update):
                document = copy.deepcopy(original)
                self.service._apply_analysis(document, {'updates': [{**update, 'category': 'recent',
                    'content': '压缩后的资料'}], 'state': None}, {}, {'consolidation': True, 'source_ts': now})
                self.assertEqual(document['entries']['task']['status'], 'stale')
                self.assertTrue(document['_analysis_errors'])

    async def test_two_independent_tasks_can_share_topic_without_overwrite(self):
        await self.service.enqueue_event(await self.message(text='明天取自己的药'))
        response = {'updates': [{'category': 'recent', 'topic': '取药', 'kind': 'task',
                                'content': '明天取自己的药', 'reason': '明确的未完成取药安排', 'sources': ['U1']}], 'state': None}
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        data = await self.service.snapshot()
        await self.service.save_manual({'entries': [{**data['entries'][0], 'locked': True}], 'current_state': None}, data['revision'])
        await self.service.enqueue_event(await self.message('two', text='下周还要取狗狗的药'))
        response['updates'][0].update(content='下周取狗狗的药', sources=['U2'])
        with patch.object(self.service, '_call_model', AsyncMock(return_value=response)):
            await self.service.run_pending_once()
        self.assertEqual(len((await self.service.snapshot())['entries']), 2)

    async def test_api_unrelated_edit_preserves_task_times_and_confirmation(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from routes.user_profile import router
        now = time.time()
        data = await self.service.save_manual({'entries': [
            {'key': 'manual.preference', 'category': 'recent', 'reason': '近期变化有后续价值', 'content': '完整段落', 'locked': True},
            {'key': 'manual.task', 'category': 'recent', 'kind': 'task', 'status': 'active',
             'content': '明天取药', 'available_at': now+86400, 'locked': False},
        ], 'current_state': None}, 0)
        original = data['entries'][1]
        app = FastAPI()
        app.include_router(router)
        # The compact editor sends only visible fields; lifecycle metadata must survive.
        entries = [{k:v for k,v in e.items() if k in ('key','category','content','locked')} for e in data['entries']]
        entries[0]['content'] = '喜欢自然完整的段落'
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.put('/api/user-profile', json={'revision':data['revision'], 'entries':entries,'current_state':None})
        self.assertEqual(response.status_code, 200)
        task = response.json()['entries'][1]
        self.assertEqual(task['source_ts'], original['source_ts'])
        self.assertEqual(task['available_at'], original['available_at'])
        self.assertEqual(task['kind'], 'task')
        self.assertEqual(task['status'], 'active')
        self.assertFalse(task['locked'])

    async def test_v4_migration_backs_up_and_never_resumes_history(self):
        legacy = {'version':4,'entries':{'old':{'content':'旧长期画像'}},
                  'initialization':{'source_ids':['private:old'],'cursor':1,'status':'pending'}}
        async with self.connect() as db:
            await db.execute('UPDATE post_sentinel_document SET payload=?', (json.dumps(legacy),))
            await db.commit()
        await self.service.ensure_schema()
        await self.service.ensure_schema()
        data = await self.service.snapshot()
        self.assertEqual(data['version'], 5)
        self.assertEqual(data['entries'], [])
        self.assertFalse(data['needs_initialization'])
        self.assertEqual(data['initialization'], {})
        async with self.connect() as db:
            backups = await (await db.execute("SELECT payload FROM post_sentinel_backups WHERE reason='profile-v4-before-v5'")).fetchall()
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0][0]), legacy)

    async def test_crashed_week_is_manual_resumable_without_auto_model_call(self):
        async with self.connect() as db:
            await db.execute('UPDATE post_sentinel_document SET payload=?', (json.dumps({
                'version':5,'entries':{},'initialization':{'mode':'recent_week','status':'running','cursor':20}}),))
            await db.commit()
        import profile_initialize
        with patch.object(profile_initialize, 'start', AsyncMock()) as init, patch.object(self.service, '_loop', AsyncMock()):
            await self.service.start()
            data = await self.service.snapshot()
            self.assertEqual(data['initialization']['status'],'pending')
            self.assertEqual(data['initialization']['cursor'],20)
            init.assert_not_awaited()

    async def test_simple_call_collects_usage_without_extra_call(self):
        import ai_providers
        async def stream(*args, **kwargs):
            kwargs['meta'].update(prompt_tokens=123,completion_tokens=45)
            yield '{}'
        usage = {}
        with patch.object(ai_providers, 'stream_ai', stream), patch.object(ai_providers, '_save_model_raw_response') as save:
            self.assertEqual(await ai_providers.simple_ai_call([], 'Codex-5.6-Sol', usage=usage), '{}')
        self.assertEqual(usage['prompt_tokens'],123)
        self.assertEqual(save.call_args.kwargs['usage']['completion_tokens'],45)


if __name__ == '__main__':
    unittest.main()
