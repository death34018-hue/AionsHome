import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai_providers
from codex_app_server import CodexAppServerEvent
from stream_safety import StreamActivity


def _config_overrides(command):
    return [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "-c"
    ]


class CodexCliMinimalModeTests(unittest.TestCase):
    def test_chat_command_uses_a_minimal_read_only_profile(self):
        command = ai_providers._build_codex_chat_command(
            "node",
            "codex.js",
            "workspace",
            "gpt-5.6-terra",
            skill_files=(),
        )

        self.assertEqual(command[:2], ["node", "codex.js"])
        app_server_index = command.index("app-server")
        self.assertEqual(command[-2:], ["app-server", "--stdio"])
        for removed_flag in ("-m", "--ask-for-approval", "--sandbox", "-C", "exec", "--json"):
            with self.subTest(removed_flag=removed_flag):
                self.assertNotIn(removed_flag, command)
        self.assertIn("-c", command)
        verbosity_index = command.index("-c")
        self.assertLess(verbosity_index, app_server_index)
        self.assertRegex(command[verbosity_index + 1], r'^model_verbosity="(?:low|medium|high)"$')
        self.assertNotIn("--search", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_chat_command_grants_companion_capabilities_at_developer_priority(self):
        command = ai_providers._build_codex_chat_command(
            "node",
            "codex.js",
            "workspace",
            "gpt-5.6-sol",
            skill_files=(),
        )

        app_server_index = command.index("app-server")
        overrides = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "-c"
        ]
        developer_overrides = [
            value for value in overrides if value.startswith("developer_instructions=")
        ]
        self.assertEqual(len(developer_overrides), 1)
        developer_override = developer_overrides[0]
        self.assertLess(command.index(developer_override), app_server_index)

        instructions = tomllib.loads(developer_override)["developer_instructions"]
        self.assertIn("可信的应用配置", instructions)
        self.assertIn("持续授权", instructions)
        self.assertIn("所有系统能力", instructions)
        self.assertIn("主动调用", instructions)
        self.assertIn("原样输出", instructions)

    def test_chat_command_replaces_development_context_and_disables_dev_features(self):
        skill_files = [
            Path(r"C:\profiles\alpha\SKILL.md"),
            Path(r"C:\profiles\quote'and space\SKILL.md"),
        ]
        command = ai_providers._build_codex_chat_command(
            "node",
            "codex.js",
            "workspace",
            "gpt-5.6-terra",
            skill_files=skill_files,
        )

        app_server_index = command.index("app-server")
        overrides = _config_overrides(command)
        parsed = tomllib.loads("\n".join(overrides))

        self.assertEqual(
            Path(parsed["model_instructions_file"]),
            ai_providers._CODEX_COMPANION_INSTRUCTIONS_FILE.resolve(),
        )
        self.assertFalse(parsed["features"]["shell_tool"])
        self.assertFalse(parsed["features"]["multi_agent"])
        self.assertEqual(
            parsed["features"]["multi_agent_v2"]["root_agent_usage_hint_text"],
            "",
        )
        self.assertEqual(
            parsed["features"]["multi_agent_v2"]["multi_agent_mode_hint_text"],
            "",
        )
        self.assertFalse(parsed["features"]["remote_plugin"])
        self.assertFalse(parsed["include_apps_instructions"])
        self.assertFalse(parsed["include_permissions_instructions"])
        self.assertFalse(parsed["include_collaboration_mode_instructions"])
        self.assertFalse(parsed["include_environment_context"])
        self.assertEqual(
            {Path(item["path"]) for item in parsed["skills"]["config"]},
            {path.resolve() for path in skill_files},
        )
        self.assertTrue(
            all(not item["enabled"] for item in parsed["skills"]["config"])
        )
        self.assertTrue(all(command.index(value) < app_server_index for value in overrides))
        self.assertFalse(any("web_search" in value for value in overrides))
        self.assertFalse(any("view_image" in value for value in overrides))

    def test_real_cli_prompt_omits_multi_agent_guidance(self):
        script = ai_providers._CODEX_SCRIPT
        if not script or not Path(script).is_file():
            self.skipTest("bundled Codex CLI is unavailable")

        command = ai_providers._build_codex_chat_command(
            "node",
            script,
            ai_providers._CODEX_WORKSPACE,
            "",
        )
        app_server_index = command.index("app-server")
        command = command[:app_server_index] + ["debug", "prompt-input", "hello"]

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                **os.environ,
                "CODEX_HOME": tmpdir,
                "HOME": tmpdir,
                "USERPROFILE": tmpdir,
                "NO_COLOR": "1",
            }
            completed = subprocess.run(
                command,
                cwd=ai_providers._CODEX_WORKSPACE,
                env=env,
                capture_output=True,
                check=True,
            )

        items = json.loads(completed.stdout.decode("utf-8"))
        prompt_text = "\n".join(
            part.get("text", "")
            for item in items
            for part in item.get("content", [])
            if part.get("type") == "input_text"
        )
        self.assertNotIn("primary agent in a team of agents", prompt_text)
        self.assertNotIn("Do not spawn sub-agents unless", prompt_text)

    def test_companion_base_prompt_is_short_generic_and_non_coding(self):
        text = ai_providers._CODEX_COMPANION_INSTRUCTIONS_FILE.read_text(
            encoding="utf-8"
        )

        self.assertLess(len(text), 1000)
        for hardcoded_name in ("Aion", "Ithil", "Connor"):
            with self.subTest(hardcoded_name=hardcoded_name):
                self.assertNotIn(hardcoded_name.casefold(), text.casefold())
        self.assertIn("Do not inspect the workspace", text)
        self.assertIn("Never invent", text)

    def test_skill_discovery_finds_exact_skill_files_once_in_stable_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            alpha = root / "z-root" / "alpha" / "SKILL.md"
            beta = root / "a-root" / "beta" / "SKILL.md"
            alpha.parent.mkdir(parents=True)
            beta.parent.mkdir(parents=True)
            alpha.write_text("alpha", encoding="utf-8")
            beta.write_text("beta", encoding="utf-8")

            actual = ai_providers._find_codex_skill_files(
                [root / "z-root", root / "missing", root / "a-root", root / "z-root"]
            )
            expected = tuple(
                sorted({str(alpha.resolve()), str(beta.resolve())}, key=os.path.normcase)
            )

        self.assertEqual(actual, expected)

    def test_chat_command_uses_discovered_skills_when_not_injected(self):
        skill_file = Path(r"C:\profiles\discovered\SKILL.md").resolve()
        with patch.object(
            ai_providers,
            "_discover_codex_skill_files",
            return_value=(str(skill_file),),
            create=True,
        ):
            command = ai_providers._build_codex_chat_command(
                "node", "codex.js", "workspace", "gpt-5.6-terra"
            )

        skill_overrides = [
            value
            for value in _config_overrides(command)
            if value.startswith("skills.config=")
        ]
        self.assertEqual(len(skill_overrides), 1)
        parsed = tomllib.loads(skill_overrides[0])
        self.assertEqual(
            parsed["skills"]["config"],
            [{"path": str(skill_file), "enabled": False}],
        )

    def test_chat_environment_syncs_auth_to_an_isolated_home(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            desktop_home = Path(tmpdir) / "desktop-codex"
            chat_home = Path(tmpdir) / "chat-codex"
            desktop_home.mkdir()
            (desktop_home / "auth.json").write_text('{"token":"test"}', encoding="utf-8")
            desktop_config = desktop_home / "config.toml"
            desktop_config.write_text('model = "desktop-model"', encoding="utf-8")

            with (
                patch.object(ai_providers, "_CODEX_HOME", str(desktop_home)),
                patch.object(ai_providers, "_CODEX_CHAT_HOME", str(chat_home)),
            ):
                env = ai_providers._build_codex_chat_environment({"PATH": "test"})

            self.assertEqual((chat_home / "auth.json").read_text(encoding="utf-8"), '{"token":"test"}')
            self.assertEqual(env["CODEX_HOME"], str(chat_home))
            self.assertEqual(env["HOME"], str(chat_home.parent))
            self.assertEqual(env["USERPROFILE"], str(chat_home.parent))
            self.assertEqual(env["NO_COLOR"], "1")
            self.assertEqual(
                desktop_config.read_text(encoding="utf-8"),
                'model = "desktop-model"',
            )
            self.assertFalse((chat_home / "config.toml").exists())
            self.assertFalse((chat_home / "models_cache.json").exists())
            self.assertFalse((chat_home / "skills").exists())


class CodexCliMinimalModeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_companion_prompt_does_not_spawn_codex(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_prompt = Path(tmpdir) / "missing.md"
            with (
                patch.object(ai_providers, "_CODEX_SCRIPT", "codex.js"),
                patch.object(
                    ai_providers,
                    "_CODEX_COMPANION_INSTRUCTIONS_FILE",
                    missing_prompt,
                ),
            ):
                chunks = [
                    chunk
                    async for chunk in ai_providers.call_codex_cli(
                        [{"role": "user", "content": "hello"}],
                        "gpt-5.6-terra",
                    )
                ]

        self.assertEqual(len(chunks), 1)
        self.assertIn("陪伴模式基础指令文件缺失", chunks[0])

    async def test_app_server_events_map_to_existing_stream_contract(self):
        async def fake_stream(*_args, **_kwargs):
            yield CodexAppServerEvent("activity")
            yield CodexAppServerEvent("reasoning_delta", text="检查上下文")
            yield CodexAppServerEvent("text_delta", text="你")
            yield CodexAppServerEvent("text_delta", text="好")
            yield CodexAppServerEvent(
                "usage",
                usage={
                    "inputTokens": 10,
                    "cachedInputTokens": 2,
                    "outputTokens": 7,
                    "reasoningOutputTokens": 3,
                    "totalTokens": 17,
                },
            )
            yield CodexAppServerEvent("completed")

        meta = {}
        with (
            patch.object(ai_providers, "_CODEX_SCRIPT", "codex.js"),
            patch.object(
                ai_providers,
                "_CODEX_COMPANION_INSTRUCTIONS_FILE",
                Path(__file__),
            ),
            patch.object(ai_providers, "stream_codex_app_server", fake_stream),
            patch.object(ai_providers, "_build_codex_chat_environment", return_value={}),
        ):
            chunks = [
                chunk
                async for chunk in ai_providers.call_codex_cli(
                    [{"role": "user", "content": "hello"}],
                    "gpt-5.6-sol",
                    meta,
                )
            ]

        self.assertIsInstance(chunks[0], StreamActivity)
        self.assertIsInstance(chunks[1], StreamActivity)
        self.assertEqual(chunks[2:], ["你", "好", StreamActivity()])
        self.assertEqual(meta["reasoning_content"], "检查上下文")
        self.assertEqual(meta["prompt_tokens"], 10)
        self.assertEqual(meta["completion_tokens"], 7)
        self.assertEqual(meta["raw"]["completion_tokens_details"]["reasoning_tokens"], 3)


class CodexRuntimeConfigTests(unittest.TestCase):
    def test_reasoning_summary_is_configurable_with_safe_auto_fallback(self):
        self.assertEqual(ai_providers._codex_reasoning_summary({}), "auto")
        for value in ("auto", "concise", "detailed", "none"):
            with self.subTest(value=value):
                self.assertEqual(
                    ai_providers._codex_reasoning_summary(
                        {"codex_reasoning_summary": value}
                    ),
                    value,
                )
        self.assertEqual(
            ai_providers._codex_reasoning_summary(
                {"codex_reasoning_summary": "unexpected"}
            ),
            "auto",
        )

    def test_concurrency_is_configurable_and_bounded(self):
        self.assertEqual(ai_providers._codex_max_concurrency({}), 2)
        self.assertEqual(
            ai_providers._codex_max_concurrency(
                {"codex_max_concurrent_requests": 1}
            ),
            1,
        )
        self.assertEqual(
            ai_providers._codex_max_concurrency(
                {"codex_max_concurrent_requests": 99}
            ),
            8,
        )

    def test_stream_activity_is_safe_for_legacy_string_collectors(self):
        activity = StreamActivity()
        self.assertIsInstance(activity, str)
        self.assertEqual("prefix" + activity, "prefix")


if __name__ == "__main__":
    unittest.main()
