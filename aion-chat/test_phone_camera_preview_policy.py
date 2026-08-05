import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
POLICY = ROOT / "static" / "phone-camera-preview-policy.js"


def run_policy(expression):
    script = f"""
const fs = require("fs");
const vm = require("vm");
const context = {{}};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(POLICY))}, "utf8"), context);
const value = vm.runInContext({json.dumps(expression)}, context);
process.stdout.write(JSON.stringify(value));
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class PhoneCameraPreviewPolicyTests(unittest.TestCase):
    def test_preview_requires_visible_phone_page_and_no_event_capture(self):
        base = {
            "pageVisible": True,
            "appForeground": True,
            "source": "phone",
            "eventCaptureActive": False,
        }
        self.assertTrue(run_policy(
            f"PhoneCameraPreviewPolicy.shouldPreview({json.dumps(base)})"
        ))
        for key, value in (
            ("pageVisible", False),
            ("appForeground", False),
            ("source", "local"),
            ("eventCaptureActive", True),
        ):
            state = dict(base)
            state[key] = value
            self.assertFalse(run_policy(
                f"PhoneCameraPreviewPolicy.shouldPreview({json.dumps(state)})"
            ))

    def test_armed_server_config_wins_and_zoom_uses_nearest_supported_value(self):
        capabilities = {
            "back": {"presets": [0.8, 1, 2]},
            "front": {"presets": [1, 2]},
        }
        saved = {"facing": "back", "zoom": 0.8}
        armed = {"armed": True, "facing": "front", "zoom": 1.7}
        result = run_policy(
            "PhoneCameraPreviewPolicy.chooseConfig("
            f"{json.dumps(capabilities)}, {json.dumps(saved)}, {json.dumps(armed)})"
        )
        self.assertEqual({"facing": "front", "zoom": 2}, result)

    def test_saved_config_is_used_when_disarmed_and_invalid_values_fall_back(self):
        capabilities = {
            "back": {"presets": [0.8, 1, 2]},
            "front": {"presets": [1, 2]},
        }
        saved_result = run_policy(
            "PhoneCameraPreviewPolicy.chooseConfig("
            f"{json.dumps(capabilities)}, "
            f"{json.dumps({'facing': 'back', 'zoom': 0.8})}, "
            f"{json.dumps({'armed': False, 'facing': 'front', 'zoom': 2})})"
        )
        self.assertEqual({"facing": "back", "zoom": 0.8}, saved_result)

        fallback_result = run_policy(
            "PhoneCameraPreviewPolicy.chooseConfig("
            f"{json.dumps(capabilities)}, "
            f"{json.dumps({'facing': 'side', 'zoom': 99})}, "
            f"{json.dumps({'armed': False})})"
        )
        self.assertEqual({"facing": "back", "zoom": 2}, fallback_result)

    def test_running_preview_without_new_frames_becomes_stale_after_grace(self):
        healthy = {
            "running": True,
            "lastFrameAt": 10_000,
            "startedAt": 9_000,
            "now": 12_000,
            "graceMs": 2_500,
        }
        stale = dict(healthy, now=12_501)
        stopped = dict(stale, running=False)
        self.assertFalse(run_policy(
            f"PhoneCameraPreviewPolicy.isStale({json.dumps(healthy)})"
        ))
        self.assertTrue(run_policy(
            f"PhoneCameraPreviewPolicy.isStale({json.dumps(stale)})"
        ))
        self.assertFalse(run_policy(
            f"PhoneCameraPreviewPolicy.isStale({json.dumps(stopped)})"
        ))


if __name__ == "__main__":
    unittest.main()
