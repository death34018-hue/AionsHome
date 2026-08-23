import unittest
from unittest.mock import AsyncMock, patch

import context_builder


class DeviceContextPromptIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_current_device_context_is_added_to_shared_ability_block(self):
        with (
            patch.object(
                context_builder,
                "build_capability_prompt_items",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(context_builder, "is_capability_enabled", return_value=False),
            patch.object(context_builder, "build_cli_file_storage_text", return_value=""),
            patch.object(
                context_builder,
                "get_device_context_for_prompt",
                return_value="【用户当前情境】\n- 正在电脑前操作。",
            ) as context_prompt,
        ):
            result = await context_builder.build_ability_block("Ithil")

        self.assertIn("【用户当前情境】", result)
        context_prompt.assert_called_once_with()

    async def test_empty_device_context_adds_nothing(self):
        with (
            patch.object(
                context_builder,
                "build_capability_prompt_items",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(context_builder, "is_capability_enabled", return_value=False),
            patch.object(context_builder, "build_cli_file_storage_text", return_value=""),
            patch.object(context_builder, "get_device_context_for_prompt", return_value=""),
        ):
            result = await context_builder.build_ability_block("Ithil")

        self.assertEqual("", result)


if __name__ == "__main__":
    unittest.main()
