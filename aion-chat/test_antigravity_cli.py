import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from PIL import Image

import antigravity_cli as agy
import antigravity_image_hook


def make_image(path):
    Image.new('RGB', (64, 48), (200, 40, 60)).save(path)
    return path.read_bytes()


class AntigravityProfileTests(unittest.TestCase):
    def test_image_hook_reads_utf8_events_even_with_windows_gbk_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            image = str(Path(directory) / '图片.png')
            manifest = Path(directory) / 'allowed.json'
            manifest.write_text(json.dumps([image], ensure_ascii=False), encoding='utf-8')
            for path, expected in ((image, 'allow'), (str(Path(directory) / 'other.png'), 'deny')):
                event = {'toolCall': {'name': 'view_file', 'args': {'AbsolutePath': path}},
                         'lastUserInput': '请查看这张图片🌙'}
                result = subprocess.run(
                    [sys.executable, str(Path(antigravity_image_hook.__file__)), str(manifest)],
                    input=json.dumps(event, ensure_ascii=False).encode('utf-8'),
                    capture_output=True, timeout=10,
                    env={**os.environ, 'PYTHONIOENCODING': 'gbk', 'PYTHONUTF8': '0'},
                )
                with self.subTest(expected=expected):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)['decision'], expected)

    def test_staging_recovers_cmyk_jpeg_as_readable_rgb_png(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'upload.jpg'
            Image.new('CMYK', (64, 48), (0, 200, 180, 0)).save(source)
            source.write_bytes(source.read_bytes()[:-2])
            staged = agy._stage_image(source, Path(directory) / 'image.jpg')
            with Image.open(staged) as image:
                image.load()
                self.assertEqual(image.mode, 'RGB')

    def test_staging_repairs_truncated_jpeg_without_changing_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'upload.jpg'
            original = make_image(source)[:-2]  # Missing JPEG EOI marker.
            source.write_bytes(original)
            with Image.open(source) as damaged:
                with self.assertRaises(OSError):
                    damaged.load()
            staged = agy._stage_image(source, Path(directory) / 'image-1.jpg')
            with Image.open(staged) as repaired:
                repaired.load()
                self.assertEqual(repaired.format, 'PNG')
                self.assertEqual(repaired.size, (64, 48))
                self.assertGreater(repaired.getpixel((6, 4))[0], 190)
            self.assertEqual(staged.suffix, '.png')
            self.assertEqual(source.read_bytes(), original)

    def test_staging_preserves_valid_images_and_rejects_unrecoverable_data(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'upload.jpg'
            original = make_image(source)
            target = Path(directory) / 'image-1.jpg'
            self.assertEqual(agy._stage_image(source, target).read_bytes(), original)
            source.write_bytes(b'not an image')
            with self.assertRaisesRegex(agy.AntigravityError, '图片'):
                agy._stage_image(source, Path(directory) / 'image-2.jpg')

    def test_image_hook_only_grants_the_current_image_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            image = str(Path(directory) / 'image.png')
            call = {'toolCall': {'name': 'view_file', 'args': {'AbsolutePath': image}}}
            result = antigravity_image_hook.image_permission_decision(call, [image])
            self.assertEqual(result['decision'], 'allow')
            self.assertTrue(result['allowTool'])
            self.assertEqual(antigravity_image_hook.image_permission_decision(call, [])['decision'], 'deny')
            call['toolCall']['name'] = 'run_command'
            self.assertEqual(antigravity_image_hook.image_permission_decision(call, [image])['decision'], 'deny')

    def test_read_file_hook_only_grants_current_attachments_with_new_path_parameter(self):
        with tempfile.TemporaryDirectory() as directory:
            image = str(Path(directory) / 'image.png')
            for name, args, expected in [
                ('read_file', {'Path': image}, 'allow'),
                ('read_file', {'Path': str(Path(directory) / 'other.png')}, 'deny'),
                ('read_file', {'AbsolutePath': image}, 'deny'),
                ('read_file', {}, 'deny'),
                ('write_file', {'Path': image}, 'deny'),
            ]:
                with self.subTest(name=name, args=args):
                    call = {'toolCall': {'name': name, 'args': args}}
                    result = antigravity_image_hook.image_permission_decision(call, [image])
                    self.assertEqual(result['decision'], expected)
                    self.assertEqual(result['allowTool'], expected == 'allow')

    def test_chat_requires_explicit_denials_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / 'settings.json'
            for value in (None, {}, {'toolPermission': 'strict'}):
                if value is not None:
                    settings.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(agy.AntigravityError):
                    agy.require_chat_permissions(settings)
            settings.write_text(json.dumps({
                'toolPermission': 'strict',
                'permissions': {'deny': ['write_file(*)', 'command(*)', 'unsandboxed(*)',
                                        'mcp(*)', 'execute_url(*)']},
            }), encoding='utf-8')
            agy.require_chat_permissions(settings)
            value = json.loads(settings.read_text(encoding='utf-8'))
            value['toolPermission'] = 'request-review'
            settings.write_text(json.dumps(value), encoding='utf-8')
            agy.require_chat_permissions(settings)
            value.pop('toolPermission')
            settings.write_text(json.dumps(value), encoding='utf-8')
            agy.require_chat_permissions(settings)
            value['toolPermission'] = 'always-proceed'
            settings.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(agy.AntigravityError):
                agy.require_chat_permissions(settings)
            settings.write_text('{bad json', encoding='utf-8')
            with self.assertRaises(agy.AntigravityError):
                agy.require_chat_permissions(settings)

    def test_companion_profile_excludes_tools_and_inherited_coding_context(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = agy.prepare_chat_workspace(Path(directory))
            text = (workspace / '.agents/agents/home-companion.md').read_text(encoding='utf-8')
        for option in ('excludeDefaultComponents: true', 'inheritCustomizations: false',
                       'tools: []', 'inheritMcp: false', 'commandExecutionPolicy: off'):
            self.assertIn(option, text)
        self.assertLess(len(text), 1600)
        for identity in ('Ithil', 'Connor'):
            self.assertNotIn(identity, text)

    def test_command_pins_model_and_uses_official_stdin_stream_without_permission_bypass(self):
        command = agy.build_chat_command('agy.exe', 'gemini-3.1-pro-high', '90s')
        self.assertEqual(command[command.index('--model') + 1], 'gemini-3.1-pro-high')
        self.assertEqual(command[command.index('--agent') + 1], 'home-companion')
        self.assertEqual(command[command.index('--input-format') + 1], 'stream-json')
        self.assertEqual(command[command.index('--output-format') + 1], 'stream-json')
        self.assertNotIn('--dangerously-skip-permissions', command)
        self.assertNotIn('--continue', command)

    def test_catalog_uses_cli_model_slugs_and_skips_unavailable_models(self):
        catalog = agy.parse_model_catalog(json.dumps({'models': [
            {'slug': 'gemini-3.1-pro-high', 'name': 'Gemini 3.1 Pro (High)'},
            {'slug': 'claude-opus', 'name': 'Claude Opus', 'available': False},
        ]}))
        self.assertEqual(catalog, [{'model': 'gemini-3.1-pro-high', 'name': 'Gemini 3.1 Pro (High)'}])

    def test_official_text_catalog_keeps_distinct_effort_variants(self):
        self.assertEqual(agy.parse_model_catalog(
            'gemini-3.1-pro-high     Gemini 3.1 Pro (High)\n'
            'gemini-3.1-pro-low      Gemini 3.1 Pro (Low)\n'
        ), [
            {'model': 'gemini-3.1-pro-high', 'name': 'Gemini 3.1 Pro (High)'},
            {'model': 'gemini-3.1-pro-low', 'name': 'Gemini 3.1 Pro (Low)'},
        ])


class FakeProcess:
    def __init__(self, events):
        self.stdin = unittest.mock.Mock()
        self.stdin.drain = AsyncMock()
        self.stdin.wait_closed = AsyncMock()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(''.join(json.dumps(event) + '\n' for event in events).encode())
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode = 0
        self.pid = None
        self.wait = AsyncMock(return_value=0)


class AntigravityStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.permission_patch = patch.object(agy, 'require_chat_permissions', create=True)
        self.permission_patch.start()
        self.addCleanup(self.permission_patch.stop)

    async def test_response_progress_without_text_is_forwarded(self):
        process = FakeProcess([
            {'event': 'init', 'init': {'agent': agy.AGENT_NAME}},
            {'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': ''}},
            {'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'done'}},
        ])
        with tempfile.TemporaryDirectory() as directory, patch.object(
            agy.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)
        ):
            events = [event async for event in agy.stream_chat('fake', 'hello', '', Path(directory))]
        self.assertEqual([e['kind'] for e in events], ['activity', 'activity', 'text', 'completed'])

    async def test_chat_waits_for_agy_retry_in_both_stream_modes_only(self):
        import ai_providers
        from dataclasses import replace
        from routes import chat, chatroom
        import safe_live_stream
        from stream_safety import StreamActivity

        # Scale the 300s upstream silence + retry to milliseconds. A default
        # 30ms app idle deadline interrupts before the retry at 60ms.
        async def source():
            yield StreamActivity()
            await asyncio.sleep(.06)
            yield StreamActivity()
            await asyncio.sleep(.01)
            yield 'reply'

        async def consume(surface, mode, model_key):
            if surface == 'private':
                if mode == 'legacy':
                    result = await chat._consume_chat_stream(source(), asyncio.Queue(), model_key=model_key)
                    return result[0]
                result = await chat._consume_chat_realtime_stream(
                    source, asyncio.Queue(), model_key=model_key, transport_mode=mode)
                return result[0]
            if mode == 'legacy':
                return await chatroom._consume_chatroom_stream(
                    source(), asyncio.Queue(), chunk_type='aion_chunk', model_key=model_key)
            result = await chatroom._consume_chatroom_realtime_stream(
                source, asyncio.Queue(), chunk_type='aion_chunk', model_key=model_key, transport_mode=mode)
            return result.result

        policy = replace(chat.CHAT_STREAM_POLICY, idle_timeout=.03, total_timeout=.4)
        with patch.dict(ai_providers.MODELS, {
            'test-agy': {'provider': 'antigravity_cli', 'model': 'fake'},
            'test-relay': {'provider': 'custom_openai', 'model': 'fake'},
        }), patch.object(ai_providers, '_ANTIGRAVITY_CHAT_IDLE_TIMEOUT', .15, create=True), patch.object(
            chat, 'CHAT_STREAM_POLICY', policy
        ), patch.object(chatroom, 'CHAT_STREAM_POLICY', policy), patch.object(
            safe_live_stream, 'SAFE_LIVE_IDLE_TIMEOUT', .03
        ), patch.object(safe_live_stream, 'SAFE_LIVE_TOTAL_TIMEOUT', .4):
            for surface in ('private', 'group'):
                for mode in ('legacy', 'safe_live'):
                    with self.subTest(surface=surface, mode=mode):
                        result = await consume(surface, mode, 'test-agy')
                        self.assertIsNone(result.stop_reason)
                        self.assertEqual(result.committed_text, 'reply')
            # Other providers keep their existing idle limit.
            result = await consume('private', 'safe_live', 'test-relay')
            self.assertEqual(result.stop_reason, 'idle_timeout')
            # Longer AGY idle allowance never removes the overall budget.
            with patch.object(safe_live_stream, 'SAFE_LIVE_TOTAL_TIMEOUT', .02):
                result = await consume('private', 'safe_live', 'test-agy')
                self.assertIsNotNone(result.stop_reason)
                self.assertEqual(result.committed_text, '')

    async def test_cancel_during_silent_agy_wait_still_cleans_up(self):
        process = FakeProcess([])
        process.stdout = asyncio.StreamReader()
        process.stderr = asyncio.StreamReader()
        process.returncode = None
        started = asyncio.Event()
        async def spawn(*args, **kwargs):
            started.set()
            return process
        async def collect(directory):
            return [e async for e in agy.stream_chat('fake', 'hello', '', Path(directory))]
        with tempfile.TemporaryDirectory() as directory, patch.object(
            agy.asyncio, 'create_subprocess_exec', spawn
        ), patch('generation_control.terminate_process_tree', new_callable=AsyncMock) as cleanup:
            task = asyncio.create_task(collect(directory))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        cleanup.assert_awaited_once_with(process)

    async def test_success_finishes_without_waiting_for_pipe_eof_and_cleans_up(self):
        process = FakeProcess([])
        process.stdout = asyncio.StreamReader()
        for event in [
            {'event': 'init', 'init': {'agent': agy.AGENT_NAME}},
            {'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'done', 'usage': {'output_tokens': 4}}},
        ]:
            process.stdout.feed_data((json.dumps(event) + '\n').encode())
        process.stderr = asyncio.StreamReader()  # Neither pipe closes after result.
        process.returncode = None
        events = []
        async def collect(directory):
            async for event in agy.stream_chat('fake', 'hello', '', Path(directory)):
                events.append(event)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            agy.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)
        ), patch('generation_control.terminate_process_tree', new_callable=AsyncMock) as cleanup:
            try:
                await asyncio.wait_for(collect(directory), .3)
            except TimeoutError:
                self.fail('SUCCESS must finish without waiting for stdout/stderr EOF')
        self.assertEqual(events[-1]['kind'], 'completed')
        self.assertEqual(events[-1]['usage']['output_tokens'], 4)
        cleanup.assert_awaited_once_with(process)

    async def test_discovery_uses_supported_tab_separated_models_command(self):
        process = unittest.mock.Mock(returncode=0)
        process.communicate = AsyncMock(return_value=(
            b'Fetching available models...\ngemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n', b''))
        with patch.object(agy, 'find_binary', return_value='agy.exe'), patch.object(
            agy.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)
        ) as spawn:
            rows = await agy.discover_models(force=True)
        self.assertEqual(rows, [{'model': 'gemini-3.8-flash-low', 'name': 'Gemini 3.8 Flash (Low)'}])
        self.assertEqual(spawn.call_args.args, ('agy.exe', 'models'))

    async def test_existing_provider_forwards_model_and_usage_to_home(self):
        import ai_providers
        from stream_safety import StreamActivity

        async def stream(*args, **kwargs):
            self.assertEqual(args[2], 'gemini-3.1-pro-high')
            self.assertIn('[User]\n你好', args[1])
            yield {'kind': 'text', 'text': '自然回复'}
            yield {'kind': 'completed', 'usage': {'input_tokens': 20, 'output_tokens': 4, 'cache_read_tokens': 10}}

        meta = {}
        with patch.object(ai_providers, '_find_antigravity_binary', return_value='agy.exe'), patch.object(
            agy, 'stream_chat', stream
        ):
            chunks = [chunk async for chunk in ai_providers.call_antigravity_cli(
                [{'role': 'user', 'content': '你好'}], 'gemini-3.1-pro-high', meta
            )]
        self.assertEqual([chunk for chunk in chunks if chunk and not chunk.startswith(ai_providers.CLI_STATUS_PREFIX)], ['自然回复'])
        self.assertEqual(meta['prompt_tokens'], 20)
        self.assertEqual(meta['completion_tokens'], 4)
        self.assertTrue(any(isinstance(chunk, StreamActivity) for chunk in chunks))

    async def test_model_selection_refreshes_runtime_catalog_and_stays_visible(self):
        import config
        from routes import settings

        before = dict(config.ANTIGRAVITY_MODELS) if hasattr(config, 'ANTIGRAVITY_MODELS') else {}
        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(
                config, 'ANTIGRAVITY_MODELS_PATH', Path(directory) / 'models.json'
            ), patch.object(agy, 'discover_models', AsyncMock(return_value=[
                {'model': 'gemini-3.8-flash-high', 'name': 'Gemini 3.8 Flash (High)'},
                {'model': 'claude-opus-5-5-medium', 'name': 'Claude Opus 5.5 (Medium)'},
                {'model': 'gemini-3.1-pro-high', 'name': 'Gemini 3.1 Pro (High)'}
            ])):
                rows = await settings.list_models()
                self.assertEqual(len(config._read_antigravity_models()), 2)
            route = next(row for row in rows if row['key'] == 'AGY · Gemini 3.8 Flash (High)')
            self.assertEqual(route['provider'], 'antigravity_cli')
            self.assertEqual([row['key'] for row in rows if row['provider'] == 'antigravity_cli'],
                             ['AGY · Gemini 3.8 Flash (High)', 'AGY · Claude Opus 5.5 (Medium)'])
            key = route['key']
            self.assertEqual(config.resolve_model_key(key), key)
            config.refresh_custom_models()
            self.assertEqual(config.MODELS[key]['model'], 'gemini-3.8-flash-high')
            self.assertFalse(config.is_model_deprecated(key))
            self.assertNotIn('AGY · Gemini 3.1 Pro (High)', config.MODELS)
            self.assertTrue(config.MODELS[key]['vision'])
            self.assertTrue(all(value['vision'] for value in config.ANTIGRAVITY_MODELS.values()))
            self.assertTrue(config.MODELS[key]['audio'])
            self.assertFalse(config.MODELS['AGY · Claude Opus 5.5 (Medium)']['audio'])
            with tempfile.TemporaryDirectory() as directory, patch.object(
                config, 'ANTIGRAVITY_MODELS_PATH', Path(directory) / 'old-cache.json'
            ):
                config.ANTIGRAVITY_MODELS_PATH.write_text(json.dumps({
                    key: {**config.MODELS[key], 'audio': False},
                    'AGY · Claude Opus 5.5 (Medium)': {
                        'provider': 'antigravity_cli', 'model': 'claude-opus-5-5-medium', 'audio': True},
                }), encoding='utf-8')
                restored = config._read_antigravity_models()
                self.assertTrue(restored[key]['audio'])
                self.assertFalse(restored['AGY · Claude Opus 5.5 (Medium)']['audio'])
            self.assertTrue(config.is_model_deprecated('AGY-3.1pro'))
        finally:
            if hasattr(config, 'replace_antigravity_models'):
                config.replace_antigravity_models(before)

    async def test_stream_preserves_chinese_deltas_without_repeating_final_response(self):
        process = FakeProcess([
            {'event': 'init', 'init': {'agent': agy.AGENT_NAME, 'tools': ['run_command', 'manage_task']}},
            {'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': '你'}},
            {'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': '好🌙'}},
            {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '你好🌙', 'usage': {'input_tokens': 12}}},
        ])
        with tempfile.TemporaryDirectory() as directory, patch.object(
            agy.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)
        ) as spawn:
            events = [event async for event in agy.stream_chat('agy.exe', '你好', 'test-model', Path(directory))]
        deltas = [event['text'] for event in events if event['kind'] == 'text']
        self.assertEqual(deltas, ['你', '好🌙'])
        self.assertEqual(events[-1]['usage']['input_tokens'], 12)
        payload = json.loads(process.stdin.write.call_args.args[0])
        self.assertEqual(payload['message']['content'], '你好')
        self.assertEqual(spawn.call_args.kwargs['cwd'], str(Path(directory)))

    async def test_provider_keeps_image_association_and_forwards_local_files(self):
        import ai_providers

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'photo.png'
            image.write_bytes(b'image data')

            async def stream(*args, **kwargs):
                self.assertEqual(kwargs['image_paths'], (str(image.resolve()),))
                self.assertIn('第一张\n（图片附件：图片1）', args[1])
                self.assertNotIn('@' + str(image), args[1])
                yield {'kind': 'text', 'text': '看到了'}

            with patch.object(ai_providers, 'UPLOADS_DIR', Path(directory)), patch.object(
                ai_providers, '_find_antigravity_binary', return_value='agy.exe'
            ), patch.object(agy, 'stream_chat', stream):
                chunks = [chunk async for chunk in ai_providers.call_antigravity_cli([
                    {'role': 'user', 'content': '第一张', 'attachments': '["/uploads/photo.png"]'},
                    {'role': 'user', 'content': '还是这张', 'attachments': ['/uploads/photo.png']},
                ], 'gemini-3.8-flash-high')]
        self.assertIn('看到了', chunks)

    async def test_image_turn_stages_files_allows_view_file_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'workspace'
            source = Path(directory) / '原图.png'
            original = make_image(source)

            async def spawn(*command, **kwargs):
                staged = next((workspace / 'images').rglob('*.png'))
                self.assertEqual(staged.read_bytes(), original)
                self.assertEqual(command[command.index('--agent') + 1], 'home-companion-vision')
                profile = workspace / '.agents/agents/home-companion-vision.md'
                self.assertIn('tools: [view_file]', profile.read_text(encoding='utf-8'))
                self.assertEqual(json.loads((workspace / '.agents/image-input.json').read_text(encoding='utf-8')), [str(staged)])
                process = FakeProcess([
                    {'event': 'init', 'init': {'agent': 'home-companion-vision'}},
                    {'event': 'step_update', 'step_update': {'step_type': 'tool', 'tool_name': 'view_file',
                        'tool_info': {'name': 'view_file', 'parameters': {'AbsolutePath': str(staged)}, 'output': 'image'}}},
                    {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '红色圆形'}},
                ])
                processes.append((process, staged))
                return process

            processes = []
            with patch.object(agy.asyncio, 'create_subprocess_exec', spawn):
                events = [event async for event in agy.stream_chat(
                    'agy.exe', '这张是什么？', 'test-model', workspace, image_paths=(str(source),))]
            process, staged = processes[0]
            payload = json.loads(process.stdin.write.call_args.args[0])
            self.assertIn('图片1：' + str(staged), payload['message']['content'])
            self.assertEqual(events[-2]['text'], '红色圆形')
            self.assertFalse(staged.exists())
            self.assertFalse((workspace / '.agents/image-input.json').exists())
            self.assertTrue(source.exists())

    async def test_two_image_read_file_turn_keeps_exact_attachment_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'workspace'
            sources = [Path(directory) / f'photo-{index}.png' for index in (1, 2)]
            for source in sources:
                make_image(source)
            for outside in (False, True):
                async def spawn(*command, **kwargs):
                    paths = json.loads((workspace / '.agents/image-input.json').read_text(encoding='utf-8'))
                    self.assertEqual(len(paths), 2)
                    reads = [paths[0], str(sources[1]) if outside else paths[1]]
                    return FakeProcess([
                        {'event': 'init', 'init': {'agent': agy.VISION_AGENT_NAME}},
                        *[{'event': 'step_update', 'step_update': {
                            'step_type': 'tool', 'tool_name': 'read_file',
                            'tool_info': {'name': 'read_file', 'parameters': {'Path': path}},
                        }} for path in reads],
                        {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '两张都看到了'}},
                    ])

                with self.subTest(outside=outside), patch.object(agy.asyncio, 'create_subprocess_exec', spawn):
                    async def collect():
                        return [event async for event in agy.stream_chat(
                            'agy.exe', '看看两张图', 'test-model', workspace,
                            image_paths=tuple(map(str, sources)))]
                    if outside:
                        with self.assertRaisesRegex(agy.AntigravityError, '未授权'):
                            await collect()
                    else:
                        events = await collect()
                        self.assertEqual(events[-2]['text'], '两张都看到了')
                        self.assertEqual(events[-1]['kind'], 'completed')
                self.assertFalse((workspace / '.agents/image-input.json').exists())
                self.assertEqual(list((workspace / 'images').iterdir()), [])
            self.assertTrue(all(source.exists() for source in sources))

    async def test_voice_files_reach_only_gemini_with_message_association(self):
        import ai_providers

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'voice.wav'
            audio.write_bytes(b'RIFF-test-audio')
            for model, expected in [('gemini-3.8-flash-high', (str(audio.resolve()),)),
                                    ('claude-opus-5-5-medium', ())]:
                async def stream(*args, **kwargs):
                    self.assertEqual(kwargs.get('audio_paths', ()), expected)
                    self.assertNotIn('@' + str(audio).replace('\\', '/'), args[1])
                    self.assertIn('转写内容', args[1])
                    if expected:
                        self.assertIn('语音附件：音频1', args[1])
                        self.assertIn('再听一次\n（语音附件：音频1）', args[1])
                    yield {'kind': 'text', 'text': '收到'}

                with self.subTest(model=model), patch.object(ai_providers, 'UPLOADS_DIR', Path(directory)), patch.object(
                    ai_providers, '_find_antigravity_binary', return_value='agy.exe'
                ), patch.object(agy, 'stream_chat', stream):
                    chunks = [chunk async for chunk in ai_providers.call_antigravity_cli([
                        {'role': 'user', 'content': '转写内容', 'attachments': [
                            {'type': 'voice', 'url': '/uploads/voice.wav', 'transcript': '转写内容'}]},
                        {'role': 'user', 'content': '再听一次', 'attachments': '["/uploads/voice.wav"]'},
                    ], model)]
                    self.assertIn('收到', chunks)

    async def test_audio_and_image_share_exact_read_allowlist_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'workspace'
            source = Path(directory) / 'voice.wav'
            source.write_bytes(b'RIFF-test-audio')
            photo = Path(directory) / 'photo.png'
            make_image(photo)
            staged_paths = []

            async def spawn(*command, **kwargs):
                paths = json.loads((workspace / '.agents/image-input.json').read_text(encoding='utf-8'))
                self.assertEqual(len(paths), 2)
                staged_paths.extend(map(Path, paths))
                audio = next(path for path in staged_paths if path.suffix == '.wav')
                self.assertEqual(audio.read_bytes(), source.read_bytes())
                for path, expected in [(str(audio), 'allow'), (str(source), 'deny')]:
                    call = {'toolCall': {'name': 'view_file', 'args': {'AbsolutePath': path}}}
                    self.assertEqual(antigravity_image_hook.image_permission_decision(call, paths)['decision'], expected)
                return FakeProcess([
                    {'event': 'init', 'init': {'agent': agy.VISION_AGENT_NAME}},
                    {'event': 'step_update', 'step_update': {'step_type': 'tool', 'tool_name': 'view_file',
                        'tool_info': {'parameters': {'AbsolutePath': str(audio)}, 'output': 'audio'}}},
                    {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '听到了'}},
                ])

            with patch.object(agy.asyncio, 'create_subprocess_exec', spawn):
                events = [event async for event in agy.stream_chat(
                    'agy.exe', '听音看图', 'gemini-3.8-flash-high', workspace,
                    image_paths=(str(photo),), audio_paths=(str(source),))]
            self.assertEqual(events[-2]['text'], '听到了')
            self.assertTrue(all(not path.exists() for path in staged_paths))
            self.assertFalse((workspace / '.agents/image-input.json').exists())
            self.assertTrue(source.exists())

    async def test_browser_voice_survives_history_and_model_dispatch(self):
        import ai_providers
        from routes.chat import _process_voice_attachments_in_history
        from routes.chatroom import _process_voice_attachments

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'voice.webm'
            audio.write_bytes(b'webm-audio')
            for processor in (_process_voice_attachments_in_history, _process_voice_attachments):
                for model, enabled in [('gemini-3.8-flash-high', True), ('claude-opus-5-5-medium', False)]:
                    history = [{'role': 'user', 'content': '', 'attachments': [
                        {'type': 'voice', 'url': '/uploads/voice.webm', 'transcript': '浏览器录音'}]}]
                    processor(history)

                    async def stream(*args, **kwargs):
                        self.assertEqual(kwargs['audio_paths'], (str(audio.resolve()),) if enabled else ())
                        self.assertIn('浏览器录音', args[1])
                        yield {'kind': 'text', 'text': '收到'}

                    with self.subTest(processor=processor.__name__, model=model), patch.dict(ai_providers.MODELS, {
                        'audio-test': {'provider': 'antigravity_cli', 'model': model, 'audio': enabled}
                    }), patch.object(ai_providers, 'UPLOADS_DIR', Path(directory)), patch.object(
                        ai_providers, '_find_antigravity_binary', return_value='agy.exe'
                    ), patch.object(agy, 'stream_chat', stream):
                        chunks = [chunk async for chunk in ai_providers.stream_ai(
                            history, 'audio-test', include_device_context=False)]
                        self.assertIn('收到', chunks)

    async def test_vision_model_moments_keep_images_for_native_reading(self):
        import config
        from routes import moments

        messages = [{'role': 'user', 'content': '看图', 'attachments': ['/uploads/photo.png']}]
        with patch.dict(config.MODELS, {'vision-test': {'provider': 'antigravity_cli', 'vision': True}}), patch.object(
            moments, '_sentinel_describe_images', AsyncMock(return_value=[])
        ) as sentinel, patch.object(moments, '_messages_have_images', return_value=True):
            self.assertEqual(await moments._prepare_moment_messages_for_model(messages, 'vision-test'), messages)
        sentinel.assert_not_awaited()

    async def test_image_launch_failure_cleans_manifest_and_staged_files(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'workspace'
            image = Path(directory) / 'source.png'
            make_image(image)
            for error in (OSError('launch failed'), asyncio.CancelledError()):
                with self.subTest(error=type(error).__name__), patch.object(
                    agy.asyncio, 'create_subprocess_exec', AsyncMock(side_effect=error)
                ):
                    with self.assertRaises(type(error)):
                        _ = [event async for event in agy.stream_chat(
                            'agy.exe', 'look', 'test-model', workspace, image_paths=(str(image),))]
                self.assertFalse((workspace / '.agents/image-input.json').exists())
                self.assertEqual(list((workspace / 'images').iterdir()), [])

    async def test_response_only_and_structured_failure_are_not_silently_lost(self):
        for result, expected in [
            ({'status': 'SUCCESS', 'response': '完整回复'}, '完整回复'),
            ({'status': 'ERROR', 'error': 'quota exhausted', 'response': ''}, 'quota exhausted'),
        ]:
            process = FakeProcess([{'event': 'init', 'init': {'agent': agy.AGENT_NAME, 'tools': []}}, {'event': 'result', 'result': result}])
            with tempfile.TemporaryDirectory() as directory, patch.object(
                agy.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)
            ):
                if result['status'] == 'SUCCESS':
                    events = [event async for event in agy.stream_chat('agy.exe', 'hello', '', Path(directory))]
                    self.assertEqual(events[-2]['text'], expected)
                else:
                    with self.assertRaisesRegex(agy.AntigravityError, expected):
                        _ = [event async for event in agy.stream_chat('agy.exe', 'hello', '', Path(directory))]

    async def test_wrong_agent_and_actual_tool_steps_are_rejected(self):
        for events, error in [
            ([{'event': 'init', 'init': {'agent': 'default'}}], '陪伴配置'),
            ([{'event': 'init', 'init': {'agent': agy.AGENT_NAME}},
              {'event': 'step_update', 'step_update': {'step_type': 'tool', 'tool_name': 'manage_task'}}], '工具'),
        ]:
            process = FakeProcess(events)
            with tempfile.TemporaryDirectory() as directory, patch.object(
                agy.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)
            ):
                with self.assertRaisesRegex(agy.AntigravityError, error):
                    _ = [event async for event in agy.stream_chat('agy.exe', 'hello', '', Path(directory))]


if __name__ == '__main__':
    unittest.main()
