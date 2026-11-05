"""Saved turns are authoritative; browser animations and model speech are independent."""
import asyncio
import copy
import time
import uuid
from pathlib import Path

from .engine import execute
from .store import SessionStore
from .matches import is_active, reserves_players

ACTORS = {'user', 'aion', 'connor'}
LEASE_SECONDS = 25


def potted_balls(game):
    if 'potted_balls' in game:
        return copy.deepcopy(game['potted_balls'])
    # Older saves retain only the last shot; do not invent earlier pot order.
    balls = [[], []]
    shot = game['state'].get('lastShot') or {}
    shooter = shot.get('shooter')
    if shooter in (0, 1):
        events = shot.get('pocketEvents', [{'ballId': ball} for ball in shot.get('pots', [])])
        balls[shooter] = [e['ballId'] for e in events if 1 <= e['ballId'] <= 15]
    return balls


def update_streaks(game: dict, state: dict):
    shot = state.get('lastShot') or {}
    shooter = shot.get('shooter', 0)
    streak = game['streaks'][shooter]
    group = state['groups'][shooter]
    pots = [ball for ball in shot.get('pots', []) if (group == 'solids' and 1 <= ball <= 7) or (group == 'stripes' and 9 <= ball <= 15)]
    if pots and not shot.get('foul'):
        streak['pots'] += len(pots)
        streak['misses'] = 0
    else:
        streak['pots'] = 0
        streak['misses'] += 1


