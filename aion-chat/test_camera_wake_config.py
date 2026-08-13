import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from routes import cam as cam_routes


class CameraWakeModeConfigTests(unittest.TestCase):
    def test_legacy_camera_config_defaults_desktop_capture_to_off(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "cam_config.json"
            config_path.write_text(
                json.dumps({"camera_index": 3}),
                encoding="utf-8",
            )
            with patch.object(config, "CAM_CONFIG_PATH", config_path):
                loaded = config.load_cam_config()

        self.assertIs(False, loaded["include_pc_screen"])

    def test_missing_mode_defaults_to_aion(self):
        self.assertEqual("aion", config.normalize_camera_wake_mode(None))

    def test_invalid_mode_defaults_to_aion(self):
        self.assertEqual("aion", config.normalize_camera_wake_mode("roulette"))

    def test_supported_modes_are_preserved(self):
        for mode in ("aion", "connor", "smart"):
            with self.subTest(mode=mode):
                self.assertEqual(mode, config.normalize_camera_wake_mode(mode))


class CameraWakeModeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_exposes_normalized_mode_and_dynamic_actor_names(self):
        original = dict(cam_routes.cam.cfg)
        try:
            cam_routes.cam.cfg["wake_mode"] = "smart"
            with patch(
                "chatroom.get_chatroom_names",
                return_value=("Configured User", "Configured Main", "Configured Second"),
            ):
                status = await cam_routes.cam_status()
        finally:
            cam_routes.cam.cfg.clear()
            cam_routes.cam.cfg.update(original)

        self.assertEqual("smart", status["wake_mode"])
        self.assertEqual("Configured Main", status["ai_name"])
        self.assertEqual("Configured Second", status["connor_name"])

    async def test_update_normalizes_and_saves_wake_mode(self):
        original = dict(cam_routes.cam.cfg)
        try:
            with patch.object(cam_routes, "save_cam_config") as save:
                response = await cam_routes.update_cam_config(
                    cam_routes.CamConfigUpdate(wake_mode="roulette")
                )
                saved = dict(cam_routes.cam.cfg)
        finally:
            cam_routes.cam.cfg.clear()
            cam_routes.cam.cfg.update(original)

        self.assertTrue(response["ok"])
        self.assertEqual("aion", saved["wake_mode"])
        save.assert_called_once()

    async def test_status_exposes_desktop_capture_preference(self):
        original = dict(cam_routes.cam.cfg)
        try:
            cam_routes.cam.cfg["include_pc_screen"] = True
            with patch(
                "chatroom.get_chatroom_names",
                return_value=("Configured User", "Configured Main", "Configured Second"),
            ):
                status = await cam_routes.cam_status()
        finally:
            cam_routes.cam.cfg.clear()
            cam_routes.cam.cfg.update(original)

        self.assertIs(True, status["include_pc_screen"])

    async def test_update_persists_desktop_capture_preference(self):
        original = dict(cam_routes.cam.cfg)
        try:
            with patch.object(cam_routes, "save_cam_config") as save:
                response = await cam_routes.update_cam_config(
                    cam_routes.CamConfigUpdate(include_pc_screen=True)
                )
                saved = dict(cam_routes.cam.cfg)
        finally:
            cam_routes.cam.cfg.clear()
            cam_routes.cam.cfg.update(original)

        self.assertTrue(response["ok"])
        self.assertIs(True, saved["include_pc_screen"])
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
