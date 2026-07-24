import pathlib
import unittest


HTML_PATH = pathlib.Path(__file__).resolve().parent / "static" / "health.html"


class HealthMiBandUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML_PATH.read_text(encoding="utf-8")

    def test_wearable_tab_and_four_minimal_sections_exist(self):
        self.assertIn('onclick="switchHealthTab(\'ring\')">小米手环</button>', self.html)
        for section in (
            'id="miBandConnectionSection"',
            'id="miBandScheduleSection"',
            'id="miBandRealtimeSection"',
            'id="miBandDataSection"',
        ):
            self.assertIn(section, self.html)

    def test_default_schedule_and_manual_reconnect_controls_exist(self):
        self.assertIn('id="miBandDayInterval"', self.html)
        self.assertIn('<option value="1" selected>1 分钟</option>', self.html)
        self.assertIn('id="miBandNightStart" type="time" value="02:00"', self.html)
        self.assertIn('id="miBandNightEnd" type="time" value="08:00"', self.html)
        self.assertIn('id="miBandNightInterval"', self.html)
        self.assertIn('<option value="20" selected>20 分钟</option>', self.html)
        self.assertIn('id="miBandReconnectButton"', self.html)

    def test_advanced_setup_uses_key_and_scan_without_editable_mac(self):
        start = self.html.index('<details id="miBandAdvanced"')
        end = self.html.index('</details>', start)
        advanced = self.html[start:end]
        for item in (
            'id="miBandAuthKey"', 'id="miBandScanButton"', 'id="miBandScanResults"',
            'id="miBandReselectButton"', 'id="miBandLog"',
            'id="miBandVibrateSingle"', 'id="miBandVibrateCall"',
        ):
            self.assertIn(item, advanced)
        self.assertNotIn('id="miBandAddress"', advanced)

    def test_legacy_ring_ui_is_hidden_but_not_deleted(self):
        self.assertIn('class="section legacy-ring-section"', self.html)
        self.assertIn('.legacy-ring-section { display: none !important; }', self.html)
        self.assertIn('id="btnConnect"', self.html)
        self.assertIn('function syncRingHistory', self.html)

    def test_native_bridge_callbacks_and_commands_are_wired(self):
        self.assertIn('window.miBandNative = {', self.html)
        for command in (
            'window.AionMiBand.connect()',
            'window.AionMiBand.manualReconnect()',
            'window.AionMiBand.syncNow()',
            'window.AionMiBand.startRealtime()',
            'window.AionMiBand.stopRealtime()',
            'window.AionMiBand.setSchedule(',
            'window.AionMiBand.startScan()',
            'window.AionMiBand.stopScan()',
            'window.AionMiBand.selectDevice(',
        ):
            self.assertIn(command, self.html)
        self.assertIn('onScanState(payload)', self.html)
        self.assertIn('onScanResults(payload)', self.html)

    def test_full_miband_summary_is_source_specific_and_visible(self):
        for item in (
            'id="miBandTodaySteps"',
            'id="miBandActivityMinutes"',
            'id="miBandSleepTotal"',
            'id="miBandSleepRange"',
            'id="miBandSleepMeta"',
            'function renderMiBandSummary(summary)',
            "healthState.miBand = msg.data",
            "miBand: data.miBand || null",
        ):
            self.assertIn(item, self.html)


if __name__ == "__main__":
    unittest.main()
