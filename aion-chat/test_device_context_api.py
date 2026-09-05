import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import activity
from device_context import DeviceContextStore
from routes import activity as activity_routes


class DeviceContextApiSmokeTest(unittest.TestCase):
    def test_device_history_omits_context_events_but_keeps_apps_and_screen(self):
        entries = [
            {"device": "phone", "app": "com.tencent.mobileqq", "timestamp": 1},
            {"device": "phone", "app": "device_context", "kind": "phone_context",
             "slot": "motion", "timestamp": 2},
            {"device": "phone", "app": "device_context", "kind": "notification",
             "timestamp": 3},
            {"device": "phone", "app": "com.xingin.xhs", "timestamp": 4},
            {"device": "phone", "app": "screen_off", "timestamp": 5},
        ]
        app = FastAPI()
        app.include_router(activity_routes.router)
        with (
            patch.object(activity_routes, "read_recent_activity", return_value=entries),
            patch.object(activity_routes, "read_activity_logs", return_value=entries),
        ):
            client = TestClient(app)
            for url in ("/api/activity/recent", "/api/activity/logs/2026-08-30"):
                result = client.get(url).json()["entries"]
                self.assertEqual(["QQ", "小红书", "screen_off"], [e["app"] for e in result])
        self.assertEqual("com.tencent.mobileqq", entries[0]["app"])

    def test_phone_notification_status_and_removal_round_trip(self):
        original_store = activity.device_context_store
        activity.device_context_store = DeviceContextStore()
        app = FastAPI()
        app.include_router(activity_routes.router)
        now = time.time()
        phone = {"data": {
            "screen": {"value": "on", "observed_at": now, "since": now - 60},
            "posture": {"value": "face_down", "observed_at": now, "since": now - 60},
            "motion": {"value": "slight", "observed_at": now, "since": now - 60},
            "light": {"value": "dark", "observed_at": now, "since": now - 60},
            "foreground_app": {
                "value": "com.xingin.xhs",
                "observed_at": now,
                "since": now - 60,
            },
        }}
        notification = {"data": {
            "key": "smoke:delivery",
            "package_name": "com.food",
            "app_name": "外卖",
            "title": "骑手正在配送",
            "text": "预计 20 分钟送达",
            "category": "delivery",
            "posted_at": now,
        }}
        try:
            with (
                patch.object(activity, "append_activity_log"),
                patch.object(activity_routes, "cleanup_old_activity_logs"),
                patch.object(activity_routes.manager, "broadcast", new=AsyncMock()),
            ):
                client = TestClient(app)
                self.assertEqual(5, client.post(
                    "/api/device-context/phone", json=phone).json()["changed"])
                self.assertTrue(client.post(
                    "/api/device-context/notification", json=notification).json()["accepted"])

                status = client.get("/api/device-context/status").json()
                self.assertEqual("face_down", status["phone"]["posture"]["value"])
                self.assertEqual(
                    "小红书",
                    status["phone"]["foreground_app"].get("display_value"),
                )
                self.assertEqual("smoke:delivery", status["notifications"][0]["key"])
                self.assertIn("骑手正在配送", status["prompt"])

                removed = client.post(
                    "/api/device-context/notification/remove",
                    json={"key": "smoke:delivery", "observed_at": now},
                ).json()
                self.assertTrue(removed["removed"])
        finally:
            activity.device_context_store = original_store


if __name__ == "__main__":
    unittest.main()