class BilliardsService:
    def __init__(self, root: Path, *, companionship=True):
        self.store = SessionStore(root)
        self.companionship = companionship
        self.locks = {}
        self.tasks = {}
        self.speech_tasks = set()
        self.chat_locks = {}
        self.report_tasks = {}
        self.create_lock = asyncio.Lock()
        self.invite_lock = asyncio.Lock()
        self.initialized = False

    def lock(self, sid):
        return self.locks.setdefault(sid, asyncio.Lock())

    def get(self, sid):
        return self.store.load(sid)

    async def start(self):
        if self.initialized:
            return
        self.initialized = True
        for game in self.store.all():
            try:
                await self.recover(game['id'])
                game = self.get(game['id'])
                if game.get('ai_preview'):
                    game['ai_preview'] = None
                    self.save(game)
                if game['status'] == 'running' and 'user' in game['players']:
                    game['status'] = 'paused'
                    game['lease'] = None
                    self.save(game)
                elif game['status'] == 'running':
                    self.ensure_loop(game['id'])
            except Exception:
                continue

    def save(self, game):
        game['revision'] += 1
        game['updated_at'] = time.time()
        self.store.save(game)

    async def create(self, players, *, difficulty='medium', accuracy=None, seed=None, mode='casual'):
        if len(players) != 2 or len(set(players)) != 2 or any(p not in ACTORS for p in players):
            raise ValueError('请选择两位不同的参与者')
        if 'user' in players and players[0] != 'user':
            raise ValueError('用户需要在第一个位置')
        if difficulty not in ('low', 'medium', 'high') or mode not in ('casual', 'strict'):
            raise ValueError('规则或难度无效')
        accuracy = {'low': 50, 'medium': 80, 'high': 95}[difficulty] if accuracy is None else accuracy
        if not isinstance(accuracy, (int, float)) or not 0 <= accuracy <= 100:
            raise ValueError('准度需要在 0 到 100 之间')
        async with self.create_lock:
            yield_tables = []
            for existing in self.store.all():
                if is_active(existing) and set(players) & set(existing['players']):
                    if 'user' in players and 'user' not in existing['players']:
                        yield_tables.append(existing['id'])
                    elif 'user' in players or 'user' not in existing['players'] or reserves_players(existing):
                        raise ValueError('参与者已有未结束的球局，请继续或结束那一局')
            seed = seed if seed is not None else int(time.time() * 1000) % 1000000
            state = (await execute(op='new', seed=seed, mode=mode))['state']
            # Pause under the table lock so a shot already committing can settle first.
            for sid in yield_tables:
                await self.pause(sid)
            game = {'id': uuid.uuid4().hex, 'players': list(players), 'state': state,
                    'difficulty': difficulty, 'accuracy': accuracy, 'mode': mode,
                    'status': 'paused', 'revision': 0, 'rack': 1, 'score': [0, 0],
                    'match_format': 'single', 'match_status': 'active',
                    'losses': [0, 0], 'streaks': [{'pots': 0, 'misses': 0}, {'pots': 0, 'misses': 0}],
                    'messages': [], 'potted_balls': [[], []], 'actions': [], 'pending': None, 'last_event': None,
                    'ai_preview': None,
                    'lease': None, 'ready_at': 0, 'last_speech_at': 0, 'room_id': '',
                    'created_at': time.time(), 'updated_at': time.time(), 'error': '', 'result_reports': []}
            self.store.save(game)
            return game

    async def watch(self, *, difficulty='medium'):
        # Share the invitation lock so a manual request and an autonomous invite
        # cannot both start a new table or advance the same saved rack.
        async with self.invite_lock:
            game = next((g for g in self.store.all() if is_active(g) and 'user' not in g['players']), None)
            if game is None:
                game = await self.create(['aion', 'connor'], difficulty=difficulty)
            if game['status'] == 'finished':
                return await self.next_rack(game['id'], '', breaker=game['rack'] % 2)
            if game['status'] == 'running':
                return game
            return await self.resume(game['id'], '')

    async def recover(self, sid):
        async with self.lock(sid):
            game = self.get(sid)
            if game.get('pending'):
                await self.commit_pending(game)
        self.schedule_reports(sid)

    def check_lease(self, game, client):
        lease = game.get('lease') or {}
        if lease.get('client') != client or lease.get('until', 0) < time.time():
            raise ValueError('这个窗口没有控制球桌，请点继续取得控制')

    def tables_to_yield(self, game):
        # Called under create_lock; validate all conflicts before pausing any table.
        tables = []
        for existing in self.store.all():
            if existing['id'] == game['id'] or not reserves_players(existing) or not set(existing['players']) & set(game['players']):
                continue
            if 'user' in game['players'] and 'user' not in existing['players']:
                tables.append(existing['id'])
            else:
                raise ValueError('参与者正在另一场球局里，这桌先保留，之后再继续')
        return tables

    async def resume(self, sid, client):
        await self.recover(sid)
        async with self.create_lock, self.lock(sid):
            game = self.get(sid)
            if game['status'] == 'finished':
                raise ValueError('本局已经结束，可以再来一局')
            yield_tables = self.tables_to_yield(game)
            if 'user' in game['players']:
                if not client:
                    raise ValueError('缺少控制窗口')
                lease = game.get('lease') or {}
                if lease.get('client') != client and lease.get('until', 0) > time.time():
                    raise ValueError('另一个窗口正在控制球桌，你可以在这里旁观')
                game['lease'] = {'client': client, 'until': time.time() + LEASE_SECONDS}
            for other_sid in yield_tables:
                await self.pause(other_sid)
            game['status'] = 'running'
            game['ai_preview'] = None
            game['error'] = ''
            self.save(game)
        self.ensure_loop(sid)
        return self.get(sid)

    async def heartbeat(self, sid, client):
        async with self.lock(sid):
            game = self.get(sid)
            self.check_lease(game, client)
            game['lease']['until'] = time.time() + LEASE_SECONDS
            self.store.save(game)
        return game

    async def pause(self, sid, client=''):
        async with self.lock(sid):
            game = self.get(sid)
            if 'user' in game['players'] and game['status'] == 'running':
                self.check_lease(game, client)
            if game['status'] != 'finished':
                game['status'] = 'paused'
            game['lease'] = None
            game['ai_preview'] = None
            self.save(game)
            return game

    async def act(self, sid, client, revision, action_id, action):
        async with self.lock(sid):
            game = self.get(sid)
            if action_id in game['actions']:
                return game
            if game['status'] != 'running':
                raise ValueError('球局已暂停，先点继续')
            self.check_lease(game, client)
            if game['revision'] != revision:
                raise ValueError('球桌状态已经更新，请重新操作')
            if game['players'][game['state']['turn']] != 'user':
                raise ValueError('现在是伴侣的回合')
            if game['ready_at'] > time.time():
                raise ValueError('请等待这一杆的结果')
            game['pending'] = {'id': action_id, 'action': action, 'actor': game['state']['turn']}
            self.store.save(game)
            await self.commit_pending(game)
        if action.get('type') == 'shot':
            self.say_later(sid, 'shot')
        return self.get(sid)

    async def commit_pending(self, game):
        pending = game['pending']
        before = copy.deepcopy(game['state'])
        previous_pots = potted_balls(game)
        try:
            result = await execute(op='apply', state=before, action=pending['action'], actor=pending['actor'], automatic=pending.get('automatic', False))
        except BaseException:
            # Cancellation leaves the accepted command on disk for recovery.
            if not asyncio.current_task().cancelling():
                game['pending'] = None
                self.store.save(game)
            raise
        game['state'] = result['state']
        if game['state']['seed'] != before['seed']:
            game['potted_balls'] = [[], []]
        game['actions'].append(pending['id'])
        game['actions'] = game['actions'][-1000:]
        game['pending'] = None
        game['ai_preview'] = None
        shot = pending['action']['type'] == 'shot'
        if shot:
            last_shot = game['state'].get('lastShot') or {}
            shooter = last_shot.get('shooter', pending['actor'])
            events = last_shot.get('pocketEvents', [{'ballId': ball} for ball in last_shot.get('pots', [])])
            previous_pots[shooter].extend(e['ballId'] for e in events if 1 <= e['ballId'] <= 15)
            game['potted_balls'] = previous_pots
            update_streaks(game, game['state'])
            game['last_event'] = {'id': pending['id'], 'rack': game['rack'], 'before': before,
                                  'action': pending['action'], 'motion_ms': result['motion_ms']}
        game['ready_at'] = time.time() + ((result['motion_ms'] + 1200) / 1000 if shot else 0)
        if game['state']['phase'] == 'over':
            winner = game['state']['result']['winner']
            game['score'][winner] += 1
            game['losses'][winner] = 0
            game['losses'][1-winner] += 1
            game['status'] = 'finished'
            if game.get('match_format') == 'single' or (game.get('match_format') == 'best_of_3' and game['score'][winner] >= 2):
                game['match_status'] = 'completed'
            game['lease'] = None
            game.setdefault('result_reports', []).append({'rack': game['rack'], 'winner': winner,
                'score': list(game['score']), 'reason': game['state']['result']['reason'],
                'shots': game['state']['shotCount'], 'finished_at': time.time(),
                'match_complete': game.get('match_status') == 'completed',
                'published': False, 'chat_published': False})
        self.save(game)

    async def next_rack(self, sid, client, *, difficulty=None, accuracy=None, mode=None, breaker=0, restart=False):
        async with self.create_lock, self.lock(sid):
            game = self.get(sid)
            if not is_active(game):
                raise ValueError('这场比赛已结束，请返回大厅开始新的一场')
            if restart:
                if game['state']['phase'] == 'over':
                    raise ValueError('本局已有胜负，请进入下一局')
                if 'user' in game['players'] and game['status'] == 'running':
                    self.check_lease(game, client)
            elif game['status'] != 'finished' or game['state']['phase'] != 'over':
                raise ValueError('请先打完当前球局')
            elif game.get('match_format') != 'best_of_3' or game['rack'] >= 3 or max(game['score']) >= 2:
                raise ValueError('这场比赛已结束，请返回大厅开始新的一场')
            self.tables_to_yield(game)
            if breaker not in (0, 1):
                raise ValueError('开球人无效')
            if difficulty is not None:
                if difficulty not in ('low', 'medium', 'high'):
                    raise ValueError('难度无效')
                game['difficulty'] = difficulty
            if accuracy is not None:
                if not 0 <= accuracy <= 100:
                    raise ValueError('准度无效')
                game['accuracy'] = accuracy
            if mode is not None:
                if mode not in ('casual', 'strict'):
                    raise ValueError('规则无效')
                game['mode'] = mode
            game['state'] = (await execute(op='new', seed=game['state']['seed']+1, mode=game['mode'], breaker=breaker))['state']
            game['potted_balls'] = [[], []]
            if not restart:
                game['rack'] += 1
            game['status'] = 'paused'
            game['streaks'] = [{'pots': 0, 'misses': 0}, {'pots': 0, 'misses': 0}]
            game['last_event'] = None
            game['ai_preview'] = None
            game['ready_at'] = 0
            self.save(game)
        return await self.resume(sid, client)

    async def restart_rack(self, sid, client, **options):
        return await self.next_rack(sid, client, restart=True, **options)

    async def finish(self, sid, client):
        async with self.lock(sid):
            game = self.get(sid)
            if 'user' in game['players'] and game['status'] == 'running':
                self.check_lease(game, client)
            game['status'] = 'finished'
            if game.get('match_format') in ('single', 'best_of_3') and game.get('match_status') != 'completed':
                game['match_status'] = 'abandoned'
            game['ai_preview'] = None
            game['lease'] = None
            self.save(game)
        return game

    def ensure_loop(self, sid):
        if sid not in self.tasks or self.tasks[sid].done():
            self.tasks[sid] = asyncio.create_task(self.loop(sid))

    async def loop(self, sid):
        try:
            while True:
                await asyncio.sleep(.4)
                game = self.get(sid)
                if game['status'] != 'running':
                    return
                if 'user' in game['players'] and (game.get('lease') or {}).get('until', 0) < time.time():
                    async with self.lock(sid):
                        game = self.get(sid)
                        if (game.get('lease') or {}).get('until', 0) < time.time():
                            game['status'] = 'paused'
                            game['lease'] = None
                            game['ai_preview'] = None
                            self.save(game)
                    continue
                if game['ready_at'] > time.time() or game['players'][game['state']['turn']] == 'user':
                    continue
                async with self.lock(sid):
                    game = self.get(sid)
                    if game['status'] != 'running':
                        continue
                    if 'user' in game['players'] and (game.get('lease') or {}).get('until', 0) < time.time():
                        game['status'] = 'paused'
                        game['lease'] = None
                        game['ai_preview'] = None
                        self.save(game)
                        continue
                    if game['ready_at'] > time.time():
                        continue
                    if game['players'][game['state']['turn']] == 'user':
                        continue
                    if game['state']['shotCount'] >= 160:
                        game['status'] = 'paused'
                        game['error'] = '这局有点僵持，暂停一下；可以继续或结束球局'
                        self.save(game)
                        return
                    action = (await execute(op='plan', state=game['state'], difficulty=game['difficulty'], accuracy=game['accuracy'], seed=game['state']['seed']+game['state']['shotCount']*31+game['state']['turn']))['action']
                    if 'user' in game['players'] and (game.get('lease') or {}).get('until', 0) < time.time():
                        game['status'] = 'paused'
                        game['lease'] = None
                        game['ai_preview'] = None
                        self.save(game)
                        continue
                    if not action:
                        raise ValueError('伴侣暂时没找到合适的击球动作')
                    preview_id = uuid.uuid4().hex
                    game['ai_preview'] = {'id': preview_id, 'action': copy.deepcopy(action),
                        'actor': game['state']['turn'], 'rack': game['rack'],
                        'started_at': time.time(), 'duration_ms': 2500}
                    self.save(game)
                # Publish the frozen plan before the human-like reaction delay.
                await asyncio.sleep(2.5)
                async with self.lock(sid):
                    game = self.get(sid)
                    preview = game.get('ai_preview')
                    if game['status'] != 'running' or not preview or preview['id'] != preview_id:
                        continue
                    if 'user' in game['players'] and (game.get('lease') or {}).get('until', 0) < time.time():
                        game['status'] = 'paused'
                        game['lease'] = None
                        game['ai_preview'] = None
                        self.save(game)
                        continue
                    action = preview['action']
                    game['pending'] = {'id': preview_id, 'action': action, 'actor': preview['actor'], 'automatic': True}
                    self.store.save(game)
                    await self.commit_pending(game)
                if action['type'] == 'shot':
                    self.say_later(sid, 'shot')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.lock(sid):
                game = self.get(sid)
                game['status'] = 'paused'
                game['error'] = str(exc)
                game['lease'] = None
                game['ai_preview'] = None
                self.save(game)

    def say_later(self, sid, reason):
        if not self.companionship:
            return
        from .companionship import react
        task = asyncio.create_task(react(self, sid, reason))
        self.speech_tasks.add(task)
        task.add_done_callback(self.speech_tasks.discard)
        self.schedule_reports(sid)

    def schedule_reports(self, sid):
        if not self.companionship or not any(not r['published'] or not r.get('chat_published', r['published']) for r in self.get(sid).get('result_reports', [])):
            return
        if sid in self.report_tasks and not self.report_tasks[sid].done():
            return
        from .companionship import publish_result_safely
        report = asyncio.create_task(publish_result_safely(self, sid))
        self.report_tasks[sid] = report
        self.speech_tasks.add(report)
        report.add_done_callback(self.speech_tasks.discard)

    async def stop(self):
        tasks = list(self.tasks.values()) + list(self.speech_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


_service = None


def get_service():
    global _service
    if _service is None:
        from config import DATA_DIR
        _service = BilliardsService(DATA_DIR / 'billiards')
    return _service
