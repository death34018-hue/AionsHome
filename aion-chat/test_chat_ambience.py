import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import capabilities
import chat_ambience


class ChatAmbienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(chat_ambience, 'STATE_PATH', Path(temp.name) / 'chat_ambience.json'))
        self.broadcast = self.enterContext(patch.object(chat_ambience.manager, 'broadcast', AsyncMock()))
        self.enabled = self.enterContext(
            patch.object(capabilities, 'is_capability_enabled', side_effect=lambda key: key == 'chat_ambience')
        )

    async def test_prompt_is_on_by_default_and_reports_current_state(self):
        self.assertTrue(capabilities.get_capability_def('chat_ambience').default_enabled)
        self.assertIsNone(chat_ambience.load_state())
        self.assertTrue(chat_ambience.ability_text('测试月亮').endswith('\n当前小家氛围：模式待同步｜无｜不叠加颜色'))
        await chat_ambience.set_state({'effect': 'snow', 'tint': '#ff5a5f'})
        await chat_ambience.api_set_state({'theme': 'light'})
        prompt = '\n'.join(await capabilities.build_capability_prompt_items('测试月亮'))
        self.assertIn('[小家氛围：效果|颜色编号|模式]', prompt)
        self.assertIn('测试月亮', prompt)
        # 当前状态跟在指令说明后面，单独一行
        self.assertIn('放一个特效。\n当前小家氛围：日间｜下雪｜红色', prompt)
        await chat_ambience.set_state({'tint': '#123456'})
        self.assertTrue(chat_ambience.ability_text('测试月亮').endswith('\n当前小家氛围：日间｜下雪｜自定义颜色 #123456'))
        excluded = await capabilities.build_capability_prompt_items('测试月亮', excluded_capabilities={'chat_ambience'})
        self.assertFalse(any('小家氛围' in text for text in excluded))

    async def test_ai_command_switches_state_and_is_removed_from_reply(self):
        cleaned = await chat_ambience.process_ambience_commands('外面下雨了。[小家氛围：下雨|5]早点睡。', sender='aion')
        self.assertEqual(cleaned, '外面下雨了。早点睡。')
        self.assertEqual(chat_ambience.load_state(), {'effect': 'rain', 'tint': '#45a8ff'})
        self.broadcast.assert_awaited_once_with(
            {'type': 'chat_ambience', 'data': {'effect': 'rain', 'tint': '#45a8ff', 'source': ''}}
        )
        # 只写效果时颜色不变；全角分隔符和 0 号颜色也能识别
        await chat_ambience.process_ambience_commands('[小家氛围:落叶]', sender='connor')
        self.assertEqual(chat_ambience.load_state(), {'effect': 'leaves', 'tint': '#45a8ff'})
        await chat_ambience.process_ambience_commands('[小家氛围：无｜0]', sender='aion')
        self.assertEqual(chat_ambience.load_state(), {'effect': 'none', 'tint': ''})

    async def test_theme_sync_between_existing_clients_and_both_senders(self):
        await chat_ambience.set_state({'effect': 'rain', 'tint': '#123456'})
        await chat_ambience.api_set_state({'only_missing': True, 'theme': 'light', 'effect': 'none', 'tint': ''})
        self.assertEqual(chat_ambience.state_text(), '当前小家氛围：日间｜下雨｜自定义颜色 #123456')
        cleaned = await chat_ambience.process_ambience_commands('晚安。[小家氛围：夜间]', sender='connor')
        self.assertEqual(cleaned, '晚安。')
        self.assertEqual(self.broadcast.await_args.args[0]['data']['theme'], 'dark')
        self.broadcast.reset_mock()
        await chat_ambience.api_set_state({'only_missing': True, 'theme': 'light'})
        self.broadcast.assert_not_awaited()
        self.assertEqual(chat_ambience.state_text(), '当前小家氛围：夜间｜下雨｜自定义颜色 #123456')
        await chat_ambience.process_ambience_commands('[小家氛围：樱花|1|日间]', sender='aion')
        self.assertEqual(chat_ambience.state_text(), '当前小家氛围：日间｜樱花｜红色')

    async def test_one_shot_bursts_play_without_changing_the_ambience(self):
        await chat_ambience.process_ambience_commands('[小家氛围：樱花|1]', sender='aion')
        self.assertEqual(chat_ambience.load_state(), {'effect': 'sakura', 'tint': '#ff5a5f'})
        cleaned = await chat_ambience.process_ambience_commands('生日快乐！[小家氛围：撒花]', sender='connor')
        self.assertEqual(cleaned, '生日快乐！')
        self.assertEqual(chat_ambience.load_state(), {'effect': 'sakura', 'tint': '#ff5a5f'})
        self.assertEqual(
            self.broadcast.await_args.args[0]['data'],
            {'effect': 'sakura', 'tint': '#ff5a5f', 'source': '', 'burst': 'confetti'},
        )
        # 同一条回复里既切换又放特效
        await chat_ambience.process_ambience_commands('[小家氛围：极光|6][小家氛围：流星]', sender='aion')
        self.assertEqual(chat_ambience.load_state(), {'effect': 'aurora', 'tint': '#4656c9'})
        self.assertEqual(self.broadcast.await_args.args[0]['data']['burst'], 'meteor')

    async def test_burst_before_any_saved_state_does_not_create_one(self):
        await chat_ambience.process_ambience_commands('[小家氛围：烟花]', sender='aion')
        self.assertIsNone(chat_ambience.load_state())
        self.assertEqual(self.broadcast.await_args.args[0]['data'], {'source': '', 'burst': 'fireworks'})

    async def test_prompt_and_frontend_list_every_effect(self):
        prompt = chat_ambience.ability_text('测试月亮')
        for name in [*chat_ambience.EFFECTS.values(), *chat_ambience.BURSTS]:
            self.assertIn(name, prompt)
        source = (Path(__file__).parent / 'static' / 'chat-ambience.js').read_text(encoding='utf-8')
        frontend_ids = set(re.findall(r"\['(\w+)', '[^']+', '[^']+'\]", source))
        backend_ids = (set(chat_ambience.EFFECTS) - {'none'}) | set(chat_ambience.BURSTS.values())
        self.assertEqual(frontend_ids, backend_ids)

    async def test_disabled_unknown_and_user_commands_change_nothing(self):
        await chat_ambience.process_ambience_commands('[小家氛围：下冰雹|9]', sender='aion')
        await chat_ambience.process_ambience_commands('[小家氛围：下雪|1]', sender='user')
        self.enabled.side_effect = None
        self.enabled.return_value = False
        cleaned = await chat_ambience.process_ambience_commands('好呀[小家氛围：下雪|1]', sender='aion')
        self.assertEqual(cleaned, '好呀')
        self.assertIsNone(chat_ambience.load_state())
        self.broadcast.assert_not_awaited()

    async def test_manual_change_keeps_only_valid_fields(self):
        result = await chat_ambience.api_set_state({'effect': 'wind', 'tint': 'red', 'source': 'abc'})
        self.assertEqual(result['state'], {'effect': 'wind', 'tint': ''})
        self.assertEqual(self.broadcast.await_args.args[0]['data']['source'], 'abc')
        self.assertEqual((await chat_ambience.api_get_state())['state'], {'effect': 'wind', 'tint': ''})

    async def test_command_is_hidden_from_stream_and_speech(self):
        from context_builder import strip_tool_commands
        from safe_live_stream import consume_safe_live_stream
        from tts import _strip_tags
        from web_search import WebCommandStreamFilter

        text = '外面下雨了。[小家氛围：下雨|5]早点睡。'
        filtered = WebCommandStreamFilter()
        self.assertEqual(''.join(filtered.feed(char) for char in text) + filtered.flush(), '外面下雨了。早点睡。')
        self.assertEqual(strip_tool_commands(text), '外面下雨了。早点睡。')
        self.assertNotIn('小家氛围', _strip_tags(text))

        async def source():
            for char in text:
                yield char

        visible = []
        result = await consume_safe_live_stream(source(), visible.append)
        self.assertEqual(result.committed_text, text)
        self.assertEqual(''.join(visible), '外面下雨了。早点睡。')


if __name__ == '__main__':
    unittest.main()
