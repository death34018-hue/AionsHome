import unittest

from activity import resolve_app_name


class AppNameResolutionTest(unittest.TestCase):
    def test_activity_log_resolves_frequent_phone_apps(self):
        expected_names = {
            "com.xingin.xhs": "小红书",
            "tv.danmaku.bili": "哔哩哔哩",
            "com.phoenix.read": "红果短剧",
            "com.ss.android.ugc.aweme": "抖音",
        }

        for package_name, display_name in expected_names.items():
            with self.subTest(package_name=package_name):
                self.assertEqual(display_name, resolve_app_name(package_name))

    def test_activity_log_keeps_unknown_package_name(self):
        self.assertEqual(
            "com.example.unknown",
            resolve_app_name("com.example.unknown"),
        )


if __name__ == "__main__":
    unittest.main()
