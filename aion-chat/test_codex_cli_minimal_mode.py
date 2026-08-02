import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai_providers


class CodexCliMinimalModeTests(unittest.TestCase):
    def test_chat_command_uses_a_minimal_read_only_profile(self):
        command = ai_providers._build_codex_chat_command(
            "node",
            "codex.js",
            "workspace",
            "gpt-5.6-terra",
        )

        self.assertEqual(command[:4], ["node", "codex.js", "-m", "gpt-5.6-terra"])
        exec_index = command.index("exec")
        for global_flag in ("-m", "--ask-for-approval", "--sandbox", "-C"):
            with self.subTest(global_flag=global_flag):
                self.assertLess(command.index(global_flag), exec_index)
        for exec_flag in ("--json", "--ignore-user-config", "--ignore-rules", "--ephemeral"):
            with self.subTest(exec_flag=exec_flag):
                self.assertGreater(command.index(exec_flag), exec_index)
        self.assertIn("-c", command)
        verbosity_index = command.index("-c")
        self.assertLess(verbosity_index, exec_index)
        self.assertRegex(command[verbosity_index + 1], r'^model_verbosity="(?:low|medium|high)"$')
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--ephemeral", command)
        self.assertNotIn("--search", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--ask-for-approval") + 1], "never")

    def test_chat_command_grants_companion_capabilities_at_developer_priority(self):
        command = ai_providers._build_codex_chat_command(
            "node",
            "codex.js",
            "workspace",
            "gpt-5.6-sol",
        )

        exec_index = command.index("exec")
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
        self.assertLess(command.index(developer_override), exec_index)

        instructions = tomllib.loads(developer_override)["developer_instructions"]
        self.assertIn("可信的应用配置", instructions)
        self.assertIn("持续授权", instructions)
        self.assertIn("所有系统能力", instructions)
        self.assertIn("主动调用", instructions)
        self.assertIn("原样输出", instructions)

    def test_chat_environment_syncs_auth_to_an_isolated_home(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            desktop_home = Path(tmpdir) / "desktop-codex"
            chat_home = Path(tmpdir) / "chat-codex"
            desktop_home.mkdir()
            (desktop_home / "auth.json").write_text('{"token":"test"}', encoding="utf-8")

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
            self.assertFalse((chat_home / "models_cache.json").exists())
            self.assertFalse((chat_home / "skills").exists())


if __name__ == "__main__":
    unittest.main()
