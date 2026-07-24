import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


class MiBandAndroidRuntimeContractTest(unittest.TestCase):
    def test_runtime_has_one_process_wide_gatt_owner_and_serial_executor(self):
        source = read("AionApp/app/src/main/java/com/aion/chat/miband/MiBandRuntime.java")
        self.assertIn("static volatile MiBandRuntime instance", source)
        self.assertIn("MiBandRuntime get(Context context)", source)
        self.assertIn("Executors.newSingleThreadExecutor", source)
        self.assertEqual(1, source.count("new MiBandGattSession("))

    def test_runtime_exposes_required_manual_and_background_operations(self):
        source = read("AionApp/app/src/main/java/com/aion/chat/miband/MiBandRuntime.java")
        for signature in (
            "void connectSaved(",
            "void manualReconnect(",
            "void disconnect(",
            "void syncNow(",
            "void startRealtime(",
            "void stopRealtime(",
            "void vibrate(",
            "void sendNote(",
        ):
            self.assertIn(signature, source)
        self.assertIn("MiBandSyncSchedule.reconnectDelayMillis", source)

    def test_gatt_session_uses_only_verified_protocol_channels(self):
        source = read("AionApp/app/src/main/java/com/aion/chat/miband/MiBandGattSession.java")
        for symbol in (
            "CHUNKED_WRITE_UUID",
            "CHUNKED_READ_UUID",
            "FETCH_METADATA_UUID",
            "FETCH_DATA_UUID",
            "HEART_RATE_MEASUREMENT_UUID",
            "BATTERY_LEVEL_UUID",
        ):
            self.assertIn("MiBandProtocol." + symbol, source)
        self.assertIn("MiBandCrypto.buildAuthResponse", source)
        self.assertIn("HEART_RATE_MODE_CONTINUE", source)
        self.assertIn("FIND_DEVICE_ENDPOINT", source)
        self.assertIn("NOTIFICATION_ENDPOINT", source)
        self.assertIn("buildNotification", source)

    def test_runtime_never_logs_or_returns_the_auth_key(self):
        runtime = read("AionApp/app/src/main/java/com/aion/chat/miband/MiBandRuntime.java")
        session = read("AionApp/app/src/main/java/com/aion/chat/miband/MiBandGattSession.java")
        self.assertNotRegex(runtime, r"Log\.[a-zA-Z]+\([^\n]*authKey")
        self.assertNotRegex(session, r"Log\.[a-zA-Z]+\([^\n]*authKey")
        self.assertNotIn('put("auth_key"', runtime)


if __name__ == "__main__":
    unittest.main()
