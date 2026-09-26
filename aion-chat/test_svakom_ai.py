import asyncio
import pytest
import capabilities
import context_builder
import svakom_ai
from unittest.mock import AsyncMock


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setitem(capabilities.SETTINGS, 'toy_profile', 'svakom')
    monkeypatch.setattr(svakom_ai, '_reports', {}, raising=False)
    monkeypatch.setitem(capabilities.SETTINGS, capabilities.CAPABILITY_SETTINGS_KEY,
                        {item.key: False for item in capabilities.CAPABILITY_DEFS})
    monkeypatch.setattr(capabilities, 'save_settings', lambda settings: None)
    monkeypatch.setattr(context_builder, 'get_device_context_for_prompt', lambda: '')
    return monkeypatch


def test_new_prompt_is_independent_and_in_final_ability_block(isolated):
    async def run():
        capabilities.set_capability_enabled('svakom', True)
        for who in ('aion', 'connor'):
            block = await context_builder.build_ability_block('测试用户', who=who, whisper_mode=False)
            assert '【新玩具独立编排指南】' in block
            assert '[SVAKOM:LOOP:' in block and '[SVAKOM:STOP]' in block
            assert '三短一长' in block and '低速方波脉冲' in block
            assert '[TOY:' not in block
        capabilities.set_capability_enabled('svakom', False)
        block = await context_builder.build_ability_block('测试用户', whisper_mode=True)
        assert '[SVAKOM:' not in block
    asyncio.run(run())


def test_reference_strength_uses_latest_controller_report_and_expires(isolated):
    from fastapi.testclient import TestClient
    from main import app
    clock = [100.0]
    isolated.setattr(svakom_ai, 'monotonic', lambda: clock[0], raising=False)
    capabilities.set_capability_enabled('svakom', True)
    client = TestClient(app)
    report = dict(source='phone-test', sequence=1, connected=True, flap=4, vibrate=2, vibrate_level=3)
    assert client.post('/api/svakom-ai/state', json=report).status_code == 200
    async def prompt():
        return await context_builder.build_ability_block('测试用户', whisper_mode=False)
    assert '当前参考强度：6/10' in asyncio.run(prompt())
    client.post('/api/svakom-ai/state', json={**report, 'sequence': 2, 'flap': 0, 'vibrate_level': 9})
    # Older/out-of-order reports cannot overwrite a newer slider value.
    client.post('/api/svakom-ai/state', json=report)
    assert '当前参考强度：9/10' in asyncio.run(prompt())
    client.post('/api/svakom-ai/state', json={**report, 'source': 'idle-page', 'connected': False})
    assert '当前参考强度：9/10' in asyncio.run(prompt())
    client.post('/api/svakom-ai/state', json={**report, 'source': 'second-controller'})
    assert '当前参考强度：未知' in asyncio.run(prompt())
    client.post('/api/svakom-ai/state', json={**report, 'source': 'second-controller', 'sequence': 2, 'connected': False})
    assert '当前参考强度：9/10' in asyncio.run(prompt())
    assert client.post('/api/svakom-ai/state', json={**report, 'vibrate_level': 11}).status_code == 422
    client.post('/api/svakom-ai/state', json={**report, 'sequence': 3, 'flap': 0, 'vibrate': 0, 'vibrate_level': 0})
    assert '当前参考强度：0/10' in asyncio.run(prompt())
    client.post('/api/svakom-ai/state', json={**report, 'sequence': 4, 'connected': False})
    assert '当前参考强度：未知' in asyncio.run(prompt())
    client.post('/api/svakom-ai/state', json={**report, 'sequence': 5})
    clock[0] += 16
    assert '当前参考强度：未知' in asyncio.run(prompt())
    capabilities.set_capability_enabled('svakom', False)
    assert '当前参考强度' not in asyncio.run(prompt())


