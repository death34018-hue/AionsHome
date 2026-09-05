import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import location
from routes import location as location_routes


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        if url.endswith("/geocode/regeo"):
            return _FakeResponse({
                "status": "1",
                "regeocode": {
                    "formatted_address": "测试地址",
                    "addressComponent": {"adcode": "110101"},
                },
            })
        if url.endswith("/weather/weatherInfo") and params["extensions"] == "base":
            return _FakeResponse({"status": "1", "lives": [{"weather": "晴"}]})
        if url.endswith("/weather/weatherInfo"):
            return _FakeResponse({
                "status": "1",
                "forecasts": [{"casts": [{"date": "2026-08-25"}]}],
            })
        if url.endswith("/place/around"):
            return _FakeResponse({"status": "1", "pois": []})
        raise AssertionError(f"unexpected URL: {url}")


class AmapUsageCounterTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_outbound_amap_request_is_counted_by_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "amap_usage.json"
            with (
                patch.object(location, "AMAP_USAGE_PATH", usage_path, create=True),
                patch.object(location.httpx, "AsyncClient", _FakeAsyncClient),
            ):
                await location.amap_regeo(116.3, 39.9, "test-key")
                await location.amap_weather("110101", "test-key")
                await location.amap_poi_search(116.3, 39.9, "050000", "test-key")

            usage = json.loads(usage_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {"regeo": 1, "weather": 2, "poi": 1, "total": 4},
                usage["counts"],
            )

    async def test_usage_file_failure_does_not_block_the_amap_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_parent_path = Path(tmp) / "missing" / "amap_usage.json"
            with (
                patch.object(location, "AMAP_USAGE_PATH", missing_parent_path),
                patch.object(location.httpx, "AsyncClient", _FakeAsyncClient),
            ):
                result = await location.amap_regeo(116.3, 39.9, "test-key")

        self.assertEqual("测试地址", result["address"])


class HeartbeatAmapPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_moving_outside_refreshes_address_without_poi_or_fresh_weather(self):
        now = time.time()
        config = {
            **location.DEFAULT_LOCATION_CONFIG,
            "amap_key": "test-key",
            "enabled": True,
            "home_lng": 116.0,
            "home_lat": 39.9,
            "movement_threshold": 500,
            "quiet_hours_enabled": False,
        }
        status = {
            **location.DEFAULT_LOCATION_STATUS,
            "state": "outside",
            "lng": 116.3,
            "lat": 39.9,
            "last_api_lng": 116.3,
            "last_api_lat": 39.9,
            "address": "旧地址",
            "adcode": "110101",
            "weather": {"weather": "晴"},
            "forecast": [{"date": "2026-08-25"}],
            "weather_fetched_at": now - 60,
            "updated_at": now - 600,
        }

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "location_config.json"
            status_path = Path(tmp) / "location_status.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            status_path.write_text(json.dumps(status), encoding="utf-8")

            async def fail_weather(*args, **kwargs):
                raise AssertionError("fresh cached weather must not be queried")

            async def fail_poi(*args, **kwargs):
                raise AssertionError("automatic heartbeats must not query POIs")

            with (
                patch.object(location, "LOCATION_CONFIG_PATH", config_path),
                patch.object(location, "LOCATION_STATUS_PATH", status_path),
                patch.object(location, "amap_regeo", new=AsyncMock(return_value={
                    "address": "新地址",
                    "adcode": "110101",
                })),
                patch.object(location, "amap_weather", new=fail_weather),
                patch.object(location, "amap_poi_search", new=fail_poi),
                patch.object(location.manager, "broadcast", new=AsyncMock()),
            ):
                result = await location.process_heartbeat(
                    116.31,
                    39.9,
                    is_gcj02=True,
                    skip_sentinel=True,
                )

            saved = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertTrue(result["full_api"])
            self.assertEqual("新地址", saved["address"])
            self.assertEqual({"weather": "晴"}, saved["weather"])
            self.assertEqual([], saved["nearby_pois"].get("餐饮美食", []))
            self.assertEqual(status["weather_fetched_at"], saved["weather_fetched_at"])

    async def test_weather_refreshes_after_one_hour_without_a_poi_search(self):
        now = time.time()
        config = {
            **location.DEFAULT_LOCATION_CONFIG,
            "amap_key": "test-key",
            "enabled": True,
            "home_lng": 116.0,
            "home_lat": 39.9,
            "quiet_hours_enabled": False,
        }
        status = {
            **location.DEFAULT_LOCATION_STATUS,
            "state": "outside",
            "lng": 116.3,
            "lat": 39.9,
            "last_api_lng": 116.3,
            "last_api_lat": 39.9,
            "address": "测试地址",
            "adcode": "110101",
            "weather": {"weather": "雨"},
            "forecast": [],
            "weather_fetched_at": now - 61 * 60,
            "updated_at": now - 600,
        }

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "location_config.json"
            status_path = Path(tmp) / "location_status.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            status_path.write_text(json.dumps(status), encoding="utf-8")

            async def fail_poi(*args, **kwargs):
                raise AssertionError("weather refresh must not query POIs")

            with (
                patch.object(location, "LOCATION_CONFIG_PATH", config_path),
                patch.object(location, "LOCATION_STATUS_PATH", status_path),
                patch.object(location, "amap_weather", new=AsyncMock(return_value={
                    "live": {"weather": "晴"},
                    "forecast": [],
                })),
                patch.object(location, "amap_poi_search", new=fail_poi),
                patch.object(location.manager, "broadcast", new=AsyncMock()),
            ):
                await location.process_heartbeat(
                    116.3,
                    39.9,
                    is_gcj02=True,
                    skip_sentinel=True,
                )

            saved = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual({"weather": "晴"}, saved["weather"])
            self.assertGreater(saved["weather_fetched_at"], status["weather_fetched_at"])


class LocationConfigPrivacyTest(unittest.TestCase):
    def test_config_endpoint_never_returns_the_raw_amap_key(self):
        app = FastAPI()
        app.include_router(location_routes.router)
        with (
            patch.object(location_routes, "load_location_config", return_value={
                **location.DEFAULT_LOCATION_CONFIG,
                "amap_key": "1234567890abcdef",
            }),
            patch.object(location_routes, "load_amap_usage", return_value={
                "date": "2026-08-25",
                "counts": {"regeo": 1, "weather": 2, "poi": 3, "total": 6},
            }, create=True),
        ):
            payload = TestClient(app).get("/api/location/config").json()

        self.assertEqual("", payload["amap_key"])
        self.assertEqual("1234********cdef", payload["amap_key_masked"])
        self.assertTrue(payload["amap_key_configured"])
        self.assertEqual(6, payload["amap_usage_today"]["counts"]["total"])
        self.assertNotIn("1234567890abcdef", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
