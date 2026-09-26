import asyncio
from unittest.mock import AsyncMock

import capabilities
import svakom_ai


def test_selection_limits_prompts_and_invalidates_old_reply(monkeypatch):
    import toy_profiles
    import ankni_ai
    monkeypatch.setitem(capabilities.SETTINGS, 'toy_profile', 'svakom')
    monkeypatch.setitem(capabilities.SETTINGS, capabilities.CAPABILITY_SETTINGS_KEY,
                        {'toy': True, 'svakom': True, 'ankni': True})
    monkeypatch.setattr(toy_profiles, 'save_settings', lambda _: None)
    sent = AsyncMock()
    monkeypatch.setattr(svakom_ai.manager, 'broadcast', sent)
    async def run():
        svakom_ai.capture_permission()
        toy_profiles.select('ankni')
        await svakom_ai.process_commands('[SVAKOM:LOOP:1,1,0,0,0]', 'old')
        sent.assert_not_called()
        block = '\n'.join(await capabilities.build_capability_prompt_items('用户', whisper_mode=True))
        assert '[ANKNI:LOOP:' in block
        assert '[SVAKOM:' not in block and '[TOY:' not in block
        await ankni_ai.process_commands('[ANKNI:LOOP:2000,5;3000,6]', 'new')
        assert sent.call_args.args[0]['type'] == 'ankni_command'
        sent.reset_mock()
        toy_profiles.select('sosexy')
        toy_profiles.select('ankni')
        await ankni_ai.process_commands('[ANKNI:LOOP:2000,5]', 'late')
        sent.assert_not_called()
    asyncio.run(run())


def test_selection_api_persists_and_rejects_unknown_profile(monkeypatch):
    import toy_profiles
    from main import app
    from fastapi.testclient import TestClient
    saved = []
    monkeypatch.setattr(toy_profiles, 'save_settings', lambda data: saved.append(dict(data)))
    monkeypatch.setitem(capabilities.SETTINGS, 'toy_profile', 'sosexy')
    client = TestClient(app)
    before = client.get('/api/toys/selection').json()
    result = client.put('/api/toys/selection', json={'profile': 'ankni'}).json()
    assert saved[-1]['toy_profile'] == 'ankni'
    assert result['epoch'] != before['epoch']
    assert client.get('/api/toys/selection').json()['profile'] == 'ankni'
    assert client.put('/api/toys/selection', json={'profile': 'unknown'}).status_code == 422
    assert client.get('/api/toys/selection').json()['profile'] == 'ankni'
    assert client.get('/toys/ankni').status_code == 200


def test_ankni_validation_and_stop_priority(monkeypatch):
    import ankni_ai
    import pytest
    assert ankni_ai.validate_command('LOOP:2000,5;3000,6') == 'LOOP:2000,5;3000,6'
    assert ankni_ai.validate_command('LOOP:2000,5;3000,0;4000,6') == 'LOOP:2000,5;3000,0;4000,6'
    assert ankni_ai.validate_command('LOOP:2000,5;1000,6;1000,0') == 'LOOP:2000,5;1000,6;1000,0'
    for bad in ('LOOP:0,1', 'LOOP:200,11', 'LOOP:2000,5;0,6', 'LOOP:2000,5;199,6', 'LOOP:400000,1;400000,2', 'LOOP:600001,1', 'LOOP:200,5,0', 'LOOP:200,1;', 'SET:5,5'):
        with pytest.raises(ValueError):
            ankni_ai.validate_command(bad)
    monkeypatch.setitem(capabilities.SETTINGS, 'toy_profile', 'ankni')
    monkeypatch.setitem(capabilities.SETTINGS, capabilities.CAPABILITY_SETTINGS_KEY, {'ankni': True})
    sent = AsyncMock()
    monkeypatch.setattr(ankni_ai.manager, 'broadcast', sent)
    async def run():
        ankni_ai.capture_permission()
        assert await ankni_ai.process_commands('正文[ANKNI:LOOP:bad][ANKNI:STOP]', 'stop') == '正文'
        assert sent.call_args.args[0]['data']['command'] == 'STOP'
    asyncio.run(run())
