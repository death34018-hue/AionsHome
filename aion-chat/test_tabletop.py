"""Small, deterministic checks for the two entertainment-room tables."""
import copy
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import tabletop as game


class DiceTests(unittest.TestCase):
    def test_wild_ones_and_challenge_cost_one_die(self):
        state = game.new_game('liars-dice', ['user', 'aion'], wild=True)
        state['dice'] = {'user': [1, 1, 5, 3, 6], 'aion': [5, 2, 3, 4, 6]}
        game.act(state, 'user', {'action': 'bid', 'count': 4, 'face': 5})
        game.act(state, 'aion', {'action': 'challenge'})
        self.assertEqual(state['result']['actual'], 4)
        self.assertEqual(state['result']['loser'], 'aion')
        self.assertEqual(state['counts']['aion'], 5)
        self.assertEqual(state['result']['net'], {'user': 1, 'aion': -1})
        self.assertEqual(state['phase'], 'round_over')

    def test_calling_ones_disables_wild_and_rejects_lower_bid(self):
        state = game.new_game('liars-dice', ['user', 'aion'], wild=True)
        game.act(state, 'user', {'action': 'bid', 'count': 2, 'face': 1})
        self.assertFalse(state['wild_active'])
        before = copy.deepcopy(state)
        with self.assertRaises(ValueError):
            game.act(state, 'aion', {'action': 'bid', 'count': 1, 'face': 6})
        self.assertEqual(state, before)

    def test_private_dice_are_not_sent_to_other_players(self):
        state = game.new_game('liars-dice', ['user', 'aion', 'connor'])
        public = game.view(state, 'aion')
        self.assertIn('dice', public['seats']['aion'])
        self.assertNotIn('dice', public['seats']['user'])
        self.assertNotIn('dice', public['seats']['connor'])


class PokerTests(unittest.TestCase):
    def test_three_hands_stop_and_keep_per_hand_accounts(self):
        state = game.new_game('texas-holdem', ['user', 'aion'])
        for hand in range(1, 4):
            game.act(state, state['turn'], {'action': 'fold'})
            self.assertEqual(len(state['rounds']), hand)
            self.assertEqual(sum(state['rounds'][-1]['net'].values()), 0)
            self.assertEqual(sum(state['rounds'][-1]['balances'].values()), 1000)
            if hand < 3:
                game.next_round(state)
        self.assertEqual(state['phase'], 'game_over')
        self.assertTrue(state['closed'])
        with self.assertRaises(ValueError):
            game.next_round(state)

    def test_heads_up_blinds_and_postflop_order(self):
        state = game.new_game('texas-holdem', ['user', 'aion'])
        self.assertEqual(state['bets'], {'user': 5, 'aion': 10})
        self.assertEqual(state['turn'], 'user')
        game.act(state, 'user', {'action': 'call'})
        game.act(state, 'aion', {'action': 'check'})
        self.assertEqual(state['street'], 'flop')
        self.assertEqual(len(state['board']), 3)
        self.assertEqual(state['turn'], 'aion')
        self.assertEqual(sum(state['stacks'].values()) + sum(state['contributed'].values()), 1000)

    def test_short_all_in_does_not_reopen_prior_raisers(self):
        state = game.new_game('texas-holdem', ['user', 'aion', 'connor'])
        state['stacks']['connor'] = 25  # already posted 10, can raise only to 35
        game.act(state, 'user', {'action': 'raise', 'amount': 30})
        game.act(state, 'aion', {'action': 'call'})
        game.act(state, 'connor', {'action': 'all_in'})
        self.assertEqual(state['turn'], 'user')
        self.assertNotIn('raise', game.legal(state, 'user')['actions'])
        with self.assertRaises(ValueError):
            game.act(state, 'user', {'action': 'raise', 'amount': 100})
        game.act(state, 'user', {'action': 'call'})
        game.act(state, 'aion', {'action': 'call'})
        self.assertEqual(state['street'], 'flop')

    def test_side_pots_and_unmatched_chips_are_settled(self):
        state = game.new_game('texas-holdem', ['user', 'aion', 'connor'])
        state.update(board=['2S', '3H', '7C', '8D', '9S'],
                     hands={'user': ['AS', 'AH'], 'aion': ['KS', 'KH'], 'connor': ['QS', 'QH']},
                     contributed={'user': 50, 'aion': 100, 'connor': 200},
                     stacks={'user': 0, 'aion': 0, 'connor': 0}, folded=[])
        game.settle_poker(state)
        self.assertEqual(state['stacks'], {'user': 150, 'aion': 100, 'connor': 100})
        self.assertEqual(sum(state['result']['payouts'].values()), 350)

    def test_best_five_wheel_and_board_tie(self):
        wheel = game.hand_rank(['AS', '2S', '3S', '4S', '5S', 'KH', 'KD'])
        quads = game.hand_rank(['AS', 'AH', 'AC', 'AD', 'KS', '2H', '3D'])
        self.assertGreater(wheel, quads)
        self.assertEqual(wheel, (8, 5))
        board = ['AS', 'KS', 'QS', 'JS', '10S']
        self.assertEqual(game.hand_rank(board + ['2H', '3H']), game.hand_rank(board + ['9D', '9C']))

    def test_fold_win_does_not_reveal_private_cards(self):
        state = game.new_game('texas-holdem', ['user', 'aion'])
        game.act(state, 'user', {'action': 'fold'})
        public = game.view(state, 'user')
        self.assertEqual(state['stacks']['aion'], 505)
        self.assertNotIn('hand', public['seats']['aion'])
        self.assertNotIn('deck', public)


class TableApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from routes import table_games
        self.api = table_games
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(table_games, 'DATA_DIR', Path(self.temp.name)))
        self.enterContext(patch.object(table_games, 'DB_PATH', Path(self.temp.name) / 'tables.db'))
        self.enterContext(patch.object(table_games, '_deliver_reports', AsyncMock()))
        self.enterContext(patch.object(table_games.companion, '_player_names', return_value={
            'user': '测试用户', 'aion': '测试同伴', 'connor': '测试伙伴'}))
        table_games._thinking.clear()
        result = await table_games.new_table('liars-dice', table_games.NewTable(opponent='aion', stake=0))
        self.state = result['state']

    def turn(self, state=None, **action):
        state = state or self.state
        return self.api.Turn(game_id=state['id'], revision=state['revision'], **action)

    def test_saved_poker_adopts_three_hands_without_resetting_cards(self):
        state = game.new_game('texas-holdem', ['user', 'aion'])
        state['round_limit'] = 5
        self.api._save(state)
        loaded = self.api._load('texas-holdem')
        self.assertEqual(loaded['round_limit'], 3)
        self.assertEqual(loaded['hands'], state['hands'])
        self.assertEqual(loaded['stacks'], state['stacks'])
        for hand in range(1, 4):
            game.act(loaded, loaded['turn'], {'action': 'fold'})
            if hand < 3:
                game.next_round(loaded)
        self.assertEqual(loaded['phase'], 'game_over')
        self.assertEqual(loaded['end_reason'], '已完成 3 手')

    async def test_new_table_defaults_to_requested_money_amounts(self):
        import tabletop_wallet as money
        with money.connect(self.api.DB_PATH) as db:
            for player in ('aion', 'connor'):
                db.execute('INSERT INTO bookkeeping VALUES (?,?,?,?,?)',
                           (player, money.TYPES[player][0], 1000, 'test funds', 1))
        with patch.object(self.api, '_notify_wallets', AsyncMock()):
            dice = (await self.api.new_table('liars-dice', self.api.NewTable()))['state']
            poker = (await self.api.new_table('texas-holdem', self.api.NewTable()))['state']
        self.assertEqual(dice['wallet']['stake'], 5000)
        self.assertEqual(poker['wallet']['initial'], {'user': 20000, 'aion': 20000, 'connor': 20000})
        self.assertEqual(poker['blind'], 1000)

    async def test_early_poker_report_excludes_unfinished_hand_and_can_retry(self):
        import tabletop_wallet as money
        target = {'type': 'chatroom', 'id': 'latest-group'}
        with patch.object(self.api, 'last_active_group', AsyncMock(return_value=target)):
            state = money.open_table(self.api.DB_PATH, 'texas-holdem', ['user', 'aion'], True, 0)
            await self.api.cashout('texas-holdem', self.turn(state))
            self.assertEqual(money.pending_reports(self.api.DB_PATH), [])

            state = money.open_table(self.api.DB_PATH, 'texas-holdem', ['user', 'aion'], True, 0)
            game.act(state, state['turn'], {'action': 'fold'})
            completed_stacks = state['stacks'].copy()
            game.next_round(state)
            money.save(self.api.DB_PATH, state)
            await self.api.cashout('texas-holdem', self.turn(state))
            closed = money.load(self.api.DB_PATH, 'texas-holdem')
            self.assertEqual(closed['stacks'], completed_stacks)
            reports = money.pending_reports(self.api.DB_PATH)
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]['target'], target)
            self.assertEqual(len(reports[0]['state']['rounds']), 1)
            await self.api._commit(money.load(self.api.DB_PATH, 'texas-holdem'))
            with patch.object(self.api, 'save_chat_card', AsyncMock(return_value=False)) as send:
                await self.api._flush_reports()
                self.assertEqual(len(money.pending_reports(self.api.DB_PATH)), 1)
                self.assertIn('共完成 1 手', send.call_args.args[1])
                self.assertEqual(send.call_args.args[2][0]['rounds_completed'], 1)
            with patch.object(self.api, 'save_chat_card', AsyncMock(return_value=True)) as send:
                await self.api._flush_reports()
                await self.api._flush_reports()
                send.assert_awaited_once()
            self.assertEqual(money.pending_reports(self.api.DB_PATH), [])

    async def user_bid(self):
        result = await self.api.user_action('liars-dice', self.turn(action='bid', count=1, face=2))
        self.state = result['state']
        return self.state

    async def test_duplicate_turn_rejected_and_reload_keeps_progress(self):
        from fastapi import HTTPException
        old = self.turn(action='bid', count=1, face=2)
        await self.user_bid()
        with self.assertRaises(HTTPException) as caught:
            await self.api.user_action('liars-dice', old)
        self.assertEqual(caught.exception.status_code, 409)
        loaded = (await self.api.get_state('liars-dice'))['state']
        self.assertEqual(loaded['turn'], 'aion')
        self.assertEqual(loaded['bid']['count'], 1)
        self.assertNotIn('dice', loaded['seats']['aion'])

    async def test_ai_receives_recent_context_and_controls_actual_bid(self):
        await self.user_bid()
        captured = []
        async def collect(messages, player, model):
            captured.extend(messages)
            return json.dumps({'action': 'bid', 'count': 7, 'face': 6, 'speech': '我敢这么叫。'})
        self.enterContext(patch.object(self.api.companion, '_latest_group_context', AsyncMock(
            return_value=('room', {'context_minutes': 30}, [{'content': '最近一起喝茶'}]))))
        self.enterContext(patch.object(self.api.companion, 'build_aion_group_context', AsyncMock(
            return_value=([{'role': 'system', 'content': '角色设定'}, {'role': 'user', 'content': '最近一起喝茶'}], {}))))
        self.enterContext(patch.object(self.api.companion, '_collect_ai_text', collect))
        result = await self.api.ai_step('liars-dice', self.turn())
        self.assertEqual(result['state']['bid']['count'], 7)
        self.assertEqual(result['state']['history'][-1]['speech'], '我敢这么叫。')
        self.assertIn('最近一起喝茶', json.dumps(captured, ensure_ascii=False))
        snapshot = json.loads(captured[-1]['content'].split('你可见的桌面（含最近桌边上下文与合法动作）：')[1])
        self.assertNotIn('dice', snapshot['seats']['user'])
        self.assertIn('dice', snapshot['seats']['aion'])

    async def test_failed_ai_leaves_turn_for_explicit_retry(self):
        from fastapi import HTTPException
        await self.user_bid()
        before = self.api._load('liars-dice')
        with patch.object(self.api, '_ask_ai', AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(HTTPException) as caught:
                await self.api.ai_step('liars-dice', self.turn())
        self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual(self.api._load('liars-dice'), before)
        self.assertFalse((await self.api.get_state('liars-dice'))['state']['busy'])

    async def test_inflight_ai_does_not_block_reads_or_overwrite_new_game(self):
        from fastapi import HTTPException
        await self.user_bid()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            entered.set()
            await release.wait()
            return {'action': 'challenge'}
        with patch.object(self.api, '_ask_ai', delayed):
            task = asyncio.create_task(self.api.ai_step('liars-dice', self.turn()))
            await entered.wait()
            visible = await asyncio.wait_for(self.api.get_state('liars-dice'), timeout=1)
            self.assertTrue(visible['state']['busy'])
            closed = await self.api.cashout('liars-dice', self.turn())
            self.assertEqual(closed['state']['phase'], 'cancelled')
            new = await self.api.new_table('liars-dice', self.api.NewTable(stake=0))
            release.set()
            with self.assertRaises(HTTPException) as caught:
                await task
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.api._load('liars-dice')['id'], new['state']['id'])

    async def test_table_chat_persists_without_advancing_game_and_reaches_turn_prompt(self):
        before = self.api._load('liars-dice')
        body = self.api.ChatMessage(game_id=before['id'], message_id='test-chat', text='要不要喝点水？')
        with patch.object(self.api, '_chat_reply', AsyncMock(return_value='好，等这一轮结束。')):
            result = await self.api.table_chat('liars-dice', body)
            await self.api.table_chat('liars-dice', body)  # network retry must not repeat dialogue
        current = self.api._load('liars-dice')
        self.assertEqual(current['revision'], before['revision'])
        self.assertEqual(current['dice'], before['dice'])
        self.assertEqual([m['sender'] for m in result['state']['messages']], ['user', 'aion'])
        self.assertEqual(len(current['messages']), 2)
        self.assertIn('要不要喝点水', self.api._prompt(current, 'aion'))

    async def test_chat_reply_from_previous_round_is_not_appended(self):
        before = self.api._load('liars-dice')
        async def delayed(*args):
            current = self.api._load('liars-dice')
            current['round'] += 1
            self.api._save(current)
            return '旧回合的回答'
        with patch.object(self.api, '_chat_reply', delayed):
            result = await self.api.table_chat('liars-dice', self.api.ChatMessage(
                game_id=before['id'], message_id='old-round', text='刚才是谁输了？'))
        self.assertEqual([m['sender'] for m in result['state']['messages']], ['user'])

    async def test_failed_chat_can_retry_without_duplicate_user_message(self):
        body = self.api.ChatMessage(game_id=self.state['id'], message_id='retry-chat', text='你听到了吗？')
        with patch.object(self.api, '_chat_reply', AsyncMock(side_effect=TimeoutError)):
            failed = await self.api.table_chat('liars-dice', body)
        self.assertTrue(failed['state']['chat_error'])
        with patch.object(self.api, '_chat_reply', AsyncMock(return_value='听到了。')):
            result = await self.api.table_chat('liars-dice', body)
        self.assertEqual(len(result['state']['messages']), 2)
        self.assertFalse(result['state']['chat_error'])

    async def test_chat_saved_while_ai_thinks_survives_move_commit(self):
        await self.user_bid()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            entered.set()
            await release.wait()
            return {'action': 'bid', 'count': 2, 'face': 3, 'speech': '那我叫两个三。'}
        with patch.object(self.api, '_ask_ai', delayed), patch.object(self.api, '_chat_reply', AsyncMock(return_value='当然听着。')):
            task = asyncio.create_task(self.api.ai_step('liars-dice', self.turn()))
            await entered.wait()
            await self.api.table_chat('liars-dice', self.api.ChatMessage(
                game_id=self.state['id'], message_id='during-turn', text='还听着我说话吗？'))
            release.set()
            result = await task
        self.assertEqual([m['text'] for m in result['state']['messages']], ['还听着我说话吗？', '当然听着。', '那我叫两个三。'])


