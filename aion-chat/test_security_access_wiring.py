import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class SecurityAccessBackendWiringTests(unittest.TestCase):
    def test_app_registers_isolated_middleware_routes_and_existing_entrypoints(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")

        self.assertEqual(1, source.count("app.add_middleware(SecurityAccessMiddleware"))
        self.assertEqual(1, source.count("create_security_access_router(security_access_service)"))
        self.assertIn("security_access_service.start(manager.broadcast)", source)
        self.assertIn("await security_access_service.stop()", source)
        self.assertIn('@app.get("/")', source)
        self.assertIn('@app.websocket("/ws")', source)


class SecurityAccessFrontendWiringTests(unittest.TestCase):
    def test_existing_websocket_scripts_only_lazy_load_and_forward_security_alerts(self):
        for relative in ("static/common.js", "static/chat.js"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(relative=relative):
                self.assertIn("import('/static/security-alert.js')", source)
                self.assertIn("msg.type === 'security_alert'", source)
                self.assertIn("handleMessage(msg)", source)
                self.assertNotIn("strangealert.mp3", source)
                self.assertNotIn("securityAlertOverlay", source)

    def test_security_module_is_separate_from_monitor_and_android_features(self):
        sources = (
            (ROOT / "security_access.py").read_text(encoding="utf-8"),
            (ROOT / "routes" / "security_access.py").read_text(encoding="utf-8"),
            (ROOT / "static" / "security-alert.js").read_text(encoding="utf-8"),
        )

        for source in sources:
            self.assertNotIn("monitor_alert", source)
            self.assertNotIn("AionApp", source)


if __name__ == "__main__":
    unittest.main()
