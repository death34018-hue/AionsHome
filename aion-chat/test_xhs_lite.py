"""Offline regression checks; never publish comments or use saved credentials."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import xhs_lite


class XhsReadFailureTests(unittest.IsolatedAsyncioTestCase):
    async def worker_result(self, endpoint, data):
        proc = SimpleNamespace(communicate=AsyncMock(return_value=(
            json.dumps({"ok": True, "status": 200, "data": data}).encode(), b"",
        )))
        with patch.object(xhs_lite.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)):
            return await xhs_lite._run_worker(endpoint, cookie="a1=offline")

    async def test_expired_login_reports_cookie_renewal(self):
        with self.assertRaisesRegex(RuntimeError, "登录已过期.*Cookie"):
            await self.worker_result("check-login", {
                "logged_in": False,
                "raw": {"success": False, "code": -100, "msg": "登录已过期"},
            })

    async def test_failed_read_is_not_returned_as_an_empty_feed(self):
        with self.assertRaisesRegex(RuntimeError, "user-profile.*-1.*请求被拒绝"):
            await self.worker_result("user-profile", {
                "success": False, "feeds": [],
                "raw_error": {"success": False, "code": -1, "msg": "请求被拒绝"},
            })

    async def test_successful_empty_feed_remains_a_valid_result(self):
        result = await self.worker_result("user-profile", {"success": True, "feeds": []})
        self.assertEqual(result["feeds"], [])

    async def test_login_service_rejection_keeps_its_real_reason(self):
        with self.assertRaisesRegex(RuntimeError, "check-login.*-1.*请求被拒绝"):
            await self.worker_result("check-login", {
                "logged_in": False,
                "raw": {"success": False, "code": -1, "msg": "请求被拒绝"},
            })

    async def test_profile_failure_still_allows_search_fallback_after_valid_login(self):
        cfg = {"enabled": True, "cookie": "a1=offline", "target_user_id": "target"}
        async def worker(endpoint, *args, **kwargs):
            if endpoint == "check-login":
                return {"logged_in": True}
            if endpoint == "user-profile":
                raise RuntimeError("profile unavailable")
            if endpoint == "search":
                return {"success": True, "feeds": [{"id": "post", "author_id": "target"}]}
            if endpoint == "get-feed-detail":
                raise RuntimeError("detail reached")
            self.fail(f"Unexpected external operation: {endpoint}")

        with patch.object(xhs_lite, "load_config", return_value=cfg), \
             patch.object(xhs_lite, "actor_label", return_value="Test actor"), \
             patch.object(xhs_lite, "_run_worker", side_effect=worker):
            with self.assertRaisesRegex(RuntimeError, "detail reached"):
                await xhs_lite.run_actor_roam("connor", manual=True)

    async def test_invalid_login_stops_roam_before_reading_posts(self):
        cfg = {"enabled": True, "cookie": "a1=offline", "target_user_id": "target"}
        async def worker(endpoint, *args, **kwargs):
            if endpoint == "check-login":
                raise RuntimeError("登录已过期，请更新 Cookie")
            self.fail("An expired login must stop before reading posts or generating comments")

        with patch.object(xhs_lite, "load_config", return_value=cfg), \
             patch.object(xhs_lite, "actor_label", return_value="Test actor"), \
             patch.object(xhs_lite, "_run_worker", side_effect=worker):
            with self.assertRaisesRegex(RuntimeError, "登录已过期"):
                await xhs_lite.run_actor_roam("connor", manual=True)


if __name__ == "__main__":
    unittest.main()