def test_command_notices_persist_but_never_enter_model_history(isolated, tmp_path):
    import json
    import aiosqlite
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def test_db():
        async with aiosqlite.connect(tmp_path / 'notices.db') as db:
            yield db
    isolated.setattr(svakom_ai, 'get_db', test_db, raising=False)
    sent = []
    isolated.setattr(svakom_ai.manager, 'broadcast', AsyncMock(side_effect=sent.append))
    async def run():
        async with test_db() as db:
            for table, scope, role in (('messages', 'conv_id', 'role'), ('chatroom_messages', 'room_id', 'sender')):
                await db.execute(f'CREATE TABLE {table} (id TEXT PRIMARY KEY, {scope} TEXT, {role} TEXT, content TEXT, created_at REAL, attachments TEXT)')
            await db.commit()
        capabilities.set_capability_enabled('svakom', True)
        for key, table in (('conv_id', 'messages'), ('room_id', 'chatroom_messages')):
            svakom_ai.capture_permission()
            raw = '[SVAKOM:LOOP:3,1,0,0,4;2,0,0,0,0]'
            assert await svakom_ai.process_commands('正文。' + raw, key, **{key: 'test-scope'}) == '正文。'
            async with test_db() as db:
                row = await (await db.execute(f'SELECT content, attachments FROM {table}')).fetchone()
            assert row is not None and 'SVAKOM:' not in row[0]
            attachments = json.loads(row[1])
            notice = next(a for a in attachments if a['type'] == 'svakom_command_notice')
            assert notice['raw'] == raw and '广播' in notice['status']
            assert {'type': 'system_notice_order', 'after_msg_id': key} in attachments
            # Even a context-visible keyword cannot override the display-only marker.
            message = dict(sender='system', content='搜索了 ' + row[0], attachments=attachments, source='private', created_at=1)
            assert context_builder.render_merged_timeline([message], 'aion') == []
        assert [e['type'] for e in sent].count('svakom_command') == 2
        assert any(e['type'] == 'msg_created' for e in sent)
        assert any(e['type'] == 'chatroom_msg_created' for e in sent)
        # Persist the ANKNI grammar so later display never reinterprets old end-time rows.
        raw_ankni = '[ANKNI:LOOP:2000,5;3000,0;4000,6]'
        await svakom_ai._save_command_notice('ankni-format', raw_ankni, 'LOOP:2000,5;3000,0;4000,6',
                                             '已广播', conv_id='test-scope', profile='ankni')
        async with test_db() as db:
            saved = await (await db.execute("SELECT attachments FROM messages WHERE id='ankni_notice_ankni-format'")).fetchone()
        assert json.loads(saved[0])[0]['format'] == 'duration_modes_v1'
        assert json.loads(saved[0])[0]['raw'] == raw_ankni
        # Rejected commands are still inspectable, without a device command event.
        sent.clear()
        assert await svakom_ai.process_commands('正文[SVAKOM:LOOP:bad]', 'invalid', conv_id='test-scope') == '正文'
        capabilities.set_capability_enabled('svakom', False)
        assert await svakom_ai.process_commands('[SVAKOM:STOP]', 'disabled', conv_id='test-scope') == ''
        assert [e['type'] for e in sent] == ['msg_created', 'msg_created']
        assert '格式无效' in sent[0]['data']['attachments'][0]['status']
        assert '开关已关闭' in sent[1]['data']['attachments'][0]['status']
    asyncio.run(run())


def test_real_private_and_group_prompt_assembly(isolated):
    from routes import chat
    import chatroom
    isolated.setattr(chat, 'with_current_device_context', lambda history, **kwargs: history)
    isolated.setattr(chatroom, 'with_current_device_context', lambda history, **kwargs: history)
    isolated.setattr(chatroom, 'load_worldbook', lambda: {})
    isolated.setattr(chatroom, '_read_connor_persona', lambda: '')
    isolated.setattr(chatroom, 'fetch_merged_timeline', AsyncMock(return_value=[]))
    isolated.setattr(chatroom, 'build_memory_blocks', AsyncMock(return_value={'time_block': '', 'memory_block': ''}))
    async def run():
        sent = []
        isolated.setattr(svakom_ai.manager, 'broadcast', AsyncMock(side_effect=sent.append))
        capabilities.set_capability_enabled('svakom', True)
        history = []
        await chat._insert_private_ability_block(history, 0, 0, user_name='测试用户', model_key='test', whisper_mode=False)
        assert '[SVAKOM:LOOP:' in str(history)
        # The response worker inherits the prompt builder's request permission.
        await asyncio.create_task(svakom_ai.process_commands('[SVAKOM:STOP]', 'private'))
        for build in (chatroom.build_aion_group_context, chatroom.build_connor_group_context):
            history, _ = await build('test', [], whisper_mode=False)
            assert '[SVAKOM:LOOP:' in str(history)
            await svakom_ai.process_commands('[SVAKOM:STOP]', build.__name__)
        assert len(sent) == 3
    asyncio.run(run())


def test_shared_master_switch_endpoints(isolated):
    from fastapi.testclient import TestClient
    from main import app
    isolated.setattr(svakom_ai.manager, 'broadcast', AsyncMock())
    client = TestClient(app)
    before = client.get('/api/svakom-ai').json()
    assert before['enabled'] is False
    assert client.put('/api/capabilities/svakom', json={'enabled': True}).status_code == 200
    enabled = client.get('/api/svakom-ai').json()
    assert enabled['enabled'] is True and enabled['epoch'] != before['epoch']
    revoked = client.post('/api/svakom-ai/takeover').json()
    assert revoked['enabled'] is True and revoked['epoch'] != enabled['epoch']
    client.put('/api/capabilities/svakom', json={'enabled': False})
    assert client.get('/api/svakom-ai').json()['enabled'] is False


def test_control_tags_never_reach_saved_chat_text_even_when_truncated(isolated):
    from routes.chat import _visible_ai_text
    from routes.chatroom import _visible_chatroom_text
    for tag in ('[SVAKOM:LOOP:3,1,0,0,2]', '[svakom:STOP]', '[SVAKOM:LOOP:3,1,0', '[SVAKOM'):
        raw = '正文。' + tag
        assert _visible_ai_text(raw) == '正文。'
        assert _visible_chatroom_text(raw) == '正文。'
        assert asyncio.run(svakom_ai.process_commands(raw, 'text-only')) == '正文。'