class TableMoneyTests(unittest.TestCase):
    def setUp(self):
        import tabletop_wallet
        self.money = tabletop_wallet
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'wallet.db'
        with self.money.connect(self.path) as db:
            for actor, amount in [('aion', 30), ('connor', 80)]:
                db.execute('INSERT INTO bookkeeping VALUES (?,?,?,?,?)',
                           (actor, self.money.TYPES[actor][0], amount, 'test funds', 1))

    def test_buyin_caps_user_and_ai_and_rejects_overbet(self):
        with self.assertRaisesRegex(ValueError, '不能超过'):
            self.money.open_table(self.path, 'texas-holdem', ['user', 'aion', 'connor'], True, 8001)
        state = self.money.open_table(self.path, 'texas-holdem', ['user', 'aion', 'connor'], True, 5000)
        self.assertEqual(state['wallet']['initial'], {'user': 5000, 'aion': 3000, 'connor': 5000})
        self.assertEqual(state['wallet']['user_limit'], 8000)
        self.assertEqual(self.money.balances(self.path), {'aion': 0, 'connor': 3000})
        with self.assertRaises(ValueError):
            game.act(state, 'user', {'action': 'raise', 'amount': 5001})
        with self.assertRaises(ValueError):
            self.money.open_table(self.path, 'texas-holdem', ['user', 'connor'], True, 9000)
        self.assertEqual(self.money.balances(self.path), {'aion': 0, 'connor': 3000})

    def test_dice_payout_and_poker_cashout_only_once(self):
        state = self.money.open_table(self.path, 'liars-dice', ['user', 'aion', 'connor'], True, 1000)
        for turn in range(3):
            state['dice'] = {p: [2] * 5 for p in state['players']}
            game.act(state, 'user', {'action': 'bid', 'count': 15, 'face': 6})
            game.act(state, 'aion', {'action': 'challenge'})
            if turn < 2:
                game.next_round(state)
        self.assertTrue(state['closed'])
        self.assertEqual(len(state['rounds']), 3)
        self.money.save(self.path, state)
        self.money.save(self.path, state)
        self.assertEqual(self.money.balances(self.path), {'aion': 6000, 'connor': 8000})
        state = self.money.open_table(self.path, 'texas-holdem', ['user', 'aion'], True, 2000)
        self.assertEqual(state['blind'], 1000)
        game.act(state, 'user', {'action': 'fold'})
        self.money.save(self.path, state)
        self.money.cashout(self.path, state)
        self.money.cashout(self.path, state)
        self.assertEqual(self.money.balances(self.path)['aion'], 6500)

    def test_mid_hand_cashout_restores_bets_and_preserves_completed_hands(self):
        state = self.money.open_table(self.path, 'texas-holdem', ['user', 'aion'], True, 2000)
        game.act(state, state['turn'], {'action': 'fold'})
        self.money.save(self.path, state)
        completed = state['stacks'].copy()
        game.next_round(state)
        self.money.cashout(self.path, state)
        self.assertEqual(state['stacks'], completed)
        self.assertEqual(state['wallet']['returned'], completed)
        self.assertEqual(sum(state['contributed'].values()), 0)
        self.assertEqual(state['phase'], 'cancelled')
        self.assertEqual(len(state['rounds']), 1)
        before = self.money.balances(self.path)
        self.money.cashout(self.path, state)
        self.assertEqual(self.money.balances(self.path), before)
        self.money.open_table(self.path, 'texas-holdem', ['user', 'aion'], True, 1000)

    def test_dice_and_practice_can_end_during_a_round(self):
        for amount in (0, 500):
            state = self.money.open_table(self.path, 'liars-dice', ['user', 'aion'], True, amount)
            state['dice'] = {p: [2] * 5 for p in state['players']}
            game.act(state, 'user', {'action': 'bid', 'count': 10, 'face': 6})
            game.act(state, 'aion', {'action': 'challenge'})
            game.next_round(state)
            self.money.cashout(self.path, state)
            self.assertTrue(state['closed'])
            self.assertEqual(state['phase'], 'cancelled')
            self.assertEqual(len(state['rounds']), 1)
            if amount:
                self.assertEqual(state['wallet']['net'], {'user': -500, 'aion': 500})

    def test_insufficient_dice_stake_rolls_back_all_debits(self):
        with self.assertRaises(ValueError):
            self.money.open_table(self.path, 'liars-dice', ['user', 'connor', 'aion'], True, 4000)
        self.assertEqual(self.money.balances(self.path), {'aion': 3000, 'connor': 8000})

    def test_third_hand_automatically_returns_wallet_and_cannot_continue(self):
        state = self.money.open_table(self.path, 'texas-holdem', ['user', 'aion'], True, 2000)
        for hand in range(3):
            game.act(state, state['turn'], {'action': 'fold'})
            self.money.save(self.path, state)
            if hand < 2:
                game.next_round(state)
        self.assertEqual(state['wallet']['status'], 'settled')
        self.assertEqual(self.money.balances(self.path)['aion'], 1000 + state['stacks']['aion'])
        before = self.money.balances(self.path)
        self.money.save(self.path, state)
        self.assertEqual(self.money.balances(self.path), before)
        with self.assertRaises(ValueError):
            game.next_round(state)


