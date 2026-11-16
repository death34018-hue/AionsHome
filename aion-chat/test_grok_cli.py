import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import grok_cli
import config


class FakeProcess:
    def __init__(self, events, *, eof=True):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(''.join(json.dumps(e) + '\n' for e in events).encode())
        if eof:
            self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode = 0 if eof else None
        self.pid = None
        self.wait = AsyncMock(return_value=0)


class GrokTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_separates_thought_and_finishes_without_waiting_for_eof(self):
        process = FakeProcess([
            {'type': 'available_commands', 'tools': ['read_file']},
            {'type': 'thought', 'data': 'thinking'},
            {'type': 'text', 'data': '你'}, {'type': 'text', 'data': '好'},
            {'type': 'end', 'stopReason': 'end_turn', 'usage': {'input_tokens': 5}},
        ], eof=False)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            grok_cli.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)
        ) as spawn, patch.object(grok_cli, 'terminate_process_tree', new=AsyncMock()) as stop:
            events = await asyncio.wait_for(self.collect(directory), 1)
            command = spawn.call_args.args
            self.assertEqual(command[command.index('--reasoning-effort') + 1], 'high')
            self.assertIn('--prompt-file', command)
            self.assertNotIn('XAI_API_KEY', spawn.call_args.kwargs['env'])
            self.assertEqual(list(Path(directory).glob('*.txt')), [])
        self.assertEqual(''.join(e.get('text', '') for e in events if e['kind'] == 'text'), '你好')
        self.assertEqual([e['text'] for e in events if e['kind'] == 'thought'], ['thinking'])
        self.assertEqual(events[-1]['kind'], 'completed')
        stop.assert_awaited_once_with(process)

    async def collect(self, workspace):
        return [event async for event in grok_cli.stream_chat('fake', 'hello', 'grok-4.7',
                                                              reasoning_effort='high', workspace=workspace)]

    async def test_truncated_stream_and_tools_are_errors(self):
        for events in ([{'type': 'text', 'data': 'unfinished'}],
                       [{'type': 'tool_start', 'toolName': 'run_terminal_command'}],
                       [{'type': 'error', 'message': 'quota exceeded'}]):
            with self.subTest(events=events), tempfile.TemporaryDirectory() as directory, patch.object(
                grok_cli.asyncio, 'create_subprocess_exec', AsyncMock(return_value=FakeProcess(events))
            ), patch.object(grok_cli, 'terminate_process_tree', new=AsyncMock()):
                with self.assertRaises(grok_cli.GrokError):
                    await self.collect(directory)

    async def test_cancellation_cleans_process_and_prompt(self):
        process = FakeProcess([], eof=False)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            grok_cli.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)
        ), patch.object(grok_cli, 'terminate_process_tree', new=AsyncMock()) as stop:
            task = asyncio.create_task(self.collect(directory))
            await asyncio.sleep(.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            stop.assert_awaited_once_with(process)
            self.assertEqual(list(Path(directory).glob('*.txt')), [])

    def test_models_have_correct_efforts_and_default_to_high(self):
        rows = [v for v in config.MODELS.values() if v['provider'] == 'grok_cli']
        self.assertEqual(len(rows), 4)
        with patch.dict(config.SETTINGS, {'grok_reasoning_efforts': {}}, clear=False):
            self.assertTrue(all(config.resolve_grok_reasoning_effort(row['model']) == 'high' for row in rows))
        self.assertEqual(config.normalize_grok_reasoning_efforts({
            'grok-4.7': 'xhigh', 'grok-4.5': 'xhigh', 'grok-unknown': 'low',
        }), {'grok-4.7': 'xhigh'})

    async def test_provider_routes_effort_and_usage_without_leaking_thought(self):
        import ai_providers
        async def fake(*args, **kwargs):
            self.assertEqual(kwargs['reasoning_effort'], 'medium')
            self.assertIn('role context', args[1])
            yield {'kind': 'thought', 'text': 'reasoning summary'}
            yield {'kind': 'text', 'text': '你好'}
            yield {'kind': 'completed', 'usage': {'input_tokens': 10, 'cache_read_input_tokens': 4,
                   'output_tokens': 5, 'reasoning_tokens': 2, 'total_tokens': 19}}
        meta = {}
        with patch.object(grok_cli, 'find_binary', return_value='fake'), patch.object(
            grok_cli, 'stream_chat', fake
        ), patch.dict(config.SETTINGS, {'grok_reasoning_efforts': {'grok-4.7': 'medium'}}):
            chunks = [c async for c in ai_providers.stream_ai(
                [{'role': 'system', 'content': 'role context'}, {'role': 'user', 'content': 'hello'}],
                'Grok · 4.7', meta, include_device_context=False)]
        self.assertEqual(''.join(c for c in chunks if isinstance(c, str)), '你好')
        self.assertEqual(meta['reasoning_content'], 'reasoning summary')
        self.assertEqual(meta['prompt_tokens'], 14)
        self.assertEqual(meta['total_tokens'], 19)

    async def test_settings_round_trip(self):
        from routes import settings
        with patch.dict(config.SETTINGS, {'grok_reasoning_efforts': {}}), patch.object(settings, 'save_settings'):
            rows = (await settings.get_settings())['grok_models']
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row['reasoning_effort'] == 'high' for row in rows))
            await settings.update_settings(settings.SettingsUpdate(grok_reasoning_efforts={'grok-4.7': 'xhigh'}))
            rows = (await settings.get_settings())['grok_models']
            self.assertEqual(rows[0]['reasoning_effort'], 'xhigh')
            self.assertNotIn('xhigh', rows[-1]['efforts'])

    async def test_errors_reach_shared_consumers(self):
        import ai_providers
        from safe_live_stream import has_provider_error_marker
        from diary import diary_response_error
        from routes.chat import _is_ai_error_text
        meta = {}
        with patch.object(grok_cli, 'find_binary', return_value=None):
            chunks = [c async for c in ai_providers.call_grok_cli([], 'grok-4.7', meta)]
        self.assertTrue(meta['provider_error'])
        self.assertTrue(has_provider_error_marker(chunks[0]))
        self.assertTrue(diary_response_error(chunks[0]))
        self.assertTrue(_is_ai_error_text(chunks[0]))


if __name__ == '__main__':
    unittest.main()
