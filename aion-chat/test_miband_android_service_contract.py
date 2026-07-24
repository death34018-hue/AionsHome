import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


class MiBandAndroidServiceContractTest(unittest.TestCase):
    def test_webview_registers_separate_miband_bridge(self):
        activity = read("AionApp/app/src/main/java/com/aion/chat/WebViewActivity.java")
        bridge = read("AionApp/app/src/main/java/com/aion/chat/AionMiBandBleBridge.java")
        self.assertIn('addJavascriptInterface(miBandBleBridge, "AionMiBand")', activity)
        for method in (
            "getStatus()", "saveConfig(String json)", "connect()",
            "manualReconnect()", "disconnect()", "syncNow()",
            "setSchedule(String json)", "startRealtime()",
            "stopRealtime()", "vibrate(String pattern)",
        ):
            self.assertIn(method, bridge)
        self.assertIn("MiBandRuntime.get(", bridge)

    def test_foreground_service_runs_adaptive_miband_scheduler(self):
        service = read("AionApp/app/src/main/java/com/aion/chat/AionPushService.java")
        self.assertIn("MiBandRuntime miBandRuntime", service)
        self.assertIn("startMiBandSyncThread();", service)
        self.assertIn("MiBandSyncSchedule.nextDelayMillis", service)
        self.assertIn("miBandRuntime.autoConnect();", service)
        self.assertIn("miBandRuntime.syncNow();", service)
        self.assertIn("new MiBandHealthUploader", service)

    def test_foreground_service_delivers_note_fields_before_ack(self):
        service = read("AionApp/app/src/main/java/com/aion/chat/AionPushService.java")
        inbox = read("AionApp/app/src/main/java/com/aion/chat/miband/MiBandCommandInbox.java")
        self.assertIn('optString("note"', service)
        self.assertIn('optString("sender_name"', service)
        self.assertIn("miBandRuntime.sendNote(", service)
        self.assertIn("command.note", service)
        self.assertIn("command.senderName", service)
        self.assertIn("final String note", inbox)
        self.assertIn("final String senderName", inbox)

    def test_uploader_posts_complete_activity_in_bounded_batches(self):
        uploader = read("AionApp/app/src/main/java/com/aion/chat/miband/MiBandHealthUploader.java")
        runtime = read("AionApp/app/src/main/java/com/aion/chat/miband/MiBandRuntime.java")
        bridge = read("AionApp/app/src/main/java/com/aion/chat/AionMiBandBleBridge.java")
        self.assertIn('"/api/health/mi-band/activity-batch"', uploader)
        self.assertIn("BATCH_SIZE = 500", uploader)
        self.assertIn('put("samples"', uploader)
        self.assertIn('put("steps"', uploader)
        self.assertIn('put("intensity"', uploader)
        self.assertIn("KEY_FULL_ACTIVITY_SYNCED", runtime)
        self.assertIn("List<MiBandProtocol.ActivitySample> samples", bridge)

    def test_existing_ble_permissions_and_ring_sources_remain(self):
        manifest = read("AionApp/app/src/main/AndroidManifest.xml")
        self.assertIn("android.permission.BLUETOOTH_SCAN", manifest)
        self.assertIn("android.permission.BLUETOOTH_CONNECT", manifest)
        self.assertIn("android.permission.FOREGROUND_SERVICE_CONNECTED_DEVICE", manifest)
        self.assertTrue((ROOT / "AionApp/app/src/main/java/com/aion/chat/AionRingBleBridge.java").exists())
        self.assertTrue((ROOT / "AionApp/app/src/main/java/com/aion/chat/RingHealthSnapshot.java").exists())


if __name__ == "__main__":
    unittest.main()
