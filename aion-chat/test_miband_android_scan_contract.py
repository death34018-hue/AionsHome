import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
JAVA = ROOT / "AionApp" / "app" / "src" / "main" / "java" / "com" / "aion" / "chat"


class MiBandAndroidScanContractTest(unittest.TestCase):
    def test_scanner_uses_android_ble_and_stops_after_eight_seconds(self):
        source = (JAVA / "miband" / "MiBandScanner.java").read_text(encoding="utf-8")
        self.assertIn("BluetoothLeScanner", source)
        self.assertIn("ScanCallback", source)
        self.assertIn("8_000L", source)
        self.assertIn("MiBandScanResult.rankComparator()", source)

    def test_bridge_exposes_scan_stop_and_select(self):
        source = (JAVA / "AionMiBandBleBridge.java").read_text(encoding="utf-8")
        self.assertIn("void startScan()", source)
        self.assertIn("void stopScan()", source)
        self.assertIn("void selectDevice(String json)", source)
        self.assertIn('callJs("onScanState"', source)
        self.assertIn('callJs("onScanResults"', source)

    def test_selection_saves_then_connects_without_exposing_key(self):
        source = (JAVA / "AionMiBandBleBridge.java").read_text(encoding="utf-8")
        selection = source[source.index("void selectDevice(String json)"):]
        self.assertLess(selection.index("runtime.saveSelectedDevice("), selection.index("runtime.connectSaved();"))
        status = source[source.index("private JSONObject statusJson"):]
        self.assertNotIn('out.put("auth_key"', status)


if __name__ == "__main__":
    unittest.main()
