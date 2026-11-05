import asyncio
import base64
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image
import image_gen
from codex_app_server import CodexAppServerEvent, stream_codex_app_server
from test_codex_app_server import _FakeProcess, _happy_messages
from test_album import sample_image


class CodexImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_image_yields_to_waiting_image_before_retry(self):
        import codex_image_gen as provider
        first_started = asyncio.Event()
        calls = []
        active = set()
        async def stream(*args, prompt, **kwargs):
            actor = prompt.splitlines()[-1]
            self.assertFalse(active, 'Two Codex image turns shared the slot')
            active.add(actor)
            calls.append(actor)
            try:
                if len(calls) == 1:
                    first_started.set()
                    await asyncio.Event().wait()
                yield CodexAppServerEvent('image', image={
                    'status': 'completed', 'result': base64.b64encode(sample_image()).decode()})
            finally:
                active.remove(actor)
        with patch.object(provider, '_build_codex_chat_environment', return_value={'CODEX_HOME': '.'}), \
                patch.object(provider, 'stream_codex_app_server', stream), \
                patch.object(provider, 'CODEX_IMAGE_QUEUE_TURN', 0.03, create=True), \
                patch.object(provider, 'CODEX_IMAGE_FIRST_ATTEMPT_TIMEOUT', 2), \
                patch.object(provider, 'save_generated_image', new=AsyncMock(return_value='photo.png')):
            first = asyncio.create_task(provider.generate_codex_image('first'))
            await first_started.wait()
            try:
                async with asyncio.timeout(1):
                    await asyncio.gather(first, provider.generate_codex_image('second'))
            finally:
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
        self.assertEqual(calls, ['first', 'second', 'first'])

    async def test_cli_receives_small_copies_in_order_and_keeps_original_anchors(self):
        import codex_image_gen as provider
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rng = random.Random(10)
            paths = [root/'portrait.png', root/'transparent.png', root/'tiny.png']
            for mode, path in zip(('RGB', 'RGBA'), paths):
                Image.frombytes(mode, (700, 500), rng.randbytes(700 * 500 * len(mode))).save(path)
            paths[-1].write_bytes(sample_image())
            originals = [p.read_bytes() for p in paths]
            sent = []
            async def stream(command, **kwargs):
                sent.extend(map(Path, kwargs['image_paths']))
                self.assertEqual(sent[2], paths[2])
                for path in sent:
                    self.assertLess(path.stat().st_size, 500_000)
                with Image.open(sent[1]) as transparent:
                    self.assertIn('A', transparent.getbands())
                yield CodexAppServerEvent('image', image={
                    'status': 'completed', 'result': base64.b64encode(sample_image()).decode()})
            with patch.object(provider, '_build_codex_chat_environment', return_value={'CODEX_HOME': directory}), \
                    patch.object(provider, 'stream_codex_app_server', stream), \
                    patch.object(provider, 'save_generated_image', new=AsyncMock()):
                await provider.generate_codex_image('group', references=[
                    {'name': path.stem, 'path': str(path)} for path in paths])
            self.assertEqual(originals, [p.read_bytes() for p in paths])
            self.assertFalse(any(p.exists() for p in sent[:2]))

    async def test_cancelled_waiter_does_not_start_or_shorten_the_remaining_turn(self):
        import codex_image_gen as provider
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def stream(*args, prompt, **kwargs):
            calls.append(prompt.splitlines()[-1])
            started.set()
            await finish.wait()
            yield CodexAppServerEvent('image', image={
                'status': 'completed', 'result': base64.b64encode(sample_image()).decode()})
        with patch.object(provider, '_build_codex_chat_environment', return_value={'CODEX_HOME': '.'}), \
                patch.object(provider, 'stream_codex_app_server', stream), \
                patch.object(provider, 'CODEX_IMAGE_QUEUE_TURN', 0.04), \
                patch.object(provider, 'save_generated_image', new=AsyncMock()):
            first = asyncio.create_task(provider.generate_codex_image('first'))
            await started.wait()
            waiting = asyncio.create_task(provider.generate_codex_image('cancelled'))
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            await asyncio.sleep(0.08)
            finish.set()
            await first
            await provider.generate_codex_image('next')
        self.assertEqual(calls, ['first', 'next'])

    async def test_chat_timeout_is_not_swallowed(self):
        with patch('image_anchors.resolve_chat_references', AsyncMock(return_value=('portrait', []))), \
                patch('codex_image_gen.generate_codex_image', AsyncMock(side_effect=TimeoutError())), \
                patch.dict(image_gen.SETTINGS, {'image_gen_provider': 'codex'}, clear=True):
            with self.assertRaises(TimeoutError):
                await image_gen.generate_chat_image('portrait')

    async def test_missing_image_keeps_provider_explanation_for_theater(self):
        import codex_image_gen as provider
        attempts = []
        async def stream(*args, **kwargs):
            attempts.append(True)
            yield CodexAppServerEvent('text_delta', text='Image generation refused: safety policy')
            yield CodexAppServerEvent('completed')
        with patch.object(provider, '_build_codex_chat_environment', return_value={'CODEX_HOME': '.'}), \
                patch.object(provider, 'image_command', return_value=['codex']), \
                patch.object(provider, 'stream_codex_app_server', stream), \
                patch.dict(image_gen.SETTINGS, {'image_gen_provider': 'codex'}):
            with self.assertRaisesRegex(RuntimeError, 'safety policy'):
                await image_gen.generate_image('draw', raise_errors=True)
        self.assertEqual(len(attempts), 1)

    async def test_stalled_attempt_closes_then_retries_and_keeps_completed_image(self):
        import codex_image_gen as provider
        steps = []
        async def stream(*args, **kwargs):
            first = not steps
            steps.append('open')
            try:
                yield CodexAppServerEvent('image_started')
                if not first:
                    yield CodexAppServerEvent('image', image={
                        'status': 'completed', 'result': base64.b64encode(sample_image()).decode()})
                await asyncio.Event().wait()
            finally:
                steps.append('close')
        with patch.object(provider, '_CODEX_SCRIPT', 'codex'), \
                patch.object(provider, '_build_codex_chat_environment', return_value={'CODEX_HOME': '.'}), \
                patch.object(provider, 'stream_codex_app_server', stream), \
                patch.object(provider, 'CODEX_IMAGE_FIRST_ATTEMPT_TIMEOUT', 0.02, create=True), \
                patch.object(provider, 'CODEX_IMAGE_TIMEOUT', 0.5), \
                patch.object(provider, 'save_generated_image', new=AsyncMock(return_value='album/photo.png')) as save:
            result = await provider.generate_codex_image('draw')
        self.assertEqual(result, 'album/photo.png')
        self.assertEqual(steps, ['open', 'close', 'open', 'close'])
        save.assert_awaited_once()
        self.assertEqual(save.call_args.args[0], sample_image())

    async def test_timeout_closes_stream_without_saving_an_unfinished_image(self):
        import codex_image_gen as provider
        closed = []
        async def stream(*args, **kwargs):
            try:
                await asyncio.sleep(10)
                yield CodexAppServerEvent('completed')
            finally:
                closed.append(True)
        with patch.object(provider, '_build_codex_chat_environment', return_value={'CODEX_HOME': '.'}), \
                patch.object(provider, 'image_command', return_value=['codex']), \
                patch.object(provider, 'stream_codex_app_server', stream), \
                patch.object(provider, 'CODEX_IMAGE_FIRST_ATTEMPT_TIMEOUT', 0.02), \
                patch.object(provider, 'CODEX_IMAGE_TIMEOUT', 0.15), \
                patch.object(provider, 'save_generated_image', new=AsyncMock()) as save:
            with self.assertRaises(TimeoutError):
                await provider.generate_codex_image('draw')
        self.assertEqual(closed, [True, True])
        save.assert_not_awaited()

    async def test_selfie_keeps_secondary_anchor_and_missing_photo_fails(self):
        import codex_image_gen as provider
        seen = []
        async def stream(command, **kwargs):
            seen.extend(kwargs['image_paths'])
            yield CodexAppServerEvent('image', image={
                'status': 'completed', 'result': base64.b64encode(sample_image()).decode()})
        with tempfile.TemporaryDirectory() as tmp:
            anchor = Path(tmp) / 'secondary.png'
            anchor.write_bytes(sample_image())
            with patch.object(image_gen, 'SECONDARY_REFERENCE_IMAGE_PATH', anchor), \
                    patch.object(provider, '_build_codex_chat_environment', return_value={'CODEX_HOME': tmp}), \
                    patch.object(provider, 'image_command', return_value=['codex']), \
                    patch.object(provider, 'stream_codex_app_server', stream), \
                    patch.object(provider, 'save_generated_image', new=AsyncMock(return_value='album/selfie.png')) as save:
                await provider.generate_codex_image('自拍', True, 'connor')
                self.assertEqual(seen, [str(anchor.resolve())])
                self.assertEqual(save.call_args.kwargs['reference_bytes'], sample_image())
                anchor.unlink()
                with self.assertRaises(FileNotFoundError):
                    await provider.generate_codex_image('自拍', True, 'connor')

    async def test_settings_persists_codex_route_without_an_api_key(self):
        from routes import settings
        with patch.dict(settings.SETTINGS, {}, clear=True), \
                patch.object(settings, 'save_settings'), \
                patch.object(settings.manager, 'broadcast', new=AsyncMock()):
            await settings.update_settings(settings.SettingsUpdate(image_gen_provider='codex'))
            self.assertEqual(settings.SETTINGS['image_gen_provider'], 'codex')

    async def test_switching_sources_preserves_existing_api_configuration(self):
        from routes import settings
        original = {'image_gen_base_url': 'https://example.invalid/v1',
                    'image_gen_api_key': 'test-key', 'image_gen_model': 'test-model'}
        with patch.dict(settings.SETTINGS, original, clear=True), \
                patch.object(settings, 'save_settings'), \
                patch.object(settings.manager, 'broadcast', new=AsyncMock()):
            for provider in ('codex', 'existing', 'codex'):
                await settings.update_settings(settings.SettingsUpdate(image_gen_provider=provider))
                self.assertEqual(settings.SETTINGS['image_gen_provider'], provider)
                self.assertEqual({key: settings.SETTINGS[key] for key in original}, original)

    async def test_protocol_returns_image_without_turning_it_into_chat_text(self):
        messages = _happy_messages()
        messages.insert(-1, {'method': 'error', 'params': {
            'willRetry': True, 'error': {'message': 'reconnecting'},
        }})
        messages.insert(-1, {'method': 'item/started', 'params': {'item': {
            'type': 'imageGeneration', 'id': 'img', 'status': 'in_progress',
        }}})
        messages.insert(-1, {'method': 'item/completed', 'params': {'item': {
            'type': 'imageGeneration', 'id': 'img', 'status': 'completed',
            'result': base64.b64encode(sample_image()).decode(), 'savedPath': None,
        }}})
        process = _FakeProcess(messages)
        events = [e async for e in stream_codex_app_server(
            ['codex'], env={}, cwd='.', model='test', prompt='draw',
            image_paths=[], reasoning_summary='none', image_results=True, spawn=AsyncMock(return_value=process))]
        images = [e for e in events if e.kind == 'image']
        self.assertEqual(len([e for e in events if e.kind == 'image_started']), 1)
        self.assertEqual(len(images), 1)
        self.assertEqual(base64.b64decode(images[0].image['result']), sample_image())

    async def test_selected_codex_keeps_named_references_and_never_falls_back(self):
        references = [{'name': name, 'path': f'{i}.png'} for i, name in enumerate(['Aion', 'Ithil', 'Connor'])]
        with patch.dict(image_gen.SETTINGS, {'image_gen_provider': 'codex'}, clear=True), \
                patch('image_gen._generate_codex_image', new=AsyncMock(return_value=None), create=True) as generate, \
                patch('image_gen.get_key', return_value='') as key:
            self.assertIsNone(await image_gen.generate_image('三人同框', references=references))
        generate.assert_awaited_once_with('三人同框', False, '', references=references)
        key.assert_not_called()

    async def test_codex_sends_all_named_photos_and_saves_image(self):
        import codex_image_gen as provider
        captured = {}
        async def stream(command, **kwargs):
            captured.update(kwargs, command=command)
            yield CodexAppServerEvent('image', image={
                'status': 'completed', 'result': base64.b64encode(sample_image()).decode()})
            yield CodexAppServerEvent('completed')
        with tempfile.TemporaryDirectory() as tmp:
            refs = []
            for i, name in enumerate(['Aion', 'Ithil', 'Connor']):
                path = Path(tmp) / f'{i}.png'
                path.write_bytes(sample_image())
                refs.append({'name': name, 'path': str(path)})
            with patch.object(provider, '_build_codex_chat_environment', return_value={'CODEX_HOME': tmp}), \
                    patch.object(provider, '_build_codex_chat_command', return_value=['node', 'codex.js', '-c', 'features.image_generation=false', 'app-server', '--stdio']), \
                    patch.object(provider, 'stream_codex_app_server', stream), \
                    patch.object(provider, 'save_generated_image', new=AsyncMock(return_value='album/picture.png')) as save:
                result = await provider.generate_codex_image('三人同框', references=refs)
            self.assertEqual(result, 'album/picture.png')
            self.assertEqual(captured['image_paths'], [r['path'] for r in refs])
            for i, ref in enumerate(refs, 1):
                self.assertIn(f'参考图 {i} 对应人物：{ref["name"]}', captured['prompt'])
            save.assert_awaited_once()
            self.assertEqual(save.call_args.args[0], sample_image())


if __name__ == '__main__':
    unittest.main()
