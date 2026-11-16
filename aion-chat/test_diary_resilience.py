import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ai_providers import (
    MODEL_RAW_RESPONSE_RETENTION_SECONDS,
    _cleanup_model_raw_responses,
    _save_model_raw_response,
)
from diary import diary_response_error, parse_diary_payload
from memory import _generate_digest_diary


class DiaryResilienceTests(unittest.TestCase):
    def test_parse_diary_payload_accepts_plain_text_wrapped_json(self):
        raw = '好的，结果如下：\n{"diary":{"title":"标题","content":"正文","mood":"平静"},"post_moment":false}\n以上。'
        payload = parse_diary_payload(raw)
        self.assertEqual(payload["diary"]["content"], "正文")

    def test_parse_diary_payload_repairs_unescaped_quotes_in_content(self):
        raw = '''```json
{"diary":{"title":"四个小时","content":"她说自己"能控制"，后来决定"不改了去睡觉"。","mood":"担心"},"post_moment":true,"moment":{"content":"记录在案。","expect_reply":false}}
```'''
        payload = parse_diary_payload(raw)
        self.assertEqual(payload["diary"]["title"], "四个小时")
        self.assertIn('"能控制"', payload["diary"]["content"])
        self.assertTrue(payload["post_moment"])
        self.assertEqual(payload["moment"]["content"], "记录在案。")

    def test_parse_diary_payload_rejects_nested_moment_fragment(self):
        raw = '{"content":"只有朋友圈残片","expect_reply":false}'
        self.assertIsNone(parse_diary_payload(raw))

    def test_parse_diary_payload_repairs_quoted_text_followed_by_comma(self):
        raw = '{"diary":{"title":"测试","content":"她说"先睡吧", then stopped working.","mood":"放心"},"post_moment":false}'
        payload = parse_diary_payload(raw)
        self.assertEqual(payload["diary"]["content"], '她说"先睡吧", then stopped working.')

    def test_diary_response_error_detects_provider_error_text(self):
        self.assertIn("限流", diary_response_error("[硅基流动错误 429] 请求限流"))
        self.assertEqual(diary_response_error(""), "模型返回为空")

    def test_raw_response_log_is_written_and_old_files_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            now = time.time()
            old = log_dir / "old.json"
            old.write_text("{}", encoding="utf-8")
            old_time = now - MODEL_RAW_RESPONSE_RETENTION_SECONDS - 10
            old.touch()
            import os
            os.utime(old, (old_time, old_time))

            path = _save_model_raw_response(
                messages=[{"role": "user", "content": "test"}],
                model_key="硅基GLM-5.2",
                trace_label="unit_test",
                raw_response="raw-server-output",
                filtered_response="raw-server-output",
                started_at=now - 1,
                finished_at=now,
                log_dir=log_dir,
            )

            self.assertIsNotNone(path)
            self.assertFalse(old.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["raw_response"], "raw-server-output")
            self.assertEqual(data["trace_label"], "unit_test")
            self.assertEqual(_cleanup_model_raw_responses(log_dir, now), 0)


class DigestDiaryCircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_skipping_diary_preserves_other_decisions(self):
        decision = {
            "diary": None, "post_moment": True, "moment": {"content": "值得分享"},
            "givegift": True, "gift": {"image_prompt": "a bookmark", "message": "送给你"},
        }
        model = AsyncMock(return_value=json.dumps(decision))
        with patch("ai_providers.simple_ai_call", model):
            payload, status = await _generate_digest_diary([], "primary-model")

        self.assertTrue(status["ok"])
        self.assertEqual(payload, decision)
        model.assert_awaited_once()

    async def test_failure_calls_model_only_once(self):
        calls = []

        async def fake_call(messages, model_key, temperature=None, *, trace_label=""):
            calls.append((model_key, trace_label))
            return "not-json"

        with patch("ai_providers.simple_ai_call", new=fake_call):
            payload, status = await _generate_digest_diary(
                [{"role": "user", "content": "write diary"}],
                "primary-model",
            )

        self.assertIsNone(payload)
        self.assertFalse(status["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "primary-model")

if __name__ == "__main__":
    unittest.main()