class TableReportTests(unittest.IsolatedAsyncioTestCase):
    def test_summary_uses_all_players_cumulative_net_instead_of_last_hand_winner(self):
        from tabletop_reports import result_card
        players = ['user', 'aion', 'connor']
        state = game.new_game('texas-holdem', players)
        state.update(phase='game_over', closed=True, round=5,
                     result={'net': {'user': -1000, 'aion': 1000, 'connor': 0}},
                     wallet={'status': 'settled', 'net': {'user': -6000, 'aion': 1000, 'connor': 5000},
                             'returned': {'user': 14000, 'aion': 21000, 'connor': 25000}})
        content, attachments = result_card(state, dict(zip(players, ['用户', '同伴', '伙伴'])))
        self.assertIn('用户：本场 -60.00 元', content)
        self.assertIn('同伴：本场 +10.00 元', content)
        self.assertIn('伙伴：本场 +50.00 元', content)
        self.assertEqual(len(attachments[0]['lines']), 3)
        self.assertEqual(attachments[0]['balanceLabel'], '收桌余额')
        self.assertEqual(attachments[0]['rows'][0], dict(actor='user', name='用户', delta=-6000,
                         net='-60.00 元', balance='140.00 元'))
        self.assertEqual([row['delta'] for row in attachments[0]['rows']], [-6000, 1000, 5000])

    async def test_table_end_group_report_retry_and_early_cashout(self):
        import database
        import tabletop_wallet as money
        from routes import table_games as api
        from billiards.reports import last_active_window
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / 'reports.db'
        self.enterContext(patch.object(api, 'DB_PATH', path))
        self.enterContext(patch.object(database, 'DB_PATH', path))
        self.enterContext(patch.object(api.companion, '_player_names', return_value={
            'user': '用户', 'aion': '同伴', 'connor': '伙伴'}))
        with money.connect(path) as db:
            db.executescript('''
                CREATE TABLE conversations(id TEXT PRIMARY KEY, updated_at REAL);
                CREATE TABLE messages(id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, created_at REAL, attachments TEXT);
                CREATE TABLE chatroom_rooms(id TEXT PRIMARY KEY, type TEXT, updated_at REAL);
                CREATE TABLE chatroom_messages(id TEXT PRIMARY KEY, room_id TEXT, sender TEXT, content TEXT, created_at REAL, attachments TEXT);
                CREATE TABLE sync_events(seq INTEGER PRIMARY KEY, event_type TEXT, entity_type TEXT, entity_id TEXT, payload TEXT, created_at REAL);
                CREATE TABLE runtime_state(key TEXT, value TEXT, updated_at REAL);
                INSERT INTO conversations VALUES ('private', 1);
                INSERT INTO messages VALUES ('user-message','private','user','hi',10,'[]');
                INSERT INTO chatroom_rooms VALUES ('group','group',1);
                INSERT INTO chatroom_messages VALUES ('group-message','group','user','hi',20,'[]');
            ''')
        self.assertEqual(await last_active_window(), {'type': 'chatroom', 'id': 'group'})
        state = money.open_table(path, 'liars-dice', ['user', 'aion'], True, 0)
        for round_number in range(1, 4):
            player = state['turn']
            game.act(state, player, {'action': 'bid', 'count': 1, 'face': 2})
            game.act(state, state['turn'], {'action': 'challenge'})
            if round_number < 3:
                await api._commit(state)
                self.assertEqual(money.pending_reports(path), [])
                game.next_round(state)
        with patch.object(api, 'save_chat_card', AsyncMock(side_effect=RuntimeError('offline'))):
            with self.assertLogs('routes.table_games', level='WARNING'):
                await api._commit(state)
        self.assertEqual(len(money.pending_reports(path)), 1)
        # A later user message must not move a previously addressed report to another window.
        with money.connect(path) as db:
            db.execute("UPDATE messages SET created_at=30 WHERE id='user-message'")
        with patch('sync_events.broadcast_synced', AsyncMock()):
            await api._deliver_reports()
            await api._commit(state)
        self.assertEqual(money.pending_reports(path), [])
        with money.connect(path) as db:
            cards = db.execute("SELECT content,attachments FROM chatroom_messages WHERE sender='system'").fetchall()
            self.assertEqual(len(cards), 1)
            self.assertIn('本场收桌结算', cards[0][0])
            self.assertIn('共完成 3 轮', cards[0][0])
            self.assertEqual(json.loads(cards[0][1])[0]['type'], 'table_game_result')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE role='system'").fetchone()[0], 0)
        # Cashing out after two hands immediately sends one cumulative report.
        with money.connect(path) as db:
            db.execute('INSERT INTO bookkeeping VALUES (?,?,?,?,?)', ('fund','wallet_user',50,'test',1))
        state = money.open_table(path, 'texas-holdem', ['user', 'aion'], True, 2000)
        with patch('sync_events.broadcast_synced', AsyncMock()):
            for hand in range(1, 3):
                game.act(state, state['turn'], {'action': 'fold'})
                await api._commit(state)
                if hand < 2:
                    game.next_round(state)
            with money.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM chatroom_messages WHERE sender='system'").fetchone()[0], 1)
            await api.cashout('texas-holdem', api.Turn(game_id=state['id'], revision=state['revision']))
            with money.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM chatroom_messages WHERE sender='system'").fetchone()[0], 2)
            closed = money.load(path, 'texas-holdem')
            await api._commit(closed)
            await api._deliver_reports()
        with money.connect(path) as db:
            cards = db.execute("SELECT content FROM chatroom_messages WHERE sender='system' ORDER BY created_at").fetchall()
            self.assertEqual(len(cards), 2)
            self.assertIn('共完成 2 手', cards[-1][0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE role='system'").fetchone()[0], 0)
        self.assertEqual(money.pending_reports(path), [])

        # Automatic three-hand closure also emits exactly one report.
        state = money.open_table(path, 'texas-holdem', ['user', 'aion'], True, 2000)
        with patch('sync_events.broadcast_synced', AsyncMock()):
            for hand in range(1, 4):
                while state['phase'] == 'playing':
                    game.act(state, state['turn'], {'action': 'fold'})
                    await api._commit(state)
                with money.connect(path) as db:
                    count = db.execute("SELECT COUNT(*) FROM chatroom_messages WHERE sender='system'").fetchone()[0]
                self.assertEqual(count, 3 if hand == 3 else 2)
                if hand < 3:
                    game.next_round(state)
            await api._commit(state)
        with money.connect(path) as db:
            cards = db.execute("SELECT content FROM chatroom_messages WHERE sender='system' ORDER BY created_at").fetchall()
        self.assertEqual(len(cards), 3)
        self.assertIn('共完成 3 手', cards[-1][0])



if __name__ == '__main__':
    unittest.main()
