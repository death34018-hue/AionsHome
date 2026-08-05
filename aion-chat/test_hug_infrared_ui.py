import asyncio
import importlib
import json
import pathlib
import subprocess
import sys
import unittest
from html.parser import HTMLParser


BASE_DIR = pathlib.Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


class ButtonParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.commands = []
        self._current_command = None
        self._current_text = []
        self.labels = {}

    def handle_starttag(self, tag, attrs):
        if tag != "button":
            return
        values = dict(attrs)
        self._current_command = values.get("data-command")
        self._current_text = []
        if self._current_command:
            self.commands.append(self._current_command)

    def handle_data(self, data):
        if self._current_command:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "button" and self._current_command:
            self.labels[self._current_command] = "".join(self._current_text).strip()
            self._current_command = None
            self._current_text = []


class HugInfraredPageTest(unittest.TestCase):
    def run_node(self, expression):
        helper = STATIC_DIR / "hug.js"
        script = (
            f"const HugRemote=require({json.dumps(str(helper))});"
            f"const result=({expression});"
            "process.stdout.write(JSON.stringify(result));"
        )
        completed = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(completed.stdout)

    def test_document_exposes_exactly_nine_confirmed_controls(self):
        parser = ButtonParser()
        parser.feed((STATIC_DIR / "hug.html").read_text(encoding="utf-8"))

        self.assertEqual(
            [
                "POWER",
                "PAT_START_STOP",
                "SPEED_DOWN",
                "SPEED_UP",
                "BLUETOOTH",
                "TIMER",
                "PREVIOUS",
                "NEXT",
                "RECORD_PLAY",
            ],
            parser.commands,
        )
        self.assertEqual(
            {
                "POWER": "总开关",
                "PAT_START_STOP": "拍打开关",
                "SPEED_DOWN": "拍拍调慢",
                "SPEED_UP": "拍拍调快",
                "BLUETOOTH": "蓝牙",
                "TIMER": "拍打定时",
                "PREVIOUS": "上一首",
                "NEXT": "下一首",
                "RECORD_PLAY": "录音播放",
            },
            parser.labels,
        )

    def test_formal_remote_uses_supplied_artwork_and_accessible_status(self):
        page = (STATIC_DIR / "hug.html").read_text(encoding="utf-8")

        self.assertIn('class="remote-shell"', page)
        self.assertIn("/public/funIcon_0029_爱的抱抱.png", page)
        self.assertIn('aria-live="polite"', page)

    def test_send_command_calls_native_bridge_once_and_returns_result(self):
        result = self.run_node(
            """(() => {
                let calls = 0;
                const bridge = {
                    transmit(command) {
                        calls += 1;
                        return JSON.stringify({
                            ok: true,
                            available: true,
                            carrierHz: 38000,
                            command
                        });
                    }
                };
                return {
                    callsBefore: calls,
                    result: HugRemote.sendCommand(bridge, 'POWER'),
                    callsAfter: calls
                };
            })()"""
        )

        self.assertEqual(0, result["callsBefore"])
        self.assertEqual(1, result["callsAfter"])
        self.assertTrue(result["result"]["ok"])
        self.assertEqual("POWER", result["result"]["command"])

    def test_invalid_native_response_becomes_safe_failure(self):
        self.assertEqual(
            {
                "ok": False,
                "available": True,
                "error": "INVALID_RESPONSE",
                "message": "红外模块返回了无效结果",
            },
            self.run_node(
                "HugRemote.sendCommand({transmit(){return 'not json';}}, 'POWER')"
            ),
        )

    def test_command_labels_are_human_readable(self):
        self.assertEqual(
            {"known": "录音播放", "fallback": "抱枕"},
            self.run_node(
                """({
                    known: HugRemote.commandLabel('RECORD_PLAY'),
                    fallback: HugRemote.commandLabel('UNKNOWN')
                })"""
            ),
        )

    def test_missing_native_bridge_never_attempts_transmission(self):
        self.assertEqual(
            {
                "ok": False,
                "available": False,
                "error": "BRIDGE_UNAVAILABLE",
                "message": "请在支持红外的 AionsHome 手机 App 中使用",
            },
            self.run_node("HugRemote.sendCommand(null, 'POWER')"),
        )

    def test_fastapi_route_returns_the_hug_document(self):
        main = importlib.import_module("main")
        route = next(
            route
            for route in main.app.routes
            if getattr(route, "path", None) == "/hug"
        )

        response = asyncio.run(route.endpoint())

        self.assertEqual(STATIC_DIR / "hug.html", pathlib.Path(response.path))
        self.assertEqual(
            "no-cache, no-store, must-revalidate",
            response.headers["cache-control"],
        )

    def test_client_asset_manifest_versions_hug_document_and_assets(self):
        asset_manifest = importlib.import_module("asset_manifest")

        files = asset_manifest.get_client_asset_manifest()["files"]

        self.assertEqual("document", files["/hug"]["category"])
        self.assertEqual("frontend", files["/static/hug.css"]["category"])
        self.assertEqual("frontend", files["/static/hug.js"]["category"])

    def test_home_app_registry_launches_the_hug_route(self):
        home_path = STATIC_DIR / "home.html"
        script = """
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const match = source.match(/const APPS = (\\[[\\s\\S]*?\\n\\]);/);
if (!match) throw new Error('APPS registry not found');
const apps = vm.runInNewContext(match[1]);
const hug = apps.find(app => app.id === 'hug');
process.stdout.write(JSON.stringify(hug || null));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(home_path)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        self.assertEqual(
            {
                "id": "hug",
                "name": "抱抱",
                "icon": "/public/funIcon_0029_爱的抱抱.png",
                "url": "/hug",
            },
            {
                key: json.loads(completed.stdout)[key]
                for key in ("id", "name", "icon", "url")
            },
        )


if __name__ == "__main__":
    unittest.main()
