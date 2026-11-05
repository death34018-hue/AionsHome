"""Rules and player-visible views for small, local entertainment tables."""
import random
import time
import uuid
from collections import Counter
from itertools import combinations

KINDS = {'liars-dice': '吹牛骰子', 'texas-holdem': '德州扑克'}
POKER_ROUND_LIMIT = 3
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
HAND_NAMES = ['高牌', '一对', '两对', '三条', '顺子', '同花', '葫芦', '四条', '同花顺']


def new_game(kind, players, wild=True, *, stacks=None, blind=10, dice_stake=1):
    if kind not in KINDS or len(players) not in (2, 3) or players[0] != 'user':
        raise ValueError('请选择两人或三人牌桌')
    if len(set(players)) != len(players) or not set(players) <= {'user', 'aion', 'connor'}:
        raise ValueError('玩家选择不正确')
    state = dict(id=uuid.uuid4().hex, kind=kind, players=players, revision=0,
                 history=[], messages=[], round=0, phase='playing', turn='user', updated_at=time.time())
    if kind == 'liars-dice':
        state.update(counts={p: 5 for p in players}, wild=wild, dice_mode='three_rounds',
                     dice_stake=dice_stake, dice_net={p: 0 for p in players}, rounds=[], round_limit=3)
        _roll(state, 'user')
    else:
        initial = dict(stacks) if stacks else {p: 500 for p in players}
        state.update(stacks=initial.copy(), starting_stacks=initial, rounds=[], round_limit=POKER_ROUND_LIMIT,
                     dealer=players[0], blind=blind)
        _deal(state)
    return state


def _clockwise(state, after, choices):
    order = state['players']
    start = order.index(after)
    return next((order[(start + i) % len(order)] for i in range(1, len(order) + 1)
                 if order[(start + i) % len(order)] in choices), None)


def _record(state, player, text, speech=''):
    state['history'].append(dict(player=player, text=text, speech=str(speech or '')[:160],
                                 round=state['round'], ts=time.time()))
    state['history'] = state['history'][-80:]
    if str(speech or '').strip():
        append_message(state, player, str(speech), event=text)


def append_message(state, sender, text, *, message_id=None, reply_to=None, event=''):
    message = dict(id=message_id or 'table_' + uuid.uuid4().hex, sender=sender,
                   text=text[:2000], round=state['round'], created_at=time.time(), event=event)
    if reply_to:
        message['reply_to'] = reply_to
    state.setdefault('messages', []).append(message)
    state['messages'] = state['messages'][-200:]
    state['chat_updated_at'] = time.time()
    return message


def _roll(state, starter):
    state.update(round=state['round'] + 1, phase='playing', turn=starter, bid=None,
                 wild_active=state['wild'], result=None,
                 dice={p: [random.randint(1, 6) for _ in range(n)] for p, n in state['counts'].items()})


def _integer(value, label):
    if type(value) is not int:
        raise ValueError(f'{label}必须是整数')
    return value


def legal(state, player):
    if state['phase'] != 'playing' or state['turn'] != player:
        return {'actions': []}
    if state['kind'] == 'liars-dice':
        last = state['bid']
        total = sum(state['counts'].values())
        bids = [{'count': n, 'face': face} for n in range(1, total + 1) for face in range(1, 7)
                if not last or (n, face) > (last['count'], last['face'])]
        return {'actions': (['bid'] if bids else []) + (['challenge'] if last else []), 'bids': bids}
    owed = max(0, state['current_bet'] - state['bets'][player])
    stack = state['stacks'][player]
    maximum = state['bets'][player] + stack
    minimum = state['current_bet'] + state['last_raise']
    previous = state['acted_at'].get(player)
    reopened = previous is None or state['current_bet'] - previous >= state['last_raise']
    opponents_can_bet = any(p != player and p not in state['folded'] and state['stacks'][p] > 0
                            for p in state['hands'])
    can_raise = reopened and opponents_can_bet and maximum > state['current_bet']
    actions = ['fold', 'call' if owed else 'check']
    if can_raise and maximum >= minimum:
        actions.append('raise')
    if stack > 0 and (maximum <= state['current_bet'] or can_raise):
        actions.append('all_in')
    return dict(actions=actions, to_call=min(owed, stack), min_raise=minimum, max_raise=maximum)


def act(state, player, decision):
    if not isinstance(decision, dict):
        raise ValueError('动作必须是 JSON 对象')
    allowed = legal(state, player)
    action = decision.get('action')
    if action not in allowed['actions']:
        raise ValueError('当前不能执行这个动作，请根据合法动作重试')
    if state['kind'] == 'liars-dice':
        _dice_action(state, player, decision, allowed)
    else:
        _poker_action(state, player, decision, allowed)


