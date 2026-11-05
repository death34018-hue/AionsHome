import asyncio
import sys
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai_providers
import config
import memory
from routes import settings as settings_routes


def _config_overrides(command):
    return [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "-c"
    ]


class SentinelRouteConfigTests(unittest.TestCase):
    def setUp(self):
        self.saved_settings = dict(config.SETTINGS)

    def tearDown(self):
        config.SETTINGS.clear()
        config.SETTINGS.update(self.saved_settings)

    def test_existing_settings_keep_using_original_route(self):
        config.SETTINGS.pop("sentinel_route", None)
        config.SETTINGS.update({
            "sentinel_base_url": "https://api.siliconflow.cn",
            "sentinel_api_key": "test-key",
            "sentinel_model": "Qwen/Qwen3.6-35B-A3B",
        })

        sentinel = config.get_sentinel_config()

        self.assertEqual(sentinel["route"], "original")
        self.assertEqual(sentinel["provider"], "openai_compatible")
        self.assertEqual(sentinel["model"], "Qwen/Qwen3.6-35B-A3B")
        self.assertEqual(sentinel["api_key"], "test-key")

    def test_luna_route_uses_codex_login_without_api_key(self):
        config.SETTINGS["sentinel_route"] = "codex_luna"

        sentinel = config.get_sentinel_config()

        self.assertEqual(sentinel["route"], "codex_luna")
        self.assertEqual(sentinel["provider"], "codex")
        self.assertEqual(sentinel["model"], "gpt-5.6-luna")
        self.assertEqual(sentinel["reasoning_effort"], "none")
        self.assertTrue(sentinel["ready"])

    def test_settings_endpoint_persists_selected_route(self):
        with patch.object(settings_routes, "save_settings") as save:
            result = asyncio.run(settings_routes.update_settings(
                settings_routes.SettingsUpdate(sentinel_route="codex_luna")
            ))

        self.assertEqual(result, {"ok": True})
        self.assertEqual(config.SETTINGS["sentinel_route"], "codex_luna")
        save.assert_called_once()


class SentinelLunaDispatchTests(unittest.TestCase):
    def test_image_status_is_replaced_before_waiting_for_chat_model(self):
        prepared = [{"role": "user", "content": "[图片内容：午饭]"}]

        async def provider(*args, request_timeout=120):
            yield "回复正文"

        async def collect():
            return [chunk async for chunk in ai_providers.stream_ai(
                [{"role": "user", "content": "吃完了"}],
                "unit-non-vision", include_device_context=False,
            )]

        with (
            patch.dict(ai_providers.MODELS, {"unit-non-vision": {
                "provider": "custom_openai", "model": "unit-model", "vision": False,
            }}),
            patch.object(ai_providers, "_messages_have_images", return_value=True),
            patch.object(ai_providers, "_sentinel_describe_images", AsyncMock(return_value=prepared)),
            patch.object(ai_providers, "call_custom_openai", provider),
        ):
            chunks = asyncio.run(collect())

        self.assertIn("识别图片", chunks[0])
        self.assertTrue(chunks[1].startswith(ai_providers.CLI_STATUS_PREFIX))
        self.assertIn("等待模型回复", chunks[1])
        self.assertEqual(chunks[-1], "回复正文")

    def test_vision_call_dispatches_to_codex_luna(self):
        call = AsyncMock(return_value='{"call_core": false}')
        sentinel = {
            "provider": "codex",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "none",
        }

        with patch.object(ai_providers, "call_codex_sentinel", call, create=True):
            result = asyncio.run(
                memory._call_sentinel_vision(
                    sentinel,
                    "巡逻提示词",
                    "ZmFrZS1qcGVn",
                    "image/jpeg",
                    timeout=37,
                )
            )

        self.assertEqual(result, '{"call_core": false}')
        call.assert_awaited_once_with(
            "巡逻提示词",
            model="gpt-5.6-luna",
            image_b64="ZmFrZS1qcGVn",
            mime_type="image/jpeg",
            timeout=37,
        )

    def test_codex_sentinel_command_disables_reasoning_and_extra_context(self):
        command = ai_providers._build_codex_sentinel_command(
            "node",
            "codex.js",
        )

        parsed = tomllib.loads("\n".join(_config_overrides(command)))
        self.assertEqual(parsed["model_reasoning_effort"], "none")
        self.assertEqual(parsed["model_verbosity"], "low")
        self.assertFalse(parsed["features"]["shell_tool"])
        self.assertFalse(parsed["features"]["multi_agent"])
        self.assertFalse(parsed["include_apps_instructions"])
        self.assertNotIn("model_instructions_file", parsed)


if __name__ == "__main__":
    unittest.main()
