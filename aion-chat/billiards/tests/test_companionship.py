import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


class CompanionshipTests(unittest.IsolatedAsyncioTestCase):
    async def test_table_prompt_keeps_daily_conversation_without_system_commands(self):
        from billiards.companionship import model_text
        persona = '[核心身份]\n喜欢打球\n\n[能力与工具规则]\n积极使用 [PAT:拍了拍用户]\n\n[关系锚点]\n在意家人'
        labels = {'user': '测试用户', 'aion': '伴侣一', 'connor': '伴侣二'}
        daily_history = [{'role': 'user', 'content': '历史消息：今天一起做了蛋糕。[PAT:揉了揉头]【心里嘀咕：偷偷高兴】[语音|方向=带笑|内容=蛋糕很好吃。]'}]
        game = {'players': ['user', 'connor'], 'rack': 1, 'score': [0, 0],
                'status': 'paused', 'losses': [0, 0], 'streaks': [{'pots': 0, 'misses': 0}] * 2,
                'state': {'groups': [None, None], 'turn': 0, 'phase': 'aim', 'balls': []},
                'messages': [{'sender': 'user', 'text': '刚刚那杆真漂亮'},
                             {'sender': 'connor', 'text': '[PAT:拍了拍自己]【心里嘀咕：继续练习】[语音|方向=带笑|内容=下一杆也试试。]'}]}
        for actor in ('aion', 'connor'):
            with self.subTest(actor=actor), patch('autonomy.load_worldbook', return_value={
                'ai_persona': persona, 'user_persona': '喜欢轻松闲聊',
            }), patch('autonomy._names', return_value=tuple(labels.values())), patch(
                'chatroom._read_connor_persona', return_value=persona,
            ), patch('autonomy._latest_group_room_id', new=AsyncMock(return_value='group')), patch(
                'autonomy.fetch_merged_timeline', new=AsyncMock(return_value=[]),
            ), patch('autonomy.render_merged_timeline', return_value=daily_history), patch(
                'billiards.companionship.names', return_value=labels,
            ), patch('autonomy._call_actor', new=AsyncMock(return_value='好球！')) as model:
                self.assertEqual(await model_text(actor, game, '自然回应'), '好球！')
                context = model.await_args.args[1]
                prompt = '\n'.join(m['content'] for m in context)
                self.assertIn('喜欢打球', prompt)
                self.assertIn('在意家人', prompt)
                self.assertIn('喜欢轻松闲聊', prompt)
                self.assertIn('刚刚那杆真漂亮', prompt)
                self.assertIn('今天一起做了蛋糕。', prompt)
                self.assertIn('蛋糕很好吃。', prompt)
                self.assertIn('下一杆也试试。', prompt)
                self.assertIn('"score": [0, 0]', prompt)
                self.assertNotIn('[PAT:', prompt)
                self.assertNotIn('[语音', prompt)
                self.assertNotIn('心里嘀咕', prompt)
                self.assertNotIn('能力与工具规则', prompt)
                self.assertTrue(any(m['role'] == 'system' for m in context))
        self.assertIn('[PAT:', daily_history[0]['content'])

    def test_table_speech_removes_monologue_and_unwraps_voice_dialogue(self):
        from billiards.companionship import spoken_text
        self.assertEqual(spoken_text('【心里嘀咕：她连进两个，好厉害。]漂亮！[语音|方向=带笑|内容=下一杆到我了。]'), '漂亮！下一杆到我了。')
        self.assertEqual(spoken_text('<think>先分析人设</think>好球。\n【内心独白：继续夸她'), '好球。')
        self.assertEqual(spoken_text('[心里嘀咕：今天手气好]'), '')
        self.assertEqual(spoken_text('好球！[PAT:拍了拍用户]'), '好球！')
        from billiards.routes import public
        game = {'status': 'paused', 'state': {}, 'ready_at': 0, 'messages': [
            {'sender': 'aion', 'text': '【心里嘀咕：偷偷高兴】好球！[语音|方向=带笑|内容=该我啦。]'},
            {'sender': 'user', 'text': '【心里嘀咕：用户原文保留】'},
        ]}
        with patch('billiards.routes.names', return_value={}):
            shown = public(game)
        self.assertEqual(shown['messages'][0]['text'], '好球！该我啦。')
        self.assertEqual(shown['messages'][1], game['messages'][1])
        self.assertIn('心里嘀咕', game['messages'][0]['text'])

    async def test_next_invitation_starts_new_game_but_resumes_paused_game(self):
        import sys
        from types import ModuleType
        from billiards.service import BilliardsService
        from billiards.companionship import invite
        autonomy = ModuleType('autonomy')
        autonomy._ask_actor_json = AsyncMock(return_value={'accept': True, 'message': '好，接着打。'})
        autonomy.append_idle_event = AsyncMock()
        config = ModuleType('autonomy_state')
        config.get_actor_config = AsyncMock(return_value={'enabled': True, 'actions': {'billiards_play': True}})
        with tempfile.TemporaryDirectory() as directory:
            service = BilliardsService(Path(directory), companionship=False)
            with patch.dict(sys.modules, {'autonomy': autonomy, 'autonomy_state': config}), patch('billiards.service.get_service', return_value=service), patch('billiards.companionship.names', return_value={'aion': '伴侣一', 'connor': '伴侣二'}), patch.object(service, 'ensure_loop'):
                await service.start()
                user = await service.create(['user', 'aion'], seed=18)
                await service.resume(user['id'], 'w')
                await service.pause(user['id'], 'w')
                first = await invite('aion')
                self.assertIn('billiards_id', first)
                game = service.get(first['billiards_id'])
                sid = game['id']
                game.update(status='finished', score=[1, 0], match_status='completed')
                game['state'].update(phase='over', result={'winner': 0, 'reason': '合法黑八'})
                service.save(game)
                next_invite = await invite('connor')
                self.assertNotEqual(next_invite['billiards_id'], sid)
                self.assertEqual(service.get(sid)['score'], [1, 0])
                sid = next_invite['billiards_id']
                second = service.get(sid)
                self.assertEqual((second['rack'], second['score'], second['players']), (1, [0, 0], ['connor', 'aion']))
                self.assertEqual(len(second['messages']), 1)
                await service.resume(user['id'], 'w')
                calls = autonomy._ask_actor_json.await_count
                self.assertNotIn('billiards_id', await invite('connor'))
                self.assertEqual(autonomy._ask_actor_json.await_count, calls)
                await service.pause(user['id'], 'w')
                continued = await invite('aion')
                self.assertEqual(continued['billiards_id'], sid)
                self.assertEqual(service.get(sid)['rack'], 1)
                self.assertEqual(service.get(sid)['state'], second['state'])
                self.assertEqual(service.get(user['id'])['state'], user['state'])
            await service.stop()

    def test_latest_facts_include_groups_score_streaks_without_coordinates(self):
        from billiards.companionship import facts
        game = {'players': ['user', 'connor'], 'rack': 3, 'score': [1, 1],
                'status': 'paused', 'losses': [0, 1],
                'streaks': [{'pots': 3, 'misses': 0}, {'pots': 0, 'misses': 2}],
                'state': {'groups': ['solids', 'stripes'], 'turn': 1,
                          'phase': 'aim', 'balls': [{'id': 1, 'pocketed': True, 'x': .3}, {'id': 9, 'pocketed': False, 'x': .5}],
                          'lastShot': {'shooter': 0, 'pots': [1], 'foul': None}, 'result': None}}
        info = facts(game, {'user': '测试用户', 'connor': '测试伴侣'})
        self.assertEqual(info['players'][1]['group'], '花色')
        self.assertEqual(info['players'][1]['remaining'], [9])
        self.assertEqual(info['score'], [1, 1])
        self.assertEqual(info['players'][0]['consecutive_pots'], 3)
        # Check coordinate keys, not letters in unrelated metadata such as max_racks.
        self.assertNotIn("'x':", str(info))
        self.assertNotIn("'y':", str(info))

    def test_cooldown_and_milestone_avoid_speaking_every_shot(self):
        from billiards.companionship import reaction_reason
        game = {'last_speech_at': 100, 'state': {'phase': 'aim', 'lastShot': {'pots': [1], 'foul': None}, 'groups': ['solids', 'stripes'], 'balls': [{'id': 1, 'pocketed': False}, {'id': 9, 'pocketed': False}]},
                'streaks': [{'pots': 1, 'misses': 0}, {'pots': 0, 'misses': 0}]}
        self.assertEqual(reaction_reason(game, 'shot', now=150), '')
        game['streaks'][0]['pots'] = 3
        self.assertTrue(reaction_reason(game, 'shot', now=150))
        self.assertFalse(reaction_reason(game, 'shot', now=105))
        self.assertFalse(reaction_reason(game, 'shot', now=129.9))
        self.assertTrue(reaction_reason(game, 'shot', now=130))
        self.assertEqual(reaction_reason(game, 'start', now=150), '')
        self.assertEqual(reaction_reason(game, 'resume', now=150), '')

    async def test_queued_reply_does_not_move_into_next_rack(self):
        from billiards.service import BilliardsService
        from billiards.companionship import chat
        with tempfile.TemporaryDirectory() as directory:
            service = BilliardsService(Path(directory), companionship=False)
            game = await service.create(['user', 'aion'], seed=17)
            lock = asyncio.Lock()
            service.chat_locks[game['id']] = lock
            await lock.acquire()
            with patch('billiards.companionship.model_text', new=AsyncMock(return_value='旧回复')) as model:
                task = asyncio.create_task(chat(service, game['id'], '刚刚那杆'))
                while not service.get(game['id'])['messages']:
                    await asyncio.sleep(0)
                current = service.get(game['id'])
                current['rack'] += 1
                service.save(current)
                lock.release()
                await task
                model.assert_not_awaited()
                self.assertEqual(len(service.get(game['id'])['messages']), 1)
            await service.stop()

    async def test_table_dialogue_never_opens_or_writes_daily_chat(self):
        from billiards.service import BilliardsService
        from billiards.companionship import append_message
        import sys
        from types import ModuleType
        database = ModuleType('database')
        database.get_db = Mock(side_effect=AssertionError('桌边消息不应访问日常聊天数据库'))
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {'database': database}):
            service = BilliardsService(Path(directory), companionship=False)
            game = await service.create(['user', 'connor'], seed=17)
            await append_message(service, game['id'], 'user', '偷偷练过吧？')
            await append_message(service, game['id'], 'connor', '下一杆再试试。')
            self.assertEqual(len(service.get(game['id'])['messages']), 2)
            database.get_db.assert_not_called()
            await service.stop()

    async def test_result_publication_does_not_wait_for_speech(self):
        from billiards.service import BilliardsService
        with tempfile.TemporaryDirectory() as directory:
            service = BilliardsService(Path(directory))
            game = await service.create(['user', 'aion'], seed=17)
            game['state']['phase'] = 'over'
            game['result_reports'] = [{'rack': 1, 'published': False}]
            service.store.save(game)
            gate = asyncio.Event()
            async def slow_speech(*args):
                await gate.wait()
            with patch('billiards.companionship.react', new=slow_speech), patch('billiards.companionship.publish_result_safely', new=AsyncMock()) as publish:
                service.say_later(game['id'], 'shot')
                await asyncio.sleep(0)
                publish.assert_awaited_once()
                gate.set()
                await service.stop()

    async def test_unpublished_result_is_scheduled_after_reload(self):
        from billiards.service import BilliardsService
        with tempfile.TemporaryDirectory() as directory:
            service = BilliardsService(Path(directory))
            game = await service.create(['user', 'aion'], seed=17)
            game['status'] = 'finished'
            game['result_reports'] = [{'rack': 1, 'published': False}]
            service.store.save(game)
            restored = BilliardsService(Path(directory))
            with patch('billiards.companionship.publish_result_safely', new=AsyncMock()) as publish:
                await restored.start()
                await asyncio.sleep(0)
                publish.assert_awaited_once()
            await restored.stop()
            await service.stop()


if __name__ == '__main__':
    unittest.main()
