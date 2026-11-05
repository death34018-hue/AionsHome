import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import capabilities


class CompositionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import compositions
        self.music = compositions
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'chat.db'
        self.enterContext(patch.object(compositions, 'get_db', lambda: aiosqlite.connect(self.path)))
        self.enterContext(patch.object(compositions, 'append_sync_event', AsyncMock(return_value=1)))
        self.broadcast = self.enterContext(patch.object(compositions, 'broadcast_synced', AsyncMock()))
        self.enterContext(patch('chatroom.get_chatroom_names', return_value=('月亮', '星星', '小熊')))
        async with aiosqlite.connect(self.path) as db:
            await db.executescript('''
                CREATE TABLE conversations(id TEXT PRIMARY KEY, updated_at REAL);
                CREATE TABLE chatroom_rooms(id TEXT PRIMARY KEY, type TEXT, updated_at REAL);
                CREATE TABLE messages(id TEXT PRIMARY KEY,conv_id TEXT,role TEXT,content TEXT,created_at REAL,attachments TEXT);
                CREATE TABLE chatroom_messages(id TEXT PRIMARY KEY,room_id TEXT,sender TEXT,content TEXT,created_at REAL,attachments TEXT);
                INSERT INTO conversations VALUES('private',0);
                INSERT INTO chatroom_rooms VALUES('group','group',0),('companion','connor_1v1',0);
            ''')
        self.block = '[COMPOSE]' + json.dumps({
            'title': '晚灯', 'cycles': 4,
            'code': 'setcpm(76/4);note("e5@2 [g5 a5] g5").s("piano")',
        }, ensure_ascii=False) + '[/COMPOSE]'

    async def test_saved_once_in_both_private_chats_and_group_without_execution_gate(self):
        with patch.object(capabilities, 'is_capability_enabled', return_value=False):
            for scope, parent, actor in [('private', 'private', 'aion'),
                                         ('chatroom', 'companion', 'connor'),
                                         ('chatroom', 'group', 'aion')]:
                saved = AsyncMock()
                arguments = dict(source_type=scope, source_id=parent, sender=actor,
                                 source_msg_id=parent, on_saved=saved)
                self.assertEqual(await self.music.process_compositions('送你一首。' + self.block, **arguments), '送你一首。')
                await self.music.process_compositions(self.block, **arguments)
                saved.assert_awaited_once()
                message = saved.call_args.args[0]
                self.assertIn({'type': 'system_notice_order', 'after_msg_id': parent}, message['attachments'])
                attachment = message['attachments'][0]
                self.assertEqual(attachment['type'], 'composition')
                self.assertNotIn('code', attachment)
        pieces = await self.music.list_compositions()
        self.assertEqual(len(pieces), 3)
        self.assertEqual({piece['actor_name'] for piece in pieces}, {'星星', '小熊'})
        piece = await self.music.get_composition(pieces[0]['id'])
        self.assertEqual(piece['bpm'], 76)
        self.assertIn('setcpm', piece['code'])
        await self.music.set_favorite(piece['id'], True)
        self.assertTrue((await self.music.get_composition(piece['id']))['favorite'])

    async def test_invalid_and_incomplete_scores_are_hidden_without_saving(self):
        for block in ('[COMPOSE]{broken}[/COMPOSE]', '[COMPOSE]{"title":"未完成"',
                      '[COMPOSE]{"title":"空谱","code":""}[/COMPOSE]'):
            self.assertEqual(await self.music.process_compositions('前文' + block, source_type='private',
                             source_id='private', sender='aion', source_msg_id='bad'), '前文')
        self.assertEqual(await self.music.list_compositions(), [])

    async def test_autonomous_private_gift_becomes_a_playable_card(self):
        import autonomy
        with patch.object(autonomy.manager, 'get_connor_last_active', return_value='companion'), \
             patch('routes.chatroom._save_msg', AsyncMock(return_value={'id': 'gift'})) as save:
            await autonomy._save_private_message('connor', '送你一首。' + self.block, auto_tts=False)
        self.assertEqual(save.call_args.args[2], '送你一首。')
        pieces = await self.music.list_compositions()
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0]['actor'], 'connor')
        self.assertEqual(pieces[0]['source_msg_id'], save.call_args.kwargs['msg_id'])


class CompositionPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stream_accepts_score_angles_and_still_rejects_visible_protocol_tags(self):
        from safe_live_stream import consume_safe_live_stream
        command = '[COMPOSE]' + json.dumps({'title': '晚灯', 'code': 'note("<c4 g4>").s("piano")'}) + '[/COMPOSE]'
        text = '给你。' + command + '慢慢听。'
        for size in (1, 7, len(text)):
            async def chunks():
                for index in range(0, len(text), size):
                    yield text[index:index + size]
            visible = []
            result = await consume_safe_live_stream(chunks(), visible.append)
            self.assertIsNone(result.stop_reason)
            self.assertEqual(result.committed_text, text)
            self.assertEqual(''.join(visible), '给你。慢慢听。')
        async def unsafe():
            yield text + '<custom_protocol>secret</custom_protocol>'
        result = await consume_safe_live_stream(unsafe(), lambda text: None)
        self.assertEqual(result.stop_reason, 'quality')

    async def test_switch_only_controls_prompt_for_each_actor(self):
        self.assertIsNotNone(capabilities.get_capability_def('compose_music'))
        for enabled in (True, False):
            with patch.object(capabilities, 'is_capability_enabled', side_effect=lambda key: enabled and key == 'compose_music'):
                for actor in ('aion', 'connor'):
                    prompt = '\n'.join(await capabilities.build_capability_prompt_items('月亮', who=actor))
                    self.assertEqual('[COMPOSE]' in prompt, enabled)
                excluded = '\n'.join(await capabilities.build_capability_prompt_items('月亮', excluded_capabilities={'compose_music'}))
                self.assertNotIn('[COMPOSE]', excluded)

    def test_composition_block_is_hidden_across_stream_chunks_and_from_context(self):
        from safe_live_stream import KnownCommandStreamFilter
        from context_builder import strip_tool_commands
        command = '[COMPOSE]{"title":"晚灯","code":"note(\"c4\").s(\"piano\")"}[/COMPOSE]'
        stream = KnownCommandStreamFilter()
        text = '给你。' + command + '慢慢听。'
        visible = ''.join(stream.feed(text[index:index + 7]) for index in range(0, len(text), 7))
        tail, error = stream.finish()
        self.assertIsNone(error)
        visible += tail
        self.assertEqual(visible, '给你。慢慢听。')
        self.assertEqual(strip_tool_commands(text), '给你。慢慢听。')


class CompositionHttpCacheTests(unittest.TestCase):
    def test_catalog_uses_304_until_a_piece_changes(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes import compositions as routes
        rows = [{'id':'one', 'code':'note("c4")', 'favorite':False, 'updated_at':1}]
        app = FastAPI()
        app.include_router(routes.router)
        with patch.object(routes, 'list_compositions', AsyncMock(return_value=rows)), TestClient(app) as client:
            first = client.get('/api/compositions')
            etag = first.headers['etag']
            unchanged = client.get('/api/compositions', headers={'If-None-Match':etag})
            self.assertEqual(unchanged.status_code, 304)
            self.assertEqual(unchanged.content, b'')
            rows[0].update(favorite=True, updated_at=2)
            changed = client.get('/api/compositions', headers={'If-None-Match':etag})
            self.assertEqual(changed.status_code, 200)
            self.assertNotEqual(changed.headers['etag'], etag)
            self.assertTrue(changed.json()['compositions'][0]['favorite'])


if __name__ == '__main__':
    unittest.main()