def _dice_action(state, player, decision, allowed):
    if decision['action'] == 'bid':
        count = _integer(decision.get('count'), '数量')
        face = _integer(decision.get('face'), '点数')
        if {'count': count, 'face': face} not in allowed['bids']:
            raise ValueError('叫数要更大；数量相同时，点数要更大，且不能超过全桌骰子数')
        state['bid'] = dict(player=player, count=count, face=face)
        if face == 1:
            state['wild_active'] = False
        _record(state, player, f'叫 {count} 个 {face} 点', decision.get('speech'))
        state['turn'] = _clockwise(state, player, [p for p, n in state['counts'].items() if n])
        return
    bid = state['bid']
    actual = sum(d == bid['face'] or (d == 1 and state['wild_active'])
                 for dice in state['dice'].values() for d in dice)
    loser = player if actual >= bid['count'] else bid['player']
    winner = bid['player'] if loser == player else player
    if state.get('dice_mode') == 'three_rounds':
        net = {p: (state['dice_stake'] if p == winner else -state['dice_stake'] if p == loser else 0)
               for p in state['players']}
        for p, n in net.items():
            state['dice_net'][p] += n
        state['result'] = dict(bid=bid.copy(), challenger=player, actual=actual, loser=loser, winner=winner,
                               net=net, revealed={p: list(d) for p, d in state['dice'].items()})
        state['rounds'].append(dict(round=state['round'], net=net.copy(),
                                   balances={p: state['dice_stake'] * 3 + n for p, n in state['dice_net'].items()}))
        finished = state['round'] >= 3
        best = max(state['dice_net'].values())
        leaders = [p for p, n in state['dice_net'].items() if n == best]
        state.update(phase='game_over' if finished else 'round_over', turn=None, closed=finished,
                     winner=leaders[0] if finished and len(leaders) == 1 else None,
                     end_reason='已完成 3 轮' if finished else '')
        _record(state, player, f'开骰！实际 {actual} 个 {bid["face"]} 点', decision.get('speech'))
        return
    state['counts'][loser] -= 1
    state['result'] = dict(bid=bid.copy(), challenger=player, actual=actual, loser=loser,
                           revealed={p: list(d) for p, d in state['dice'].items()})
    survivors = [p for p, n in state['counts'].items() if n]
    state['phase'] = 'game_over' if len(survivors) == 1 else 'round_over'
    state['winner'] = survivors[0] if len(survivors) == 1 else None
    state['turn'] = None
    _record(state, player, f'开骰！实际 {actual} 个 {bid["face"]} 点', decision.get('speech'))


def next_round(state):
    if state.get('closed'):
        raise ValueError('这桌已经收桌，请重新入座')
    if state['phase'] != 'round_over':
        raise ValueError('本轮尚未结束，或整局已经结束')
    if state['kind'] == 'texas-holdem' and state['round'] >= state.get('round_limit', POKER_ROUND_LIMIT):
        _finish_poker(state)
        return
    if state['kind'] == 'liars-dice':
        loser = state['result']['loser']
        starter = loser if state['counts'][loser] else _clockwise(
            state, loser, [p for p, n in state['counts'].items() if n])
        _roll(state, starter)
    else:
        active = [p for p in state['players'] if state['stacks'][p] > 0]
        if len(active) == 2:
            big = _clockwise(state, state['big_blind'], active)
            state['dealer'] = next(p for p in active if p != big)
        else:
            state['dealer'] = _clockwise(state, state['dealer'], active)
        _deal(state)


def _pay(state, player, amount):
    amount = min(amount, state['stacks'][player])
    state['stacks'][player] -= amount
    state['bets'][player] += amount
    state['contributed'][player] += amount


