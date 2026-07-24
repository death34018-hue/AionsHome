import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class MiBandPersistentConnectionContractTest(unittest.TestCase):
    def test_runtime_owns_one_connection_and_persists_backoff(self):
        source = (ROOT / "AionApp/app/src/main/java/com/aion/chat/miband/MiBandRuntime.java").read_text(encoding="utf-8")
        preferences = (ROOT / "AionApp/app/src/main/java/com/aion/chat/miband/MiBandPreferences.java").read_text(encoding="utf-8")
        service = (ROOT / "AionApp/app/src/main/java/com/aion/chat/AionPushService.java").read_text(encoding="utf-8")

        self.assertIn("AtomicBoolean connectionQueued", source)
        self.assertIn("void autoConnect()", source)
        self.assertIn("KEY_RECONNECT_FAILURES", preferences)
        self.assertIn("KEY_NEXT_RECONNECT_AT", preferences)
        self.assertIn("miBandRuntime.autoConnect();", service)
        self.assertNotIn("miBandRuntime.connectSaved();", service)

    def test_busy_writes_are_retried_and_webview_callbacks_reach_iframe(self):
        session = (ROOT / "AionApp/app/src/main/java/com/aion/chat/miband/MiBandGattSession.java").read_text(encoding="utf-8")
        bridge = (ROOT / "AionApp/app/src/main/java/com/aion/chat/AionMiBandBleBridge.java").read_text(encoding="utf-8")

        self.assertIn("MiBandGattWritePolicy.shouldRetry", session)
        self.assertIn("CountDownLatch characteristicWriteLatch", session)
        self.assertIn("onCharacteristicWrite", session)
        self.assertIn("requestMtu(247)", session)
        self.assertIn("onMtuChanged", session)
        self.assertIn("querySelectorAll('iframe')", bridge)
        self.assertIn("contentWindow", bridge)


if __name__ == "__main__":
    unittest.main()
