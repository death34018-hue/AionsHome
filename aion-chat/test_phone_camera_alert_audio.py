import unittest

import camera


class PhoneCameraAlertPayloadTests(unittest.TestCase):
    def setUp(self):
        self.previous_source = camera.cam.cfg.get("active_source", "local")

    def tearDown(self):
        camera.cam.cfg["active_source"] = self.previous_source

    def test_phone_source_marks_native_capture(self):
        camera.cam.cfg["active_source"] = "phone"

        payload = camera.build_monitor_alert_data(
            "check",
            origin="connor",
        )

        self.assertEqual("check", payload["content"])
        self.assertEqual("connor", payload["origin"])
        self.assertTrue(payload["phone_camera_native_capture"])

    def test_local_source_does_not_mark_native_capture(self):
        camera.cam.cfg["active_source"] = "local"

        payload = camera.build_monitor_alert_data("check")

        self.assertNotIn("phone_camera_native_capture", payload)


if __name__ == "__main__":
    unittest.main()
