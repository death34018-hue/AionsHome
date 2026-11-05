"""Offline checks: no real keys, sentinel calls, or paid audio requests."""
import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import elevenlabs_tts as provider
import expressive_voice as voice
import tts
from ws import ConnectionManager


class VoiceContextTests(unittest.TestCase):
    def test_voice_context_preserves_original_command_and_rebuilds_legacy_clips(self):
        command = '[语音｜方向= 带笑逗弄 ｜内容=Come on, baby.]'
        attachments = [
            {'type': 'image', 'text': '忽略图片'},
            {'type': 'expressive_voice', 'source_command': command,
             'direction': '带笑逗弄', 'text': 'Come on, baby.',
             'synthesis_text': '[chuckles] Come on, baby.'},
            {'type': 'expressive_voice', 'direction': '温柔', 'text': '先歇一会儿。'},
            {'type': 'expressive_voice', 'text': '我在。'},
        ]
        expected = '\n'.join([command, '[语音|方向=温柔|内容=先歇一会儿。]',
                              '[语音|方向=自然|内容=我在。]'])
        self.assertEqual(voice.transcript_for_context(attachments), expected)
        self.assertEqual(voice.transcript_for_context(json.dumps(attachments)), expected)
        self.assertEqual(voice.transcript_for_context([]), '')

    def test_voice_commands_reach_private_and_group_history(self):
        from context_builder import render_merged_timeline
        command = '[语音|方向=带笑逗弄|内容=Come on, baby.]'
        for source, sender, clip in [
            ('private', 'assistant', {'source_command': command}),
            ('group', 'connor', {'direction': '带笑逗弄', 'text': 'Come on, baby.'}),
        ]:
            with self.subTest(source=source):
                history = render_merged_timeline([{
                    'source': source, 'sender': sender, 'content': '轮到你了。',
                    'created_at': 1786369912.0,
                    'attachments': [{'type': 'expressive_voice', **clip}],
                }], 'connor')
                content = history[-1]['content']
                self.assertIn('轮到你了。\n' + command, content)
                self.assertNotIn('（语音内容）', content)


class ElevenLabsTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_sends_model_tags_and_never_retries_timeout(self):
        conn = AsyncMock()
        conn.post.return_value = httpx.Response(200, content=b'mp3', headers={'content-type':'audio/mpeg'})
        with patch.object(provider, 'client') as factory, patch.object(provider, 'get_key', return_value='fixture'), patch.dict(provider.SETTINGS, {'elevenlabs_tts_model':'eleven_v4'}):
            factory.return_value.__aenter__.return_value = conn
            self.assertEqual(await tts._request_tts_audio('[softly] 你好。', 'elevenlabs:fixture'), b'mp3')
            self.assertEqual(conn.post.call_args.kwargs['json'], {'text':'[softly] 你好。', 'model_id':'eleven_v4'})
            conn.post.reset_mock()
            conn.post.side_effect = httpx.ReadTimeout('fixture')
            with self.assertRaises(RuntimeError):
                await provider.request_audio('你好', 'fixture')
            self.assertEqual(conn.post.await_count, 1)

    async def test_voice_catalog_paginates_and_deduplicates(self):
        conn = AsyncMock()
        conn.get.side_effect = [httpx.Response(200, json={'voices':[{'voice_id':'a','name':'A'}], 'has_more':True, 'next_page_token':'p2'}),
                               httpx.Response(200, json={'voices':[{'voice_id':'a'},{'voice_id':'b'}], 'has_more':False})]
        with patch.object(provider, 'client') as factory, patch.object(provider, 'get_key', return_value='fixture'):
            factory.return_value.__aenter__.return_value = conn
            result = await provider.list_voices()
        self.assertEqual([v['uri'] for v in result], ['elevenlabs:a','elevenlabs:b'])
        self.assertEqual(conn.get.await_count, 2)

    async def test_settings_save_independent_provider_and_reject_bad_model(self):
        from routes import settings
        configured = {'siliconflow_key':'keep-existing'}
        with patch.object(settings, 'SETTINGS', configured), patch.object(settings, 'save_settings'):
            await settings.update_settings(settings.SettingsUpdate(elevenlabs_tts_key='fixture', elevenlabs_tts_model='eleven_v4', elevenlabs_aion_voice_id='voice_A'))
            self.assertEqual(configured['siliconflow_key'], 'keep-existing')
            self.assertEqual(configured['elevenlabs_aion_voice_id'], 'voice_A')
            with self.assertRaises(settings.HTTPException):
                await settings.update_settings(settings.SettingsUpdate(elevenlabs_tts_model='unsupported'))

    async def test_normal_picker_only_lists_selected_voices_but_settings_keeps_catalog(self):
        from routes import settings
        configured = {'elevenlabs_tts_key': 'fixture', 'elevenlabs_aion_voice_id': 'b', 'elevenlabs_connor_voice_id': 'a'}
        catalog = [{'uri': 'elevenlabs:' + key, 'customName': key.upper(), 'provider': 'elevenlabs'} for key in ('a', 'b', 'unused')]
        with patch.object(settings, 'SETTINGS', configured), patch.object(settings, 'get_key', return_value=''), patch.object(provider, 'list_voices', new_callable=AsyncMock, return_value=catalog) as fetch:
            result = await settings.tts_voice_list()
            self.assertEqual([v['uri'] for v in result['voices']], ['elevenlabs:b', 'elevenlabs:a'])
            self.assertEqual(result['voices'][0]['customName'], 'B')
            self.assertEqual((await settings.elevenlabs_voice_catalog())['voices'], catalog)
            configured['elevenlabs_connor_voice_id'] = 'b'
            self.assertEqual(len((await settings.tts_voice_list())['voices']), 1)
            fetch.side_effect = RuntimeError('offline')
            self.assertEqual((await settings.tts_voice_list())['voices'][0]['uri'], 'elevenlabs:b')

    async def test_ordinary_tts_never_reads_complete_or_partial_directive(self):
        raw = '正常正文。[语音|方向=温柔|内容=只在专用语音里说。]'
        self.assertEqual(tts._strip_tags(raw), '正常正文。')
        self.assertEqual(tts._strip_tags(raw[:-1]), '正常正文。')
        with patch.object(tts, '_request_edge_tts_audio', new_callable=AsyncMock) as synth:
            await tts._request_tts_audio(raw, 'edge:fixture')
            synth.assert_awaited_once_with('正常正文。', 'fixture')
        with patch.object(provider, 'request_audio', new_callable=AsyncMock) as synth:
            block = '[语音]【barely a whisper】I missed you.【/语音】'
            self.assertIsNone(await tts._request_tts_audio(block, 'elevenlabs:fixture'))
            await tts._request_tts_audio('前。' + block + '后。', 'elevenlabs:fixture')
            synth.assert_awaited_once_with('前。后。', 'fixture')

    def test_direct_script_preserves_delivery_and_counts_the_whole_script(self):
        cue = '[quiet at first,\na smile creeping into the last words]'
        script = (cue + '\n').ljust(500, '好')
        command = '【语音】' + script.replace('[', '【').replace(']', '】') + '[/语音]'
        clean, clips = voice.extract('前' + command + '后')
        self.assertEqual(clean, '前后')
        self.assertNotIn('error', clips[0])
        self.assertEqual(clips[0]['synthesis_text'], script)
        self.assertEqual(clips[0]['text'], script.split(']\n')[1])
        self.assertEqual(clips[0]['source_command'], command)
        from routes.chatroom import _normalize_cli_bubble_breaks
        with patch('routes.chatroom.resolve_model_key', return_value='fixture'), patch.dict('routes.chatroom.MODELS', {'fixture': {'provider': 'gemini_cli'}}):
            self.assertEqual(_normalize_cli_bubble_breaks(command, 'fixture'), command)
        for bad in (script + '呀', '[quiet', '[softly]'):
            clean, clips = voice.extract('前[语音]' + bad + '【/语音】后')
            self.assertEqual(clean, '前后')
            self.assertTrue(clips[0]['error'])

    async def test_voice_block_stays_out_of_live_chat_and_normal_tts(self):
        from safe_live_stream import KnownCommandStreamFilter
        from web_search import WebCommandStreamFilter
        block = '【语音】[quiet, then bright]\nOnly in the recording. 【whispers】Good night.[/语音]'
        with tempfile.TemporaryDirectory() as tmp:
            for ending, expected in [(block + '后。', '前。后。'), (block[:-5], '前。')]:
                text = '前。' + ending
                display = KnownCommandStreamFilter()
                fallback = WebCommandStreamFilter()
                streamer = tts.TTSStreamer('block-test', 'edge:test', min_chars=1, max_chars=8, cache_dir=Path(tmp))
                with patch.object(streamer, '_enqueue_segment', new_callable=AsyncMock) as queue:
                    visible = ''
                    fallback_visible = ''
                    for char in text:
                        visible += display.feed(char)
                        fallback_visible += fallback.feed(char)
                        await streamer.feed_async(char)
                    await streamer.flush()
                self.assertEqual(visible + display.finish()[0], expected)
                self.assertEqual(fallback_visible + fallback.flush(), expected)
                self.assertEqual(''.join(c.args[0] for c in queue.await_args_list), expected)
                self.assertEqual(''.join(tts.split_text_for_tts(text, min_chars=1, max_chars=3)), expected)

    async def test_expressive_audio_can_route_when_full_tts_is_off(self):
        manager = ConnectionManager()
        client = AsyncMock()
        await manager.connect(client)
        manager.register_client_id(client, 'fixture')
        manager.set_tts_state(client, False, '', can_play=True)
        self.assertFalse(manager.any_tts_enabled())
        await manager.send_tts_event({'type':'tts_chunk', 'data':{'msg_id':'m_expressive','expressive':True}})
        client.send_text.assert_awaited_once()


class MessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'messages.db'
        @asynccontextmanager
        async def connect():
            async with aiosqlite.connect(self.path) as db:
                yield db
        self.connect = connect
        async with connect() as db:
            await db.execute('CREATE TABLE messages (id TEXT PRIMARY KEY, content TEXT, attachments TEXT)')
            await db.commit()
        self.db_patch = patch.object(voice, 'get_db', connect)
        self.db_patch.start()
        self.cache_patch = patch.object(voice, 'TTS_CACHE_DIR', Path(self.tmp.name))
        self.cache_patch.start()
        self.keepsakes = Path(self.tmp.name) / 'voice_keepsakes'
        self.keepsakes.mkdir()
        self.keepsakes_patch = patch.object(voice, 'EXPRESSIVE_VOICE_DIR', self.keepsakes, create=True)
        self.keepsakes_patch.start()

    async def asyncTearDown(self):
        self.keepsakes_patch.stop()
        self.cache_patch.stop(); self.db_patch.stop(); self.tmp.cleanup()

    async def message(self):
        data = {'id':'fixture', 'role':'assistant','content':'正文。【语音】【softly, with a smile】先歇一会儿。[/语音]', 'attachments':[]}
        async with self.connect() as db:
            await db.execute('INSERT INTO messages VALUES (?,?,?)', (data['id'], data['content'], '[]'))
            await db.commit()
        return {'type':'msg_created', 'data':data}

    async def test_disabled_still_removes_directive_but_does_not_schedule(self):
        event = await self.message()
        with patch.object(voice, 'enabled', return_value=False), patch.object(voice, 'spawn_generation_task') as spawn:
            await voice.prepare_message(event)
            voice.start_message(event, AsyncMock())
        self.assertEqual(event['data']['content'], '正文。')
        self.assertEqual(event['data']['attachments'][0]['status'], 'unavailable')
        spawn.assert_not_called()

    async def test_passive_has_replay_attachment_no_autoplay_and_no_duplicate_charge(self):
        event = await self.message(); manager = AsyncMock()
        with patch.object(voice, 'enabled', return_value=True), patch.object(voice, 'get_key', return_value='fixture'), patch.object(provider, 'voice_for', return_value='fixture'), patch.object(provider, 'request_audio', new_callable=AsyncMock, return_value=b'mp3') as synth:
            await voice.prepare_message(event)
            await voice._produce(('messages','aion'), event['data'], manager, False)
            await voice._produce(('messages','aion'), event['data'], manager, False)
        synth.assert_awaited_once_with('[softly, with a smile]先歇一会儿。', 'fixture')
        self.assertEqual(event['data']['attachments'][0]['synthesis_text'], synth.await_args.args[0])
        manager.send_tts_event.assert_not_awaited()
        self.assertEqual(event['data']['attachments'][0]['status'], 'ready')
        self.assertEqual((self.keepsakes / 'fixture_expressive.mp3').read_bytes(), b'mp3')
        self.assertFalse((Path(self.tmp.name) / 'fixture_expressive.mp3').exists())

    async def test_group_voice_uses_the_speaker_and_never_bills_an_overlong_script(self):
        async with self.connect() as db:
            await db.execute('CREATE TABLE chatroom_messages (id TEXT PRIMARY KEY, content TEXT, attachments TEXT)')
            await db.commit()
        for sender, script in [('connor', 'Come here. 【breathy】I missed you. [soft moan] Mm…'), ('aion', 'a' * 501)]:
            event = {'type': 'chatroom_msg_created', 'data': {'sender': sender, 'id': sender,
                     'content': f'【语音】{script}【/语音】'}}
            async with self.connect() as db:
                await db.execute('INSERT INTO chatroom_messages VALUES (?,?,?)', (sender, event['data']['content'], '[]'))
                await db.commit()
            with patch.object(voice, 'enabled', return_value=True), patch.object(voice, 'get_key', return_value='key'), patch.object(provider, 'voice_for', side_effect=lambda actor: actor + '-voice'), patch.object(provider, 'request_audio', new_callable=AsyncMock, return_value=b'mp3') as synth:
                await voice.prepare_message(event)
                await voice._produce(('chatroom_messages', sender), event['data'], AsyncMock(), False)
            self.assertEqual(event['data']['content'], '')
            if sender == 'connor':
                synth.assert_awaited_once_with('Come here. [breathy]I missed you. [soft moan] Mm…', 'connor-voice')
                self.assertEqual(event['data']['attachments'][0]['text'].split(), 'Come here. I missed you. Mm…'.split())
            else:
                synth.assert_not_awaited()
                self.assertEqual(event['data']['attachments'][0]['status'], 'failed')

    async def test_scheduled_wakeup_opts_into_autoplay_without_chat_generation(self):
        event = await self.message(); manager = AsyncMock()
        with patch.object(voice, 'enabled', return_value=True), patch.object(voice, 'get_key', return_value='fixture'), patch.object(provider, 'voice_for', return_value='fixture'), patch.object(provider, 'request_audio', new_callable=AsyncMock, return_value=b'mp3'):
            await voice.prepare_message(event)
            voice.start_message(event, manager, autoplay=True)
            await voice._tasks[event['data']['id']]
        self.assertEqual([c.args[0]['type'] for c in manager.send_tts_event.call_args_list], ['tts_chunk', 'tts_done'])
        self.assertEqual(manager.send_tts_event.call_args_list[0].args[0]['data']['text'], '先歇一会儿。')


    async def test_autoplay_waits_for_ordinary_tts_and_preserves_queue_protocol(self):
        event = await self.message(); manager = AsyncMock()
        streamer = tts.TTSStreamer('fixture', 'edge:fixture', cache_dir=Path(self.tmp.name))
        with patch.object(voice, 'enabled', return_value=True), patch.object(voice, 'get_key', return_value='fixture'), patch.object(provider, 'voice_for', return_value='fixture'), patch.object(provider, 'request_audio', new_callable=AsyncMock, return_value=b'mp3'):
            await voice.prepare_message(event)
            task = asyncio.create_task(voice._produce(('messages','aion'), event['data'], manager, True))
            for _ in range(100):
                if manager.broadcast.await_count:
                    break
                await asyncio.sleep(.005)
            manager.send_tts_event.assert_not_awaited()
            streamer.completed.set()
            await task
        self.assertEqual([c.args[0]['type'] for c in manager.send_tts_event.call_args_list], ['tts_chunk','tts_done'])
        self.assertEqual(manager.send_tts_event.call_args_list[0].args[0]['data']['parent_msg_id'], 'fixture')

    async def test_cancel_after_audio_is_saved_preserves_replay(self):
        event = await self.message(); manager = AsyncMock()
        streamer = tts.TTSStreamer('fixture', 'edge:fixture', cache_dir=Path(self.tmp.name))
        with patch.object(voice, 'enabled', return_value=True), patch.object(voice, 'get_key', return_value='fixture'), patch.object(provider, 'voice_for', return_value='fixture'), patch.object(provider, 'request_audio', new_callable=AsyncMock, return_value=b'mp3'):
            await voice.prepare_message(event)
            task = asyncio.create_task(voice._produce(('messages','aion'), event['data'], manager, True))
            for _ in range(100):
                if manager.broadcast.await_count:
                    break
                await asyncio.sleep(.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue((self.keepsakes / 'fixture_expressive.mp3').exists())
        self.assertEqual(event['data']['attachments'][0]['status'], 'ready')
        manager.send_tts_event.assert_not_awaited()

    async def test_legacy_audio_is_archived_without_overwriting_keepsakes(self):
        legacy = Path(self.tmp.name) / 'old_expressive.mp3'
        legacy.write_bytes(b'old recording')
        (Path(self.tmp.name) / 'ordinary.mp3').write_bytes(b'ordinary')
        voice.archive_legacy_audio()
        self.assertEqual((self.keepsakes / legacy.name).read_bytes(), b'old recording')
        self.assertFalse((self.keepsakes / 'ordinary.mp3').exists())
        legacy.write_bytes(b'different')
        voice.archive_legacy_audio()
        self.assertEqual((self.keepsakes / legacy.name).read_bytes(), b'old recording')

    async def test_keepsake_replays_at_original_url_after_cache_cleanup(self):
        from routes import settings
        saved = self.keepsakes / 'saved_expressive.mp3'
        saved.write_bytes(b'saved recording')
        legacy = Path(self.tmp.name) / 'legacy_expressive.mp3'
        legacy.write_bytes(b'legacy recording')
        ordinary = Path(self.tmp.name) / 'ordinary.mp3'
        ordinary.write_bytes(b'ordinary')
        with patch.object(settings, 'TTS_CACHE_DIR', Path(self.tmp.name)), patch.object(settings, 'EXPRESSIVE_VOICE_DIR', self.keepsakes, create=True):
            self.assertEqual((await settings.tts_audio('legacy_expressive')).path, legacy)
            tts.cleanup_tts_cache_dir(Path(self.tmp.name), max_bytes=0)
            self.assertFalse(ordinary.exists())
            self.assertTrue(legacy.exists())
            self.assertEqual((await settings.tts_audio('saved_expressive')).path, saved)
            self.assertEqual(saved.read_bytes(), b'saved recording')


if __name__ == '__main__':
    unittest.main()
