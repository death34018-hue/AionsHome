import unittest
from unittest.mock import AsyncMock

import agy_usage
from codex_app_server import codex_quota_groups


class UsageTests(unittest.IsolatedAsyncioTestCase):
    def test_codex_reads_account_windows_without_counting_model_specific_limits(self):
        shared = {"limitId": "codex",
                  "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1893456000},
                  "secondary": {"usedPercent": 27, "windowDurationMins": 10080, "resetsAt": None}}
        other = {"limitId": "other", "primary": {"usedPercent": 100, "windowDurationMins": 60}}
        report = {"rateLimits": other, "rateLimitsByLimitId": {"codex": shared, "other": other}}
        group, = codex_quota_groups(report)
        short, weekly = group["buckets"]
        self.assertEqual((short["window"], short["remaining"]), ("5h", 88))
        self.assertEqual(short["reset_at"], "2030-01-01T00:00:00+00:00")
        self.assertEqual((weekly["window"], weekly["remaining"], weekly["reset_at"]), ("weekly", 73, None))
        self.assertEqual(codex_quota_groups({"rateLimits": shared}), [group])
        self.assertEqual(codex_quota_groups({"rateLimits": {"primary": {"usedPercent": 0}}})
                         [0]["buckets"][0]["remaining"], 100)
        with self.assertRaises(ValueError):
            codex_quota_groups({"rateLimitsByLimitId": {"other": other}, "rateLimits": other})

    def test_read_official_buckets_with_full_precision_and_optional_reset(self):
        report = {"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": [
            {"name": "Claude and GPT models", "buckets": [
                {"name": "Weekly Limit Remaining", "window": "weekly",
                 "remaining_fraction": 0.3664168119, "reset_time": "2026-11-17T10:18:00Z"},
                {"name": "Five Hour Limit Remaining", "window": "5h", "remaining_fraction": 1},
            ]},
        ]}}}
        group, = agy_usage.parse_report(report)
        weekly, short = group["buckets"]
        self.assertEqual(group["name"], "Claude and GPT models")
        self.assertAlmostEqual(weekly["remaining"], 36.64168119)
        self.assertEqual(weekly["reset_at"], "2026-11-17T10:18:00Z")
        self.assertEqual(short["remaining"], 100)
        self.assertIsNone(short["reset_at"])
        with self.assertRaises(agy_usage.UsageError):
            agy_usage.parse_report({"status": "SUCCESS", "response": "98%"})

    async def test_poll_failure_retains_values_and_recovers_on_the_next_check(self):
        initial = [{"name": "Gemini", "buckets": []}]
        updated = [{"name": "Gemini 4 Argon", "buckets": []}]
        query = AsyncMock(side_effect=[initial, agy_usage.UsageError("查询失败"), updated])
        monitor = agy_usage.UsageMonitor(query, "AGY")
        await monitor.refresh()
        timestamp = monitor.snapshot["updated_at"]
        await monitor.refresh()
        self.assertEqual(monitor.snapshot["groups"], initial)
        self.assertEqual(monitor.snapshot["updated_at"], timestamp)
        self.assertEqual(monitor.snapshot["error"], "查询失败")
        await monitor.refresh()
        self.assertEqual(monitor.snapshot["groups"], updated)
        self.assertIsNone(monitor.snapshot["error"])
