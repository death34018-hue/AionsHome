import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import activity
from device_context import DeviceContextStore
from routes import activity as activity_routes


class DeviceContextApiSmokeTest(unittest.TestCase):
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