def _deal(state):
    blind = state.get('blind', 10)
    active = [p for p in state['players'] if state['stacks'][p] > 0]
    deck = [rank + suit for rank in RANKS for suit in 'SHCD']
    random.shuffle(deck)
    state.update(round=state['round'] + 1, phase='playing', street='preflop', board=[],
                 deck=deck, hands={p: [deck.pop(), deck.pop()] for p in active},
                 bets={p: 0 for p in active}, contributed={p: 0 for p in active},
                 folded=[], current_bet=blind, last_raise=blind, acted_at={}, result=None,
                 pending=list(active), showdown=False)
    small = state['dealer'] if len(active) == 2 else _clockwise(state, state['dealer'], active)
    big = _clockwise(state, small, active)
    state.update(small_blind=small, big_blind=big)
    _pay(state, small, blind // 2)
    _pay(state, big, blind)
    state['turn'] = _clockwise(state, big, active)
    _advance_poker(state, big)


def _poker_action(state, player, decision, allowed):
    action = decision['action']
    old_bet = state['current_bet']
    target = state['bets'][player]
    if action == 'raise':
        target = _integer(decision.get('amount'), '加注到的总额')
        if not allowed['min_raise'] <= target <= allowed['max_raise']:
            raise ValueError(f'本轮总下注须为 {allowed["min_raise"]}～{allowed["max_raise"]}')
    elif action == 'all_in':
        target = allowed['max_raise']
    elif action == 'call':
        target += allowed['to_call']
    if action == 'fold':
        state['folded'].append(player)
    else:
        _pay(state, player, target - state['bets'][player])
    if target > old_bet:
        increment = target - old_bet
        if increment >= state['last_raise']:
            state['last_raise'] = increment
        state['current_bet'] = target
        state['pending'] = [p for p in state['hands'] if p != player and p not in state['folded']
                            and state['stacks'][p] > 0 and state['bets'][p] < target]
    else:
        state['pending'] = [p for p in state['pending'] if p != player]
    # Checking before any wager retains the right to raise even a short opening all-in.
    state['acted_at'][player] = -state.get('blind', 10) if action == 'check' and old_bet == 0 else state['current_bet']
    shown = f'¥{target / 100:.2f}' if state.get('wallet') else str(target)
    labels = {'fold': '弃牌', 'check': '过牌', 'call': f'跟注，本轮共 {shown}',
              'raise': f'加注到 {shown}', 'all_in': f'全下，本轮共 {shown}'}
    _record(state, player, labels[action], decision.get('speech'))
    _advance_poker(state, player)


def _advance_poker(state, after):
    alive = [p for p in state['hands'] if p not in state['folded']]
    if len(alive) == 1:
        settle_poker(state)
        return
    can_bet = [p for p in alive if state['stacks'][p] > 0]
    state['pending'] = [p for p in state['pending'] if p in can_bet]
    # A lone funded player only needs to answer outstanding opposing chips.
    if len(can_bet) <= 1:
        lone = can_bet[0] if can_bet else None
        opposing = max((state['bets'][p] for p in alive if p != lone), default=0)
        if lone is None or state['bets'][lone] >= opposing:
            while len(state['board']) < 5:
                state['board'].append(state['deck'].pop())
            settle_poker(state)
            return
        state['current_bet'] = opposing
        state['pending'] = [lone]
    if state['pending']:
        state['turn'] = _clockwise(state, after, state['pending'])
        return
    if state['street'] == 'river':
        settle_poker(state)
        return
    streets = ['preflop', 'flop', 'turn', 'river']
    state['street'] = streets[streets.index(state['street']) + 1]
    for _ in range(3 if state['street'] == 'flop' else 1):
        state['board'].append(state['deck'].pop())
    state.update(bets={p: 0 for p in state['hands']}, current_bet=0, last_raise=state.get('blind', 10),
                 acted_at={}, pending=can_bet, turn=_clockwise(state, state['dealer'], can_bet))


def _rank_five(cards):
    values = [RANKS.index(c[:-1]) + 2 for c in cards]
    counts = Counter(values)
    groups = sorted(((n, v) for v, n in counts.items()), reverse=True)
    unique = sorted(counts, reverse=True)
    straight = 5 if unique == [14, 5, 4, 3, 2] else (
        unique[0] if len(unique) == 5 and unique[0] - unique[-1] == 4 else 0)
    flush = len({c[-1] for c in cards}) == 1
    if flush and straight:
        return (8, straight)
    if groups[0][0] == 4:
        return (7, groups[0][1], groups[1][1])
    if [g[0] for g in groups] == [3, 2]:
        return (6, groups[0][1], groups[1][1])
    if flush:
        return (5, *unique)
    if straight:
        return (4, straight)
    if groups[0][0] == 3:
        return (3, groups[0][1], *sorted((v for n, v in groups[1:]), reverse=True))
    if [g[0] for g in groups[:2]] == [2, 2]:
        return (2, groups[0][1], groups[1][1], groups[2][1])
    if groups[0][0] == 2:
        return (1, groups[0][1], *sorted((v for n, v in groups[1:]), reverse=True))
    return (0, *unique)


def hand_rank(cards):
    return max(_rank_five(list(hand)) for hand in combinations(cards, 5))


def settle_poker(state):
    alive = [p for p in state['hands'] if p not in state['folded']]
    showdown = len(alive) > 1
    ranks = {p: hand_rank(state['hands'][p] + state['board']) for p in alive} if showdown else {}
    payouts = {p: 0 for p in state['players']}
    pots = []
    previous = 0
    for level in sorted(set(state['contributed'].values()) - {0}):
        contributors = [p for p, n in state['contributed'].items() if n >= level]
        amount = (level - previous) * len(contributors)
        previous = level
        eligible = [p for p in alive if p in contributors]
        if len(contributors) == 1:
            winners = contributors  # Return the uncalled part of an oversized wager.
        elif not showdown:
            winners = alive
        else:
            best = max(ranks[p] for p in eligible)
            winners = [p for p in eligible if ranks[p] == best]
        order = []
        seat = state['dealer']
        for _ in winners:
            seat = _clockwise(state, seat, [p for p in winners if p not in order])
            order.append(seat)
        share, extra = divmod(amount, len(order))
        for i, p in enumerate(order):
            payouts[p] += share + (i < extra)
        pots.append(dict(amount=amount, winners=order, refund=len(contributors) == 1))
    for p, amount in payouts.items():
        state['stacks'][p] += amount
    state.update(showdown=showdown, turn=None, pending=[],
                 result=dict(payouts=payouts, pots=pots,
                             net={p: payouts[p] - state['contributed'].get(p, 0) for p in payouts},
                             hand_names={p: HAND_NAMES[r[0]] for p, r in ranks.items()}))
    if not any(r['round'] == state['round'] for r in state.setdefault('rounds', [])):
        state['rounds'].append(dict(round=state['round'], net=state['result']['net'].copy(),
                                   balances=state['stacks'].copy(), hand_names=state['result']['hand_names'].copy()))
    survivors = [p for p, n in state['stacks'].items() if n > 0]
    state['phase'], state['winner'] = 'round_over', None
    if len(survivors) == 1 or state['round'] >= state.get('round_limit', POKER_ROUND_LIMIT):
        _finish_poker(state)


def _finish_poker(state):
    initial = (state.get('wallet') or {}).get('initial') or state.get('starting_stacks', {})
    net = {p: state['stacks'][p] - initial.get(p, 500) for p in state['players']}
    best = max(net.values())
    winners = [p for p, n in net.items() if n == best]
    state.update(phase='game_over', closed=True, turn=None, pending=[], winner=winners[0] if len(winners) == 1 else None,
                 leaders=winners, end_reason=f"已完成 {state['round']} 手" if state['round'] >= state.get('round_limit', POKER_ROUND_LIMIT) else '桌上只剩一位有余额')


def view(state, player='user'):
    """Whitelist only information this seat is allowed to see; never send deck/state wholesale."""
    public = {key: state.get(key) for key in
              ('id', 'kind', 'revision', 'players', 'phase', 'turn', 'round', 'history', 'result', 'winner')}
    public['legal'] = legal(state, player)
    public['messages'] = state.get('messages', [])[-200:]
    public['chat_error'] = state.get('chat_error', '')
    public['chat_updated_at'] = state.get('chat_updated_at', 0)
    public['wallet'] = state.get('wallet')
    public['closed'] = state.get('closed', False)
    public['blind'] = state.get('blind', 10)
    public['round_limit'] = state.get('round_limit', POKER_ROUND_LIMIT if state['kind'] == 'texas-holdem' else None)
    public['rounds'] = state.get('rounds', [])
    public['end_reason'] = state.get('end_reason', '')
    public['starting_stacks'] = state.get('starting_stacks', {})
    public['dice_mode'] = state.get('dice_mode', 'elimination')
    public['dice_net'] = state.get('dice_net', {})
    public['seats'] = {}
    if state['kind'] == 'liars-dice':
        public.update(bid=state['bid'], wild=state['wild'], wild_active=state['wild_active'],
                      total_dice=sum(len(d) for d in state['dice'].values()))
        for p in state['players']:
            seat = dict(count=state['counts'][p])
            if p == player or state['phase'] != 'playing':
                seat['dice'] = list(state['dice'][p])
            public['seats'][p] = seat
    else:
        public.update(board=list(state['board']), street=state['street'], dealer=state['dealer'],
                      small_blind=state['small_blind'], big_blind=state['big_blind'],
                      pot=sum(state['contributed'].values()), current_bet=state['current_bet'])
        for p in state['players']:
            seat = dict(stack=state['stacks'][p], bet=state['bets'].get(p, 0),
                        contributed=state['contributed'].get(p, 0), folded=p in state['folded'],
                        in_hand=p in state['hands'])
            if p in state['hands'] and (p == player or (state['showdown'] and p not in state['folded'])):
                seat['hand'] = list(state['hands'][p])
            public['seats'][p] = seat
    return public
