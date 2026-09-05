"""Offline checks: never execute the real shopping client in these tests."""
import json
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

import taobao_native_mcp as bridge


class NativeSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_known_working_node_command_and_stdin(self):
        response = subprocess.CompletedProcess([], 0, '{"result":{"products":[]}}', '')
        with patch.object(bridge.subprocess, "run", return_value=response) as run, \
             patch.object(bridge.asyncio, "create_subprocess_exec", new=AsyncMock(
                 side_effect=AssertionError("Legacy Electron launcher must not run"))):
            result = await bridge.search_products("桌面手机支架")
        self.assertEqual(result, {"products": []})
        run.assert_called_once_with(
            ["C:/Program Files/nodejs/node.exe", "H:/taobao/bin/cli-rpc.js", "--stdin"],
            input=json.dumps({"tool": "search_products", "arguments": {
                "keyword": "桌面手机支架", "type": "all", "sourceApp": "AionsHome",
            }}, ensure_ascii=False),
            encoding="utf-8", capture_output=True, timeout=120,
        )

    async def test_login_failure_is_reported_without_retry(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({
            "error": "未登录，已打开登录页面，请先登录淘宝账号",
        }), '')
        with patch.object(bridge.subprocess, "run", return_value=response) as run, \
             patch.object(bridge.asyncio, "create_subprocess_exec", new=AsyncMock(
                 side_effect=AssertionError("Legacy Electron launcher must not run"))):
            with self.assertRaisesRegex(RuntimeError, "未登录"):
                await bridge.search_products("桌面手机支架")
        self.assertEqual(run.call_count, 1)
