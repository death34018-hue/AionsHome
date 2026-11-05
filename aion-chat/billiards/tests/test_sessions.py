import asyncio
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch


class SessionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_rack_completes_match_and_allows_fresh_game(self):
        from billiards.matches import match_info, standings
        for winner in (0, 1):
            game = await self.service.create(['user', 'aion'], seed=17)
            final = await self.settle_rack(game['id'], winner)
            self.assertEqual(final['match_format'], 'single')
            self.assertEqual(final['match_status'], 'completed')
            self.assertEqual(match_info(final)['winner'], winner)
            self.assertEqual(match_info(final)['max_racks'], 1)
            self.assertTrue(final['result_reports'][0]['match_complete'])
            for operation in (self.service.next_rack, self.service.restart_rack):
                with self.assertRaises(ValueError):
                    await operation(game['id'], 'w')
        stats = {s['actor']: s for s in standings(self.service.store.all())}
        self.assertEqual((stats['user']['wins'], stats['user']['match_wins']), (1, 1))
        fresh = await self.service.create(['user', 'aion'], seed=18)
        self.assertEqual((fresh['rack'], fresh['score']), (1, [0, 0]))

    async def test_completed_single_game_moves_from_lobby_to_history(self):
        from billiards.routes import history, lobby
        game = await self.service.create(['aion', 'connor'], seed=17)
        sid = game['id']
        with patch('billiards.routes.get_service', return_value=self.service):
            self.assertEqual((await history(offset=0, limit=10))['total'], 0)
            await self.settle_rack(sid, 0)
            saved_before = self.service.store.path(sid).read_bytes()
            page = await history(offset=0, limit=1)
            self.assertEqual([g['id'] for g in page['games']], [sid])
            self.assertEqual(page['games'][0]['match']['status'], 'completed')
            self.assertFalse(page['has_more'])
            self.assertEqual((await history(offset=1, limit=1))['games'], [])
            home = await lobby()
            self.assertEqual(home['history_count'], 1)
            self.assertNotIn(sid, [g['id'] for g in home['games']])
            self.assertEqual(self.service.store.path(sid).read_bytes(), saved_before)
            self.assertEqual((await lobby())['games'], [])

    async def test_watch_creates_one_ai_table_and_resumes_without_reset(self):
        first, second = await asyncio.gather(self.service.watch(difficulty='low'), self.service.watch())
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['players'], ['aion', 'connor'])
        self.assertEqual(first['difficulty'], 'low')
        self.assertEqual(len(self.service.store.all()), 1)
        paused = await self.service.pause(first['id'])
        resumed = await self.service.watch(difficulty='high')
        self.assertEqual(resumed['state'], paused['state'])
        self.assertEqual(resumed['difficulty'], 'low')
        self.assertEqual(resumed['status'], 'running')

    async def test_watch_starts_fresh_after_one_game_and_preserves_history(self):
        game = await self.service.create(['connor', 'aion'], seed=17)
        game['messages'] = [{'sender': 'connor', 'text': '下一局继续', 'rack': 1}]
        self.service.store.save(game)
        await self.settle_rack(game['id'], 0)
        new = await self.service.watch()
        self.assertNotEqual(new['id'], game['id'])
        self.assertEqual((new['rack'], new['score']), (1, [0, 0]))
        self.assertEqual(self.service.get(game['id'])['messages'], game['messages'])
        self.assertEqual(self.service.get(game['id'])['score'], [1, 0])

    async def test_watch_waits_for_running_user_table(self):
        game = await self.service.create(['user', 'aion'], seed=17)
        await self.service.resume(game['id'], 'w')
        with self.assertRaises(ValueError):
            await self.service.watch()
        self.assertEqual(len(self.service.store.all()), 1)
        await self.service.pause(game['id'], 'w')
        watching = await self.service.watch()
        self.assertNotIn('user', watching['players'])
        self.assertEqual(self.service.get(game['id'])['status'], 'paused')

    async def test_potted_balls_keep_shooter_and_order_across_reload_then_reset(self):
        from copy import deepcopy
        game = await self.service.create(['user', 'aion'], seed=17)
        sid = game['id']
        for shooter, ids in [(0, [3, 10, 0]), (1, [12, 2]), (0, [7])]:
            game = self.service.get(sid)
            state = deepcopy(game['state'])
            state['shotCount'] += 1
            state['lastShot'] = {'shooter': shooter, 'pots': ids, 'pocketEvents': [{'ballId': ball, 'pocketId': 1} for ball in ids]}
            game['pending'] = {'id': f"pots-{state['shotCount']}", 'action': {'type': 'shot'}, 'actor': shooter}
            with patch('billiards.service.execute', new=AsyncMock(return_value={'state': state, 'motion_ms': 0})):
                await self.service.commit_pending(game)
        self.assertEqual(self.service.get(sid)['potted_balls'], [[3, 10, 7], [12, 2]])
        await self.service.restart_rack(sid, 'w')
        self.assertEqual(self.service.get(sid)['potted_balls'], [[], []])

    async def test_ai_stops_after_one_rack_and_waits_across_restart(self):
        game = await self.service.create(['aion', 'connor'], seed=17)
        await self.settle_rack(game['id'], 0)
        with patch.object(self.service, 'next_rack', new=AsyncMock(side_effect=ValueError('unexpected next rack'))) as next_rack:
            await self.service.loop(game['id'])
            next_rack.assert_not_awaited()
        saved = self.service.get(game['id'])
        self.assertEqual((saved['status'], saved['rack'], saved['score']), ('finished', 1, [1, 0]))
        self.service.initialized = False
        with patch.object(self.service, 'ensure_loop') as loop:
            await self.service.start()
            loop.assert_not_called()

    async def test_user_can_take_table_and_ai_preserves_progress_until_user_pauses(self):
        game = await self.service.create(['aion', 'connor'], seed=17)
        game['status'] = 'running'
        game['messages'] = [{'sender': 'aion', 'text': '等会儿接着打'}]
        game['ai_preview'] = {'id': 'not-struck-yet'}
        self.service.store.save(game)
        user_game = await self.service.create(['user', 'connor'], seed=18)
        paused = self.service.get(game['id'])
        self.assertEqual(paused['status'], 'paused')
        self.assertEqual(paused['state'], game['state'])
        self.assertEqual(paused['messages'], game['messages'])
        self.assertIsNone(paused['ai_preview'])
        await self.service.resume(user_game['id'], 'w')
        with self.assertRaises(ValueError):
            await self.service.resume(game['id'], '')
        await self.service.pause(user_game['id'], 'w')
        resumed = await self.service.resume(game['id'], '')
        self.assertEqual(resumed['state'], game['state'])
        self.assertEqual(resumed['rack'], 1)
        await self.service.resume(user_game['id'], 'w')
        self.assertEqual(self.service.get(game['id'])['status'], 'paused')
        self.assertEqual(self.service.get(user_game['id'])['state'], user_game['state'])
        await self.service.finish(user_game['id'], 'w')
        await self.service.pause(game['id'])
        await self.settle_rack(game['id'], 0)
        user_game = await self.service.create(['user', 'aion'], seed=19)
        await self.service.resume(user_game['id'], 'w')
        with self.assertRaises(ValueError):
            await self.service.next_rack(game['id'], '')
        await self.service.pause(user_game['id'], 'w')
        second = await self.service.watch()
        self.assertEqual((second['rack'], second['score']), (1, [0, 0]))
        await self.settle_rack(user_game['id'], 0)
        fresh = await self.service.create(['user', 'aion'], seed=20)
        continued = await self.service.resume(fresh['id'], 'w')
        self.assertEqual((continued['rack'], continued['score'], continued['status']), (1, [0, 0], 'running'))
        self.assertEqual(self.service.get(second['id'])['status'], 'paused')

    async def settle_rack(self, sid, winner):
        game = self.service.get(sid)
        state = dict(game['state'], phase='over', result={'winner': winner, 'reason': '合法黑八'})
        game['pending'] = {'id': f"rack-{game['rack']}", 'action': {'type': 'shot'}, 'actor': winner}
        with patch('billiards.service.execute', new=AsyncMock(return_value={'state': state, 'motion_ms': 0})):
            await self.service.commit_pending(game)
        return self.service.get(sid)

    async def test_historical_two_zero_series_keeps_original_rules(self):
        game = await self.service.create(['user', 'aion'], seed=17)
        game['match_format'] = 'best_of_3'
        self.service.store.save(game)
        sid = game['id']
        first = await self.settle_rack(sid, 0)
        self.assertEqual(first.get('match_status'), 'active')
        # The user still has one saved series, even though its partner is free.
        with self.assertRaises(ValueError):
            await self.service.create(['user', 'connor'], seed=17)
        await self.service.next_rack(sid, 'w')
        final = await self.settle_rack(sid, 0)
        self.assertEqual(final['score'], [2, 0])
        self.assertEqual(final['match_status'], 'completed')
        with self.assertRaises(ValueError):
            await self.service.next_rack(sid, 'w')
        self.assertEqual(self.service.get(sid)['rack'], 2)

    async def test_historical_tied_series_keeps_original_winner(self):
        game = await self.service.create(['user', 'connor'], seed=17)
        game['match_format'] = 'best_of_3'
        self.service.store.save(game)
        sid = game['id']
        await self.settle_rack(sid, 0)
        await self.service.next_rack(sid, 'w')
        await self.settle_rack(sid, 1)
        deciding = await self.service.next_rack(sid, 'w')
        self.assertEqual(deciding['rack'], 3)
        self.assertEqual(deciding.get('match_status'), 'active')
        final = await self.settle_rack(sid, 1)
        self.assertEqual(final['score'], [1, 2])
        self.assertEqual(self.service.get(sid)['match_status'], 'completed')
        with self.assertRaises(ValueError):
            await self.service.next_rack(sid, 'w')

    async def test_restart_keeps_rack_score_and_chat_and_abandon_has_no_winner(self):
        game = await self.service.create(['user', 'aion'], seed=17)
        sid = game['id']
        game['messages'] = [{'sender': 'user', 'text': '继续', 'rack': 1}]
        self.service.store.save(game)
        self.assertTrue(hasattr(self.service, 'restart_rack'), '缺少本局重摆，旧版会多算局号')
        restarted = await self.service.restart_rack(sid, 'w')
        self.assertEqual((restarted['rack'], restarted['score']), (1, [0, 0]))
        self.assertEqual(len(restarted['messages']), 1)
        ended = await self.service.finish(sid, 'w')
        self.assertEqual(ended.get('match_status'), 'abandoned')
        with self.assertRaises(ValueError):
            await self.service.next_rack(sid, 'w')

    def test_legacy_and_abandoned_scores_count_racks_but_not_series_champions(self):
        from billiards.matches import standings, is_active
        games = [
            {'players': ['user', 'connor'], 'score': [2, 2], 'status': 'finished'},
            {'players': ['user', 'aion'], 'score': [2, 0], 'status': 'finished',
             'match_format': 'best_of_3', 'match_status': 'completed'},
            {'players': ['aion', 'connor'], 'score': [1, 0], 'status': 'finished',
             'match_format': 'best_of_3', 'match_status': 'abandoned'},
            {'players': ['aion', 'connor'], 'score': [0, 1], 'status': 'finished',
             'match_format': 'best_of_3', 'match_status': 'active'},
        ]
        stats = {s['actor']: s for s in standings(games)}
        self.assertEqual((stats['user']['wins'], stats['user']['losses'], stats['user']['match_wins']), (4, 2, 1))
        self.assertEqual((stats['connor']['wins'], stats['connor']['losses'], stats['connor']['match_wins']), (3, 3, 0))
        self.assertEqual((stats['aion']['wins'], stats['aion']['losses'], stats['aion']['match_losses']), (1, 3, 1))
        self.assertFalse(is_active(games[0]))
        self.assertTrue(is_active(games[3]))

    async def asyncSetUp(self):
        from billiards.service import BilliardsService
        self.tmp = tempfile.TemporaryDirectory()
        self.service = BilliardsService(Path(self.tmp.name), companionship=False)

    async def asyncTearDown(self):
        if hasattr(self, 'service'):
            await self.service.stop()
        if hasattr(self, 'tmp'):
            self.tmp.cleanup()

    async def test_saved_game_reload_and_duplicate_action(self):
        from billiards.service import BilliardsService
        game = await self.service.create(['user', 'aion'], difficulty='low', seed=17)
        sid = game['id']
        await self.service.resume(sid, 'window-a')
        game = self.service.get(sid)
        first = await self.service.act(sid, 'window-a', game['revision'], 'place-1', {'type': 'confirmPlacement'})
        again = await self.service.act(sid, 'window-a', game['revision'], 'place-1', {'type': 'confirmPlacement'})
        self.assertEqual(first['revision'], again['revision'])
        self.assertEqual(first['state']['phase'], 'aim')
        other = BilliardsService(Path(self.tmp.name), companionship=False)
        self.assertEqual(other.get(sid)['state'], first['state'])
        await other.stop()

    async def test_pause_preserves_board_and_blocks_moves_and_second_window(self):
        game = await self.service.create(['user', 'connor'], seed=8)
        sid = game['id']
        await self.service.resume(sid, 'window-a')
        game = self.service.get(sid)
        game['ai_preview'] = {'id': 'active-preview', 'action': {'type': 'shot', 'shot': {'angle': .2, 'power': 60}}}
        self.service.store.save(game)
        with self.assertRaises(ValueError):
            await self.service.resume(sid, 'window-b')
        self.assertEqual(self.service.get(sid)['ai_preview']['id'], 'active-preview')
        paused = await self.service.pause(sid, 'window-a')
        self.assertIsNone(paused.get('ai_preview'))
        before = paused['state']
        with self.assertRaises(ValueError):
            await self.service.act(sid, 'window-a', paused['revision'], 'x', {'type': 'confirmPlacement'})
        resumed = await self.service.resume(sid, 'window-a')
        self.assertEqual(resumed['state'], before)

    async def test_stale_revision_and_out_of_turn_are_rejected(self):
        game = await self.service.create(['user', 'aion'], seed=4)
        sid = game['id']
        game = await self.service.resume(sid, 'w')
        with self.assertRaises(ValueError):
            await self.service.act(sid, 'w', -1, 'x', {'type': 'confirmPlacement'})
        game = await self.service.act(sid, 'w', game['revision'], 'confirm', {'type': 'confirmPlacement'})
        game = await self.service.act(sid, 'w', game['revision'], 'shot', {'type': 'shot', 'shot': {'angle': 0, 'power': 80}})
        self.assertEqual(game['state']['shotCount'], 1)
        duplicate = await self.service.act(sid, 'w', game['revision']-1, 'shot', {'type': 'shot', 'shot': {'angle': 0, 'power': 80}})
        self.assertEqual(duplicate['state']['shotCount'], 1)

    async def test_pending_shot_is_recovered_once(self):
        from billiards.service import BilliardsService
        game = await self.service.create(['user', 'aion'], difficulty='low', seed=17)
        game['state']['phase'] = 'aim'
        game['pending'] = {'id': 'interrupted', 'action': {'type': 'shot', 'shot': {'angle': 0, 'power': 80}}, 'actor': 0}
        self.service.store.save(game)
        reloaded = BilliardsService(Path(self.tmp.name), companionship=False)
        await reloaded.recover(game['id'])
        self.assertEqual(reloaded.get(game['id'])['state']['shotCount'], 1)
        await reloaded.recover(game['id'])
        self.assertEqual(reloaded.get(game['id'])['state']['shotCount'], 1)
        await reloaded.stop()

    async def test_algorithm_can_confirm_seat_zero_placement(self):
        from billiards.engine import execute
        game = await self.service.create(['aion', 'connor'], difficulty='low', seed=17)
        action = (await execute(op='plan', state=game['state'], difficulty='low', accuracy=50, seed=17))['action']
        self.assertEqual(action['type'], 'place')
        game['pending'] = {'id': 'automatic-place', 'action': action, 'actor': 0, 'automatic': True}
        await self.service.commit_pending(game)
        self.assertEqual(game['state']['phase'], 'aim')

    async def test_expired_control_during_preparation_cannot_accept_ai_turn(self):
        game = await self.service.create(['user', 'aion'], seed=17)
        game['status'] = 'running'
        game['state']['turn'] = 1
        game['lease'] = {'client': 'gone', 'until': time.time()+20}
        self.service.store.save(game)
        async def advance(seconds):
            if seconds == 2.5:
                current = self.service.get(game['id'])
                current['lease']['until'] = time.time()-1
                self.service.store.save(current)
        with patch('billiards.service.asyncio.sleep', new=advance), patch('billiards.service.execute', new=AsyncMock(return_value={'action': {'type': 'shot', 'shot': {'angle': .4, 'power': 60}}})) as engine:
            await self.service.loop(game['id'])
            self.assertEqual([call.kwargs['op'] for call in engine.await_args_list], ['plan'])
            self.assertEqual(self.service.get(game['id'])['status'], 'paused')
            self.assertIsNone(self.service.get(game['id']).get('ai_preview'))

    async def test_ai_preparation_exposes_frozen_shot_then_commits_same_direction_once(self):
        from billiards.engine import execute
        game = await self.service.create(['user', 'aion'], seed=17)
        game['status'] = 'running'
        game['state']['turn'] = 1
        game['state']['phase'] = 'aim'
        game['lease'] = {'client': 'w', 'until': time.time()+20}
        self.service.store.save(game)
        action = {'type': 'shot', 'shot': {'angle': .07, 'power': 70}}
        preview_id = None
        async def engine(**kwargs):
            return {'action': action} if kwargs['op'] == 'plan' else await execute(**kwargs)
        async def advance(seconds):
            nonlocal preview_id
            current = self.service.get(game['id'])
            if seconds == 2.5:
                preview = current.get('ai_preview')
                self.assertIsNotNone(preview, 'AI delay must publish a cue preview before striking')
                self.assertEqual(current['state']['shotCount'], 0)
                self.assertEqual(preview['action']['shot']['angle'], .07)
                preview_id = preview['id']
                action['shot']['angle'] = 1.2
            elif current['state']['shotCount']:
                current['status'] = 'finished'
                self.service.store.save(current)
        with patch('billiards.service.asyncio.sleep', new=advance), patch('billiards.service.execute', new=engine):
            await self.service.loop(game['id'])
        settled = self.service.get(game['id'])
        self.assertEqual(settled['state']['shotCount'], 1)
        self.assertEqual(settled['last_event']['id'], preview_id)
        self.assertEqual(settled['last_event']['action']['shot']['angle'], .07)
        self.assertIsNone(settled.get('ai_preview'))

    async def test_finished_match_cannot_restart_over_another_match(self):
        first = await self.service.create(['user', 'aion'], seed=17)
        await self.service.finish(first['id'], '')
        await self.service.create(['user', 'connor'], seed=17)
        with self.assertRaises(ValueError):
            await self.service.next_rack(first['id'], 'w')
        self.assertEqual(self.service.get(first['id'])['rack'], 1)

    def test_streaks_count_legal_own_pots_and_reset_on_miss(self):
        from billiards.service import update_streaks
        game = {'streaks': [{'pots': 0, 'misses': 0}, {'pots': 0, 'misses': 0}]}
        state = {'groups': ['solids', 'stripes'], 'lastShot': {'shooter': 0, 'foul': None, 'pots': [1, 2]}}
        update_streaks(game, state)
        self.assertEqual(game['streaks'][0]['pots'], 2)
        state['lastShot'] = {'shooter': 0, 'foul': None, 'pots': [9]}
        update_streaks(game, state)
        self.assertEqual(game['streaks'][0], {'pots': 0, 'misses': 1})


if __name__ == '__main__':
    unittest.main()