def test_control_tags_never_reach_tts_even_across_chunks(isolated):
    import tts
    tags = ('[SVAKOM:LOOP:3,1,0,0,2]', '[svakom:STOP]', '[SVAKOM:LOOP:3,1,0', '[SVAKOM')
    async def run():
        for tag in tags:
            text = '正文。' + tag
            assert tts.split_text_for_tts(text) == ['正文。']
            assert tts.split_text_for_tts(tag) == []
            spoken = []
            streamer = tts.TTSStreamer('svakom_text_only', 'unused')
            async def capture(segment): spoken.append(segment)
            # Capture the synthesis queue boundary; no audio provider or files involved.
            isolated.setattr(streamer, '_enqueue_segment', capture)
            for char in text:
                await streamer.feed_async(char)
            await streamer.flush()
            assert ''.join(spoken) == '正文。'
    asyncio.run(run())


def test_validation_and_revoked_reply_cannot_broadcast(isolated):
    assert svakom_ai.validate_command('LOOP:3,1,0,0,2;5,0,0,0,0') == 'LOOP:3,1,0,0,2;5,0,0,0,0'
    for text in ('1', 'LOOP:0,1,0,0,1', 'LOOP:3,1,1,0,1', 'LOOP:3,0,0,2,0'):
        with pytest.raises(ValueError):
            svakom_ai.validate_command(text)
    async def run():
        sent = []
        async def broadcast(event): sent.append(event)
        isolated.setattr(svakom_ai.manager, 'broadcast', broadcast)
        capabilities.set_capability_enabled('svakom', True)
        svakom_ai.capture_permission()
        cleaned = await svakom_ai.process_commands('你好[SVAKOM:LOOP:3,1,0,0,2]', 'm1')
        assert cleaned == '你好'
        assert sent[-1]['type'] == 'svakom_command'
        assert sent[-1]['data']['command'] == 'LOOP:3,1,0,0,2'
        await svakom_ai.process_commands('[SVAKOM:LOOP:3,1,0,0,2][SVAKOM:LOOP:broken', 'truncated-plan')
        assert len(sent) == 1, 'an incomplete trailing plan must not execute an earlier plan'
        await svakom_ai.process_commands('[SVAKOM:LOOP:bad][SVAKOM:STOP]', 'stop')
        assert sent[-1]['data']['command'] == 'STOP'
        await svakom_ai.process_commands('[SVAKOM:LOOP:3,1,0,0,2]', 'older-parallel-reply')
        assert len(sent) == 2, 'AI STOP must also revoke replies already being generated'
        svakom_ai.invalidate_permission()
        await svakom_ai.process_commands('[SVAKOM:LOOP:3,1,0,0,2]', 'late')
        assert len(sent) == 2
        capabilities.set_capability_enabled('svakom', False)
        svakom_ai.capture_permission()
        await svakom_ai.process_commands('[SVAKOM:STOP]', 'disabled')
        assert len(sent) == 2
    asyncio.run(run())


def test_background_replies_always_extract_toy_tags_and_honor_revocation(isolated):
    import schedule
    notices = AsyncMock()
    sent = []
    isolated.setattr(svakom_ai, '_save_command_notice', notices)
    isolated.setattr(svakom_ai.manager, 'broadcast', AsyncMock(side_effect=sent.append))
    async def run():
        for target, conv_id, scope in (({'type':'chatroom','room_id':'room-test'}, None, {'room_id':'room-test'}),
                                       ({'type':'private'}, 'conv-test', {'conv_id':'conv-test'})):
            capabilities.set_capability_enabled('svakom', True)
            svakom_ai.capture_permission()
            capabilities.set_capability_enabled('svakom', False)
            for raw in ('[SVAKOM:LOOP:4,1,2,2,0]', '[SVAKOM:LOOP:4,1,2'):
                result = await schedule._process_background_reply_commands(raw + '\n' + '查岗正文' if raw.endswith(']') else '查岗正文' + raw,
                    target=target, conv_id=conv_id, sender='connor', ai_msg_id='background-test')
                assert result == '查岗正文'
                assert notices.call_args.kwargs == {'conv_id': None, 'room_id': None, **scope}
                assert '开关已关闭' in notices.call_args.args[3]
            assert sent == []
        # A later re-enable cannot authorize the old wakeup response.
        capabilities.set_capability_enabled('svakom', True)
        await schedule._process_background_reply_commands('[SVAKOM:STOP]', target={'type':'private'},
            conv_id='conv-test', sender='aion', ai_msg_id='old-wakeup')
        assert sent == []
        svakom_ai.capture_permission()
        await schedule._process_background_reply_commands('[SVAKOM:LOOP:4,1,2,2,0]', target={'type':'private'},
            conv_id='conv-test', sender='aion', ai_msg_id='new-wakeup')
        assert len(sent) == 1 and sent[0]['type'] == 'svakom_command'
    asyncio.run(run())
