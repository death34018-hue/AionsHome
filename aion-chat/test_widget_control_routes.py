import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import capabilities
from routes import widget_control as routes
from widget_control import WidgetAssetCatalog, WidgetControlStore


class WidgetControlRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "小组件"
        states = root / "状态"
        states.mkdir(parents=True)
        (states / "Main-平静.png").write_bytes(b"main-calm")
        (states / "Main-困倦.png").write_bytes(b"main-sleepy")
        (states / "Second-平静.png").write_bytes(b"second-calm")
        (root / "横幅.png").write_bytes(b"banner-image")
        self.catalog = WidgetAssetCatalog(
            root, lambda: {"aion": "Main", "connor": "Second"}
        )
        self.store = WidgetControlStore(Path(self.tmp.name) / "state.json", self.catalog)
        self.catalog_patch = patch.object(routes, "catalog", self.catalog)
        self.store_patch = patch.object(routes, "store", self.store)
        self.broadcast_patch = patch.object(routes, "broadcast_state", new=AsyncMock())
        self.catalog_patch.start()
        self.store_patch.start()
        self.broadcast = self.broadcast_patch.start()
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.broadcast_patch.stop()
        self.store_patch.stop()
        self.catalog_patch.stop()
        self.tmp.cleanup()

    def test_state_payload_has_actor_catalog_and_selected_asset(self):
        response = self.client.get("/api/widget-control/state")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["actors"]["aion"]["states"], ["困倦", "平静"])
        self.assertEqual(payload["actors"]["aion"]["current_state"], "平静")
        self.assertIn("/api/widget-control/assets/aion/", payload["actors"]["aion"]["asset"]["url"])
        self.assertEqual(payload["banner_asset"]["url"], "/api/widget-control/banner/image")
        self.assertTrue(payload["banner_asset"]["version"])

    def test_actor_update_validates_ownership_catalog(self):
        ok = self.client.patch(
            "/api/widget-control/actors/aion", json={"state": "困倦"}
        )
        missing = self.client.patch(
            "/api/widget-control/actors/connor", json={"state": "困倦"}
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["actors"]["aion"]["current_state"], "困倦")
        self.assertEqual(missing.status_code, 404)

    def test_asset_route_only_serves_catalog_entries(self):
        ok = self.client.get("/api/widget-control/assets/aion/%E5%B9%B3%E9%9D%99")
        escaped = self.client.get("/api/widget-control/assets/aion/..%2F..%2Fsecret")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.content, b"main-calm")
        self.assertEqual(escaped.status_code, 404)

    def test_banner_image_route_serves_current_source_file(self):
        response = self.client.get("/api/widget-control/banner/image")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"banner-image")

    def test_clear_banner_keeps_actor_states(self):
        asyncio.run(self.store.show_banner("aion", "醒目内容"))
        response = self.client.post("/api/widget-control/banner/clear")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["banner"]["content"], "")
        self.assertEqual(response.json()["actors"]["aion"]["current_state"], "平静")


class WidgetControlCapabilityTests(unittest.TestCase):
    def test_capability_toggle_only_controls_prompt_injection(self):
        with patch.object(capabilities, "is_capability_enabled", return_value=True), \
             patch("widget_control.build_widget_control_prompt", return_value="widget prompt"):
            enabled = asyncio.run(capabilities.build_capability_prompt_items("User", who="aion"))
        with patch.object(capabilities, "is_capability_enabled", return_value=False), \
             patch("widget_control.build_widget_control_prompt", return_value="widget prompt"):
            disabled = asyncio.run(capabilities.build_capability_prompt_items("User", who="aion"))

        self.assertIn("widget prompt", enabled)
        self.assertNotIn("widget prompt", disabled)


if __name__ == "__main__":
    unittest.main()
