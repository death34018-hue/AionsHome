import os
import sys
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(__file__))

import capabilities
import context_builder
from context_builder import strip_tool_commands
from web_search import WebCommandStreamFilter


class ActiveMemoryCapabilityTest(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_is_present_only_when_enabled_or_not_excluded(self):
        with patch.object(capabilities, "is_capability_enabled", return_value=True):
            enabled = await capabilities.build_capability_prompt_items("宝宝")
            excluded = await capabilities.build_capability_prompt_items(
                "宝宝", excluded_capabilities={"memory_search"}
            )
        self.assertTrue(any("MEMORY_SEARCH" in item for item in enabled))
        self.assertFalse(any("MEMORY_SEARCH" in item for item in excluded))

        def enabled_except_memory(key):
            return key != "memory_search"

        with patch.object(capabilities, "is_capability_enabled", side_effect=enabled_except_memory):
            disabled = await capabilities.build_capability_prompt_items("宝宝")
        self.assertFalse(any("MEMORY_SEARCH" in item for item in disabled))

    def test_capability_is_default_enabled_and_user_toggleable(self):
        item = capabilities.get_capability_def("memory_search")
        self.assertIsNotNone(item)
        self.assertTrue(item.default_enabled)
        self.assertIsNone(item.setting_key)

    def test_memory_command_is_hidden_from_stream(self):
        stream_filter = WebCommandStreamFilter()
        visible = stream_filter.feed("[MEMORY_SEARCH:Ctrl+X|relevant]") + stream_filter.flush()
        self.assertEqual("", visible)

    def test_full_width_memory_commands_are_hidden_but_preface_stays(self):
        for command in (
            "【MEMORY_SEARCH：辣椒炒肉|latest】",
            "［MEMORY_SEARCH:辣椒炒肉|latest］",
            "【MEMORY_SEARCH:辣椒炒肉|latest]",
        ):
            with self.subTest(command=command):
                stream_filter = WebCommandStreamFilter()
                visible = stream_filter.feed("我翻翻看。" + command) + stream_filter.flush()
                self.assertEqual("我翻翻看。", visible)

    def test_ordinary_stream_is_unchanged(self):
        stream_filter = WebCommandStreamFilter()
        visible = stream_filter.feed("宝宝，我记得这件事。") + stream_filter.flush()
        self.assertEqual("宝宝，我记得这件事。", visible)

    def test_mixed_reply_never_persists_memory_tool_tag(self):
        self.assertEqual(
            "我需要再确认一下。",
            strip_tool_commands("我需要再确认一下。[MEMORY_SEARCH:开斯婷|latest]"),
        )

    async def test_ability_block_forwards_second_stage_exclusion(self):
        builder = AsyncMock(return_value=[])
        with (
            patch.object(context_builder, "build_capability_prompt_items", builder),
            patch.object(context_builder, "is_capability_enabled", return_value=False),
            patch.object(context_builder, "get_device_context_for_prompt", return_value=""),
        ):
            await context_builder.build_ability_block(
                "宝宝", excluded_capabilities={"memory_search"}
            )
        self.assertEqual(
            {"memory_search"}, builder.await_args.kwargs["excluded_capabilities"]
        )

    async def test_prompt_allows_contextual_preface_without_fixed_wording(self):
        with patch.object(capabilities, "is_capability_enabled", return_value=True):
            items = await capabilities.build_capability_prompt_items("宝宝")
        prompt = next(item for item in items if "MEMORY_SEARCH" in item)
        self.assertIn("根据上下文", prompt)
        self.assertIn("不要固定", prompt)


if __name__ == "__main__":
    unittest.main()
