import unittest
from unittest.mock import patch

import ai_providers
import chatroom


class RoleDeviceContextInjectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_insertion_places_device_context_before_memory_and_body(self):
        messages = [
            {"role": "user", "content": "[系统能力]\n能力说明"},
            {"role": "assistant", "content": "好的，需要时我会使用这些指令。"},
            {"role": "user", "content": "系统当前的准确时间是 12:00\n\n[背景记忆]\n旧事"},
            {"role": "assistant", "content": "收到，我会在合适的时候自然提及。"},
            {"role": "user", "content": "当前用户消息 - User：正文"},
        ]

        with patch(
            "activity.get_device_context_for_prompt",
            return_value="【设备当前状态】\n电脑：刚刚有键鼠输入。",
        ):
            prepared = ai_providers.with_current_device_context(
                messages,
                insert_at=2,
                add_acknowledgement=True,
            )

        contents = [message["content"] for message in prepared]
        self.assertEqual("【设备当前状态】\n电脑：刚刚有键鼠输入。", contents[2])
        self.assertEqual("收到，我会结合当前设备状态判断。", contents[3])
        self.assertIn("[背景记忆]", contents[4])
        self.assertNotIn("【设备当前状态】", contents[-1])

    async def test_main_ai_request_receives_current_device_context(self):
        captured = []

        async def fake_provider(messages, config, meta, temperature, max_tokens):
            captured.extend(messages)
            yield "ok"

        model = {
            "provider": "custom_openai",
            "model": "role-test",
            "vision": True,
            "audio": False,
        }
        with (
            patch.object(ai_providers, "MODELS", {"role-test": model}),
            patch.object(ai_providers, "resolve_model_key", return_value="role-test"),
            patch.object(ai_providers, "is_model_deprecated", return_value=False),
            patch.object(ai_providers, "call_custom_openai", new=fake_provider),
            patch(
                "activity.get_device_context_for_prompt",
                return_value="【设备当前状态】\n电脑：刚刚有键鼠输入。",
            ),
        ):
            chunks = [
                chunk
                async for chunk in ai_providers.stream_ai(
                    [{"role": "user", "content": "哨兵刚刚唤醒了你。"}],
                    "role-test",
                )
            ]

        self.assertEqual(["ok"], chunks)
        prompt = "\n".join(str(message.get("content") or "") for message in captured)
        self.assertIn("【设备当前状态】", prompt)
        self.assertIn("电脑：刚刚有键鼠输入。", prompt)

    async def test_existing_device_context_is_not_duplicated(self):
        captured = []

        async def fake_provider(messages, config, meta, temperature, max_tokens):
            captured.extend(messages)
            yield "ok"

        model = {
            "provider": "custom_openai",
            "model": "role-test",
            "vision": True,
            "audio": False,
        }
        existing = "【设备当前状态】\n手机：亮屏、前台为 微信。"
        with (
            patch.object(ai_providers, "MODELS", {"role-test": model}),
            patch.object(ai_providers, "resolve_model_key", return_value="role-test"),
            patch.object(ai_providers, "is_model_deprecated", return_value=False),
            patch.object(ai_providers, "call_custom_openai", new=fake_provider),
            patch(
                "activity.get_device_context_for_prompt",
                return_value="【设备当前状态】\n手机：亮屏、前台为 Chrome。",
            ),
        ):
            chunks = [
                chunk
                async for chunk in ai_providers.stream_ai(
                    [{"role": "user", "content": existing}],
                    "role-test",
                )
            ]

        self.assertEqual(["ok"], chunks)
        prompt = "\n".join(str(message.get("content") or "") for message in captured)
        self.assertEqual(1, prompt.count("【设备当前状态】"))
        self.assertIn("前台为 微信", prompt)
        self.assertNotIn("前台为 Chrome", prompt)

    async def test_second_ai_codex_request_receives_current_device_context(self):
        captured = []

        async def fake_codex(messages, model, meta):
            captured.extend(messages)
            yield "ok"

        with (
            patch.object(chatroom, "call_codex_cli", new=fake_codex),
            patch.object(chatroom, "_read_connor_persona", return_value=""),
            patch.object(chatroom, "MODELS", {"Codex": {"model": "role-test"}}),
            patch(
                "activity.get_device_context_for_prompt",
                return_value="【设备当前状态】\n手机：亮屏、前台为 微信。",
            ),
        ):
            chunks = [
                chunk
                async for chunk in chatroom.stream_connor_cli(
                    messages=[{"role": "user", "content": "监督到时间了。"}],
                )
            ]

        self.assertEqual(["ok"], chunks)
        prompt = "\n".join(str(message.get("content") or "") for message in captured)
        self.assertIn("【设备当前状态】", prompt)
        self.assertIn("前台为 微信", prompt)

    async def test_second_ai_non_codex_request_receives_current_device_context(self):
        captured = []

        async def fake_provider(messages, config, meta, temperature, max_tokens):
            captured.extend(messages)
            yield "ok"

        model = {
            "provider": "custom_openai",
            "model": "role-test",
            "vision": True,
            "audio": False,
        }
        with (
            patch.object(ai_providers, "MODELS", {"role-test": model}),
            patch.object(ai_providers, "resolve_model_key", return_value="role-test"),
            patch.object(ai_providers, "is_model_deprecated", return_value=False),
            patch.object(ai_providers, "call_custom_openai", new=fake_provider),
            patch.object(ai_providers, "_save_model_raw_response"),
            patch(
                "activity.get_device_context_for_prompt",
                return_value="【设备当前状态】\n手机：亮屏、前台为 微信。",
            ),
        ):
            reply = await chatroom.simple_connor_cli_call(
                "监督到时间了。",
                model_key="role-test",
            )

        self.assertEqual("ok", reply)
        prompt = "\n".join(str(message.get("content") or "") for message in captured)
        self.assertIn("【设备当前状态】", prompt)
        self.assertIn("前台为 微信", prompt)


if __name__ == "__main__":
    unittest.main()
