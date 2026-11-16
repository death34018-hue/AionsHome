import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import grok_image_gen as provider
import image_gen
from test_album import sample_image


class GrokImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_references_keep_order_and_route_to_image_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            refs = []
            for name in ('人物甲', '人物乙'):
                path = Path(tmp) / (name + '.png')
                path.write_bytes(sample_image())
                refs.append({'name': name, 'path': path})
            with patch.object(provider, '_generate_bytes', new=AsyncMock(return_value=sample_image())) as generate, \
                    patch.object(provider, 'save_generated_image', new=AsyncMock(return_value='album/photo.png')) as save:
                self.assertEqual(await provider.generate_grok_image('两人一起喝茶', references=refs), 'album/photo.png')
                request, paths = generate.call_args.args
                self.assertEqual(paths, [str(r['path'].resolve()) for r in refs])
                self.assertEqual(request['image'], paths)
                self.assertIn('参考图 1 对应人物：人物甲', request['prompt'])
                self.assertIn('参考图 2 对应人物：人物乙', request['prompt'])
                self.assertIn('两人一起喝茶', request['prompt'])
                self.assertEqual(save.call_args.kwargs['model'], 'grok/imagine')
                with self.assertRaisesRegex(ValueError, '5'):
                    await provider.generate_grok_image('合照', references=refs * 3)
                self.assertEqual(generate.await_count, 1)

    async def test_selfie_uses_configured_anchor_and_missing_anchor_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            anchor = Path(tmp) / 'secondary.png'
            anchor.write_bytes(sample_image())
            with patch.object(image_gen, 'SECONDARY_REFERENCE_IMAGE_PATH', anchor), \
                    patch.object(provider, '_generate_bytes', new=AsyncMock(return_value=sample_image())) as generate, \
                    patch.object(provider, 'save_generated_image', new=AsyncMock()) as save:
                await provider.generate_grok_image('自拍', True, 'connor')
                self.assertEqual(generate.call_args.args[1], [str(anchor.resolve())])
                self.assertEqual(save.call_args.kwargs['reference_bytes'], sample_image())
                anchor.unlink()
                with self.assertRaises(FileNotFoundError):
                    await provider.generate_grok_image('自拍', True, 'connor')

    async def test_shared_route_and_settings_keep_other_sources(self):
        from routes import settings
        from chatroom_image_retry import _provider
        with patch.dict(image_gen.SETTINGS, {'image_gen_provider': 'grok', 'image_gen_model': 'gpt-image-2.6-flare'}), \
                patch.object(provider, 'generate_grok_image', new=AsyncMock(return_value='album/grok.jpg')) as generate:
            self.assertEqual(await image_gen.generate_image('test', references=[{'name':'甲','path':'a.png'}]), 'album/grok.jpg')
            generate.assert_awaited_once_with('test', False, '', references=[{'name':'甲','path':'a.png'}])
            self.assertEqual(image_gen.image_model_label(), 'grok/imagine')
            self.assertEqual(_provider(), 'grok')
            self.assertFalse(image_gen.supports_gif_generation())
            generate.side_effect = RuntimeError('Grok quota')
            with self.assertRaisesRegex(RuntimeError, 'Grok quota'):
                await image_gen.generate_image('test', raise_errors=True)
            self.assertIsNone(await image_gen.generate_image('test'))
        with patch.dict(settings.SETTINGS, {'image_gen_provider':'existing', 'image_gen_api_key':'test-key'}), \
                patch.object(settings, 'save_settings'):
            await settings.update_settings(settings.SettingsUpdate(image_gen_provider='grok'))
            self.assertEqual(settings.SETTINGS['image_gen_provider'], 'grok')
            self.assertEqual(settings.SETTINGS['image_gen_api_key'], 'test-key')

    async def test_cli_only_accepts_its_image_and_cleans_process(self):
        from test_grok_cli import FakeProcess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = 'test-session'
            output = root / 'sessions' / 'cwd' / session / 'images' / '1.jpg'
            output.parent.mkdir(parents=True)
            output.write_bytes(sample_image())
            process = FakeProcess([
                {'type':'tool_call','toolCallId':'i','toolName':'image_gen','rawInput':{'prompt':'test'}},
                {'type':'tool_call_update','toolCallId':'i','status':'completed',
                 'rawOutput':{'type':'ImageGen','path':str(output)}}], eof=False)
            with patch.object(provider, 'find_binary', return_value='grok'), \
                    patch.object(provider, 'chat_environment', return_value={'GROK_HOME':tmp}), \
                    patch.object(provider.uuid, 'uuid4', return_value=session), \
                    patch.object(provider.asyncio, 'create_subprocess_exec', new=AsyncMock(return_value=process)) as spawn, \
                    patch.object(provider, 'terminate_process_tree', new=AsyncMock()) as stop:
                data = await asyncio.wait_for(provider._generate_bytes({'prompt':'test'}, [], workspace=root), 1)
                self.assertEqual(data, sample_image())
                command = spawn.call_args.args
                self.assertEqual(command[command.index('--tools')+1], 'image_gen')
                self.assertEqual(command[command.index('--reasoning-effort')+1], 'high')
                self.assertFalse(list(root.glob('*/prompt.txt')))
                stop.assert_awaited_once_with(process)
            with self.assertRaises(ValueError):
                provider._read_image(output, root / 'elsewhere', session)

    async def test_lost_references_failed_output_and_cancel_are_errors(self):
        from test_grok_cli import FakeProcess
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'anchor.jpg')
            for events in (
                [{'type':'tool_call','toolCallId':'i','toolName':'image_gen','rawInput':{}}],
                [{'type':'tool_call','toolCallId':'i','toolName':'image_edit','rawInput':{'image':[]}}],
                [{'type':'tool_call','toolCallId':'i','toolName':'image_edit','rawInput':{'image':[path]}},
                 {'type':'tool_call_update','toolCallId':'i','status':'failed','rawOutput':{}}],
                [{'type':'end','stopReason':'end_turn'}],
            ):
                with self.subTest(events=events), patch.object(provider, 'find_binary', return_value='grok'), \
                        patch.object(provider.asyncio, 'create_subprocess_exec', new=AsyncMock(return_value=FakeProcess(events))), \
                        patch.object(provider, 'terminate_process_tree', new=AsyncMock()):
                    with self.assertRaises((RuntimeError, ValueError)):
                        await provider._generate_bytes({'prompt':'test','image':[path]}, [path], workspace=tmp)
            process = FakeProcess([], eof=False)
            with patch.object(provider, 'find_binary', return_value='grok'), \
                    patch.object(provider.asyncio, 'create_subprocess_exec', new=AsyncMock(return_value=process)), \
                    patch.object(provider, 'terminate_process_tree', new=AsyncMock()) as stop:
                task = asyncio.create_task(provider._generate_bytes({'prompt':'test'}, [], workspace=tmp))
                await asyncio.sleep(.02)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                stop.assert_awaited_once_with(process)


if __name__ == '__main__':
    unittest.main()
