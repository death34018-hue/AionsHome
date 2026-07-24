import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
JAVA_ROOT = ROOT / "AionApp" / "app" / "src" / "main" / "java" / "com" / "aion" / "chat"


def method_body(source: str, signature: str) -> str:
    start = source.index(signature)
    open_brace = source.index("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace + 1:index]
    raise AssertionError(f"method body not closed: {signature}")


class AppSupervisionAndroidLifecycleTests(unittest.TestCase):
    def test_accessibility_connection_starts_or_restores_runtime(self):
        source = (JAVA_ROOT / "AionAccessibilityService.java").read_text(
            encoding="utf-8"
        )
        body = method_body(source, "protected void onServiceConnected()")
        start_call = "AppSupervisionRuntime.start(this)"
        connected_call = "runtime.onAccessibilityConnected()"
        self.assertIn(start_call, body)
        self.assertIn(connected_call, body)
        self.assertLess(body.index(start_call), body.index(connected_call))
        self.assertNotRegex(body, r"AppSupervisionRuntime\.get\(\)")

    def test_push_service_destruction_keeps_process_runtime_alive(self):
        source = (JAVA_ROOT / "AionPushService.java").read_text(encoding="utf-8")
        body = method_body(source, "public void onDestroy()")
        self.assertNotIn("AppSupervisionRuntime.stop()", body)
        self.assertIn("runtime.setSyncListener(null)", body)

    def test_task_swipe_restart_path_remains_enabled(self):
        source = (JAVA_ROOT / "AionPushService.java").read_text(encoding="utf-8")
        start_body = method_body(source, "public int onStartCommand(")
        removed_body = method_body(source, "public void onTaskRemoved(")
        self.assertIn("return START_STICKY;", start_body)
        self.assertIn("setExactAndAllowWhileIdle", removed_body)
        self.assertRegex(removed_body, re.compile(r"AionPushService\.class"))


if __name__ == "__main__":
    unittest.main()
