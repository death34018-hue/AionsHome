"""Read real digest queries against isolated data, stopping before AI generation."""
import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite


ROOT = Path(__file__).resolve().parent


class MessagesCaptured(BaseException):
    pass


class DigestWindowScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        async with self.connect() as db:
            await db.executescript("""
                CREATE TABLE messages (id TEXT, conv_id TEXT, role TEXT, content TEXT,
                                       attachments TEXT, created_at REAL);
                CREATE TABLE chatroom_rooms (id TEXT, type TEXT, updated_at REAL);
                CREATE TABLE chatroom_messages (id TEXT, room_id TEXT, sender TEXT,
                                                content TEXT, created_at REAL);
                CREATE TABLE chatroom_digest_anchors (room_id TEXT, anchor_ts REAL);
                INSERT INTO chatroom_digest_anchors VALUES ('connor_unified', 20);
                INSERT INTO messages VALUES
                    ('p-old', 'old', 'user', 'old window new message', NULL, 11),
                    ('p-new', 'new', 'assistant', 'new window', NULL, 30),
                    ('p-boundary', 'old', 'user', 'already summarized', NULL, 10);
            """)
            for room, kind, updated in (
                ('g-old', 'group', 1), ('g-new', 'group', 100),
                ('c-old', 'connor_1v1', 1), ('c-new', 'connor_1v1', 100),
                ('excluded', 'other', 200),
            ):
                await db.execute("INSERT INTO chatroom_rooms VALUES (?,?,?)", (room, kind, updated))
                for ts, sender in ((10, 'user'), (15, 'user'), (20, 'user'), (25, 'connor'), (26, 'system')):
                    await db.execute("INSERT INTO chatroom_messages VALUES (?,?,?,?,?)",
                                     (f'{room}-{ts}', room, sender, 'message', ts))
            await db.commit()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def connect(self):
        return aiosqlite.connect(self.db_path)

    async def collect(self, filename, function_name):
        # Load the complete production function without unrelated hardware imports.
        tree = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == function_name)
        env = {'get_db': self.connect, 'aiosqlite': aiosqlite, 'load_digest_anchor': lambda: 10}
        exec(compile(ast.Module(body=[node], type_ignores=[]), filename, 'exec'), env)
        captured = {}

        async def capture(db, target, messages):
            captured.update(target=target, messages=messages)
            raise MessagesCaptured()

        with patch('homecoming.summary_coverage.filter_uncovered', side_effect=capture), patch(
            'ai_providers.simple_ai_call', new_callable=AsyncMock
        ) as model:
            with self.assertRaises(MessagesCaptured):
                await env[function_name]()
            model.assert_not_awaited()
        return captured

    async def test_main_includes_all_private_and_group_windows_after_its_anchor(self):
        result = await self.collect('memory.py', '_do_digest')
        self.assertEqual(result['target'], 'main')
        messages = result['messages']
        self.assertEqual({m['_source_id'] for m in messages}, {
            'private:p-old', 'private:p-new',
            *(f'chatroom:{room}-{ts}' for room in ('g-old', 'g-new') for ts in (15, 20, 25)),
        })
        self.assertEqual([m['created_at'] for m in messages], sorted(m['created_at'] for m in messages))

    async def test_second_uses_its_own_anchor_across_all_private_and_group_windows(self):
        result = await self.collect('chatroom.py', 'digest_chatroom')
        self.assertEqual(result['target'], 'second')
        self.assertEqual({m['_source_id'] for m in result['messages']}, {
            f'chatroom:{room}-25' for room in ('g-old', 'g-new', 'c-old', 'c-new')
        })
        self.assertEqual({m['_source'] for m in result['messages']}, {'private', 'group'})

    async def capture_full_text_prompt(self, target, complete=False):
        import chatroom
        import memory

        # The meaningful details occur well beyond the old 300-character cutoff.
        user_text = '长消息背景。' * 100 + '\n最后一条告别视频达到37万播放。'
        assistant_text = '长回复背景。' * 100 + '\n这是创作阶段结束的重要告别。'
        async with self.connect() as db:
            await db.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?)', [
                ('long-user', 'new', 'user', user_text, None, 40),
                ('long-ai', 'new', 'assistant', assistant_text, None, 41),
            ])
            await db.executemany('INSERT INTO chatroom_messages VALUES (?,?,?,?,?)', [
                ('long-c-user', 'c-new', 'user', user_text, 40),
                ('long-c-ai', 'c-new', 'connor', assistant_text, 41),
                ('long-g-user', 'g-new', 'user', user_text, 42),
                ('long-g-ai', 'g-new', 'aion', assistant_text, 43),
                *((f'fill-{i}', 'g-new', 'user', '短消息', 50 + i) for i in range(36)),
            ])
            await db.commit()

        captured = []

        async def capture(value, *args, **kwargs):
            captured.append(value[0]['content'] if isinstance(value, list) else value)
            if complete:
                return '{"memories":[],"profile_updates":[]}'
            # Stop at the external model boundary, before writing memories or anchors.
            raise MessagesCaptured()

        async def persist(context, result):
            self.assertEqual(context['prompt'], '\n伴侣画像维护测试材料')
            self.assertEqual(result['profile_updates'], [])
            raise MessagesCaptured()

        async def uncovered(db, scope, messages):
            return messages, set()

        module = memory if target == 'main' else chatroom
        with patch.object(module, 'get_db', self.connect), patch.object(
            module, 'load_worldbook', return_value={'user_name': 'Mira', 'ai_name': 'Sol'}
        ), patch('homecoming.summary_coverage.filter_uncovered', side_effect=uncovered), patch(
            'profile_digest.prepare', AsyncMock(return_value={'prompt': '\n伴侣画像维护测试材料'} if complete else None)
        ), patch('profile_digest.store_result', side_effect=persist):
            with self.assertRaises(MessagesCaptured):
                if target == 'main':
                    with patch.object(memory, 'load_digest_anchor', return_value=10), patch.object(
                        memory, '_get_active_model_and_conv', AsyncMock(return_value=('test', 'new'))
                    ), patch('ai_providers.simple_ai_call', side_effect=capture):
                        await memory._do_digest()
                else:
                    with patch.object(chatroom, 'get_chatroom_names', return_value=('Mira', 'Sol', 'Nova')), patch.object(
                        chatroom, '_read_connor_persona', return_value=''
                    ), patch.object(chatroom, 'simple_connor_cli_call', side_effect=capture):
                        await chatroom.digest_chatroom(model_key='test')
        return captured[0], user_text, assistant_text

    async def test_main_sends_complete_long_user_and_assistant_messages(self):
        prompt, user_text, assistant_text = await self.capture_full_text_prompt('main')
        self.assertEqual(prompt.count(user_text), 2)  # Private and group messages.
        self.assertEqual(prompt.count(assistant_text), 2)

    async def test_second_sends_complete_long_user_and_assistant_messages(self):
        prompt, user_text, assistant_text = await self.capture_full_text_prompt('second')
        self.assertEqual(prompt.count(user_text), 2)  # Companion private and group messages.
        self.assertEqual(prompt.count(assistant_text), 2)

    async def test_both_digest_paths_pass_profile_material_and_persist_returned_updates(self):
        for target in ('main', 'second'):
            with self.subTest(target=target):
                prompt, _, _ = await self.capture_full_text_prompt(target, complete=True)
                self.assertIn('伴侣画像维护测试材料', prompt)

    async def test_both_digests_label_peer_message_with_participants_and_actual_speaker(self):
        async with self.connect() as db:
            await db.execute("INSERT INTO chatroom_rooms VALUES ('cr_peer_dm','peer_dm',44)")
            await db.execute("INSERT INTO chatroom_messages VALUES ('peer-1','cr_peer_dm','connor','一起去找北极星',44)")
            await db.commit()
        with patch('memory._connor_display_name', return_value='Nova'):
            for target in ('main', 'second'):
                with self.subTest(target=target):
                    prompt, _, _ = await self.capture_full_text_prompt(target)
                    self.assertIn('[id=chatroom:peer-1][Sol与Nova私聊] Nova: 一起去找北极星', prompt)


if __name__ == '__main__':
    unittest.main()
