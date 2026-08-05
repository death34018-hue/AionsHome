import unittest
from pathlib import Path

import camera


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent


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

    def test_all_monitor_alert_producers_use_payload_helper(self):
        sources = {
            "camera": (ROOT / "camera.py").read_text(encoding="utf-8"),
            "schedule": (ROOT / "schedule.py").read_text(encoding="utf-8"),
            "chatroom": (ROOT / "routes" / "chatroom.py").read_text(encoding="utf-8"),
        }

        self.assertGreaterEqual(
            sources["camera"].count("build_monitor_alert_data("),
            2,
        )
        self.assertIn(
            "build_monitor_alert_data(",
            sources["schedule"],
        )
        self.assertIn(
            'build_monitor_alert_data("监控查看")',
            sources["chatroom"],
        )


class PhoneCameraAlertScriptTests(unittest.TestCase):
    def test_shipped_scripts_skip_html_audio_for_native_phone_capture(self):
        paths = (
            ROOT / "static" / "chat.js",
            ROOT / "static" / "common.js",
            PROJECT_ROOT / "AionApp" / "app" / "src" / "main"
            / "assets" / "static" / "common.js",
        )

        for path in paths:
            source = path.read_text(encoding="utf-8")
            alert_index = source.index('type === "monitor_alert"')
            audio_index = source.index("AionMonitoralart.mp3", alert_index)
            guarded = source[alert_index:audio_index]
            with self.subTest(path=path):
                self.assertIn("!data.phone_camera_native_capture", guarded)


class AndroidPhoneCameraAlertWiringTests(unittest.TestCase):
    def test_native_audio_and_shared_capture_target_are_wired_after_deduplication(self):
        path = (
            PROJECT_ROOT / "AionApp" / "app" / "src" / "main" / "java"
            / "com" / "aion" / "chat" / "AionPushService.java"
        )
        source = path.read_text(encoding="utf-8")
        switch_start = source.index('case "phone_camera_capture"')
        switch_end = source.index("break;", switch_start)
        phone_camera_switch_case = source[switch_start:switch_end]
        dispatch_start = source.index("private void dispatchPhoneCameraCapture")
        dispatch_end = source.index(
            "private void attemptPhoneCameraCapture",
            dispatch_start,
        )
        dispatch = source[dispatch_start:dispatch_end]

        self.assertIn(
            "PHONE_CAMERA_SHUTTER_OFFSET_MS = 5_000L",
            source,
        )
        self.assertIn("captureTargetElapsedMs", dispatch)
        self.assertIn("startPhoneCameraAlert", dispatch)
        self.assertIn("schedulePhoneScreenSnapshotAt", dispatch)
        self.assertLess(
            dispatch.index("PhoneCameraState.Decision.ACCEPTED"),
            dispatch.index("startPhoneCameraAlert"),
        )
        self.assertNotIn(
            'schedulePhoneScreenCapture("phone_camera_capture");',
            phone_camera_switch_case,
        )

    def test_native_monitor_alert_skips_default_audio_and_old_screen_timer(self):
        path = (
            PROJECT_ROOT / "AionApp" / "app" / "src" / "main" / "java"
            / "com" / "aion" / "chat" / "AionPushService.java"
        )
        source = path.read_text(encoding="utf-8")
        case_start = source.index('case "monitor_alert"')
        case_end = source.index("break;", case_start)
        monitor_case = source[case_start:case_end]

        self.assertIn("phone_camera_native_capture", monitor_case)
        self.assertIn("showNotif", monitor_case)
        self.assertIn("!nativePhoneCapture", monitor_case)


if __name__ == "__main__":
    unittest.main()
