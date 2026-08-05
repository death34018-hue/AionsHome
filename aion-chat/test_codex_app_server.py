import asyncio
import json
import unittest

from codex_app_server import (
    CodexAppServerError,
    build_codex_app_server_command,
    stream_codex_app_server,
)


class _FakeStdin:
    def __init__(self, process):
        self.process = process
        self.messages = []
        self.closed = False

    def write(self, raw):
        self.messages.append(json.loads(raw.decode("utf-8")))

    async def drain(self):
        return None

    def close(self):
        self.closed = True
        self.process.returncode = 0

    async def wait_closed(self):
        return None


class _FakeReader:
    def __init__(self, messages):
        self.lines = [
            json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
            for message in messages
        ]

    async def readline(self):
        if self.lines:
            return self.lines.pop(0)
        return b""

    async def read(self, _size=-1):
        return b""


class _FakeProcess:
    def __init__(self, messages):
        self.returncode = None
        self.stdout = _FakeReader(messages)
        self.stderr = _FakeReader([])
        self.stdin = _FakeStdin(self)
        self.terminated = False
        self.killed = False

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def _happy_messages(final_text="你好，宝宝。"):
    return [
        {"id": 1, "result": {"userAgent": "test"}},
        {"id": 2, "result": {"thread": {"id": "thr_1"}}},
        {"id": 3, "result": {"turn": {"id": "turn_1"}}},
        {
            "method": "item/reasoning/summaryTextDelta",
            "params": {"itemId": "reason_1", "summaryIndex": 0, "delta": "先理解问题"},
        },
        {
            "method": "item/reasoning/summaryPartAdded",
            "params": {"itemId": "reason_1", "summaryIndex": 1},
        },
        {
            "method": "item/reasoning/summaryTextDelta",
            "params": {"itemId": "reason_1", "summaryIndex": 1, "delta": "再组织回答"},
        },
        {
            "method": "item/agentMessage/delta",
            "params": {"itemId": "msg_1", "delta": "你好，"},
        },
        {"method": "warning", "params": {"message": "non-fatal"}},
        {
            "method": "item/agentMessage/delta",
            "params": {"itemId": "msg_1", "delta": "宝宝。"},
        },
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "tokenUsage": {
                    "total": {
                        "inputTokens": 12,
                        "cachedInputTokens": 3,
                        "outputTokens": 8,
                        "reasoningOutputTokens": 4,
                        "totalTokens": 20,
                    }
                }
            },
        },
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "id": "msg_1", "text": final_text}},
        },
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn_1", "status": "completed", "items": []}},
        },
    ]


class CodexAppServerCommandTests(unittest.TestCase):
    def test_command_is_stdio_app_server_with_overrides(self):
        command = build_codex_app_server_command(
            "node", "codex.js", ['model_verbosity="high"']
        )

        self.assertEqual(command[:2], ["node", "codex.js"])
        self.assertEqual(command[-2:], ["app-server", "--stdio"])
        self.assertIn('-c', command)
        self.assertIn('model_verbosity="high"', command)
        self.assertNotIn("exec", command)


class CodexAppServerStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_text_reasoning_usage_and_sends_safe_params(self):
        process = _FakeProcess(_happy_messages())

        async def spawn(*_args, **_kwargs):
            return process

        events = [
            event
            async for event in stream_codex_app_server(
                ["node", "codex.js", "app-server", "--stdio"],
                env={"CODEX_HOME": "isolated"},
                cwd="workspace",
                model="gpt-5.6-sol",
                prompt="完整提示词",
                image_paths=[r"C:\images\one.png"],
                reasoning_summary="auto",
                spawn=spawn,
            )
        ]

        text_events = [event.text for event in events if event.kind == "text_delta"]
        self.assertEqual("".join(text_events), "你好，宝宝。")
        self.assertLess(len(text_events), 2)
        self.assertEqual(
            "".join(event.text for event in events if event.kind == "reasoning_delta"),
            "先理解问题\n\n再组织回答",
        )
        usage = [event.usage for event in events if event.kind == "usage"][-1]
        self.assertEqual(usage["inputTokens"], 12)
        self.assertTrue(any(event.kind == "activity" for event in events))

        sent = process.stdin.messages
        self.assertEqual([message.get("method") for message in sent[:4]], [
            "initialize", "initialized", "thread/start", "turn/start",
        ])
        thread_params = sent[2]["params"]
        self.assertEqual(thread_params["model"], "gpt-5.6-sol")
        self.assertEqual(thread_params["cwd"], "workspace")
        self.assertEqual(thread_params["approvalPolicy"], "never")
        self.assertEqual(thread_params["sandbox"], "read-only")
        self.assertTrue(thread_params["ephemeral"])
        turn_params = sent[3]["params"]
        self.assertEqual(turn_params["summary"], "auto")
        self.assertEqual(turn_params["input"][0], {"type": "text", "text": "完整提示词"})
        self.assertEqual(
            turn_params["input"][1],
            {"type": "localImage", "path": r"C:\images\one.png"},
        )
        self.assertTrue(process.stdin.closed)

    async def test_completed_snapshot_only_yields_missing_suffix(self):
        process = _FakeProcess(_happy_messages("你好，宝宝。尾巴"))

        async def spawn(*_args, **_kwargs):
            return process

        events = [
            event
            async for event in stream_codex_app_server(
                ["codex", "app-server"],
                env={},
                cwd="workspace",
                model="model",
                prompt="prompt",
                image_paths=[],
                reasoning_summary="auto",
                spawn=spawn,
            )
        ]

        text_events = [event.text for event in events if event.kind == "text_delta"]
        self.assertEqual("".join(text_events), "你好，宝宝。尾巴")
        self.assertLess(len(text_events), 3)

    async def test_failed_turn_raises_protocol_error_and_closes_process(self):
        messages = _happy_messages()[:3] + [
            {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "id": "turn_1",
                        "status": "failed",
                        "error": {"message": "quota exhausted"},
                    }
                },
            }
        ]
        process = _FakeProcess(messages)

        async def spawn(*_args, **_kwargs):
            return process

        with self.assertRaisesRegex(CodexAppServerError, "quota exhausted"):
            async for _ in stream_codex_app_server(
                ["codex", "app-server"],
                env={}, cwd="workspace", model="model", prompt="prompt",
                image_paths=[], reasoning_summary="auto", spawn=spawn,
            ):
                pass

        methods = [message.get("method") for message in process.stdin.messages]
        self.assertIn("turn/interrupt", methods)
        self.assertTrue(process.stdin.closed)


if __name__ == "__main__":
    unittest.main()
