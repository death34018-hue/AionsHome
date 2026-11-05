"""Isolated local QA host. Never opens the real chat DB or calls a real model."""
import sys
import tempfile
from pathlib import Path
from types import ModuleType

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
chatroom = ModuleType('chatroom')
chatroom.get_chatroom_names = lambda: ('试玩用户', '试玩伴侣一', '试玩伴侣二')
sys.modules['chatroom'] = chatroom

from billiards import service
from billiards.routes import install
from billiards.companionship import append_message

tmp = tempfile.TemporaryDirectory(prefix='aions-billiards-qa-')
service._service = service.BilliardsService(Path(tmp.name), companionship=False)
app = FastAPI()
install(app)
app.mount('/entertainment-assets', StaticFiles(directory=str(ROOT / 'entertainment')))
app.mount('/public', StaticFiles(directory=str(ROOT.parent / 'public')))
app.mount('/static', StaticFiles(directory=str(ROOT / 'static')))

@app.get('/playground')
async def lobby():
    return FileResponse(ROOT / 'entertainment' / 'lobby.html')

@app.get('/api/chatroom/config')
async def config():
    return {}

@app.post('/api/qa/autonomous')
async def autonomous():
    game = await service._service.create(['aion', 'connor'], difficulty='low', seed=17)
    await service._service.resume(game['id'], '')
    return game


@app.post('/api/qa/pots')
async def potted_preview():
    game = await service._service.create(['user', 'connor'], seed=17)
    game['potted_balls'] = [[3, 10, 2, 7, 1, 4, 6, 5], [12, 9, 11, 15, 14, 13]]
    for ball in game['state']['balls']:
        ball['pocketed'] = ball['id'] in game['potted_balls'][0] + game['potted_balls'][1]
    game['messages'] = [{'id': 'qa-spoken', 'sender': 'connor', 'rack': 1, 'shot': 8,
                         'text': '【心里嘀咕：连进两个，漂亮。】好球！[语音|方向=带笑|内容=下一杆看我的。]'}]
    service._service.save(game)
    return game

@app.post('/api/qa/abandon/{sid}')
async def abandon(sid: str):
    return await service._service.finish(sid, (service._service.get(sid).get('lease') or {}).get('client', ''))


@app.post('/api/qa/seed')
async def seed_history():
    import time
    for index in range(13):
        players = ['user', 'aion' if index % 2 else 'connor']
        game = await service._service.create(players, seed=17 + index)
        winner = index % 2
        game['score'] = [1, 0] if winner == 0 else [0, 1]
        game['rack'] = sum(game['score'])
        game['status'] = 'finished'
        game['match_status'] = 'completed'
        game['state']['phase'] = 'over'
        game['state']['result'] = {'winner': winner, 'reason': '合法黑八'}
        game['result_reports'] = [{'rack': game['rack'], 'winner': winner, 'score': game['score'],
                                  'reason': '合法黑八', 'shots': 18, 'published': True, 'chat_published': True}]
        game['messages'] = [{'id': f'qa-{index}', 'sender': players[1], 'rack': game['rack'],
                             'shot': 18, 'text': '这场打得不错，下次球桌上再见。'}]
        game['updated_at'] = time.time() - (13-index)*3600
        if index == 0:
            del game['match_format']
            del game['match_status']
            game['score'] = [2, 2]
            game['rack'] = 4
        service._service.store.save(game)
    return {'seeded': 13}


@app.post('/api/qa/win/{sid}/{winner}')
async def settle_for_preview(sid: str, winner: int):
    from unittest.mock import AsyncMock, patch
    from copy import deepcopy
    assert winner in (0, 1)
    async with service._service.lock(sid):
        game = service._service.get(sid)
        state = deepcopy(game['state'])
        state.update(phase='over', result={'winner': winner, 'reason': '合法黑八'}, shotCount=18)
        state['lastShot'] = {'shooter': winner, 'pots': [8], 'foul': None,
                             'pocketEvents': [{'ballId': 8, 'pocketId': 1}]}
        game['pending'] = {'id': f"qa-win-{game['rack']}", 'action': {'type': 'shot'}, 'actor': winner}
        with patch('billiards.service.execute', new=AsyncMock(return_value={'state': state, 'motion_ms': 0})):
            await service._service.commit_pending(game)
        # The fixture already represents a settled result; do not replay its synthetic shot.
        game['last_event'] = None
        game['ready_at'] = 0
        service._service.store.save(game)
    return game

@app.post('/api/qa/ai-turn/{seat}')
async def ai_turn(seat: int):
    assert seat in (0, 1)
    game = await service._service.create(['aion', 'connor'], difficulty='low', seed=17)
    game['state']['turn'] = seat
    game['state']['phase'] = 'aim'
    game['state']['placement'] = None
    service._service.save(game)
    return game

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=18769, log_level='warning')
