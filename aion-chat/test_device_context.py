import unittest

from device_context import DeviceContextStore


class DeviceContextStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = DeviceContextStore()

    def test_home_foreground_follows_theater_then_returns_to_home(self):
        self.store.update_phone({
            "screen": {"value": "on", "observed_at": 100},
            "foreground_app": {"value": "com.aion.chat", "observed_at": 100},
        }, 100)
        for timestamp, page, expected in (
            (101, "theater", "在小剧场"),
            (102, "home", "在小家里"),
            (103, "theater", "在小剧场"),
            (104, "background", "在小家里"),
        ):
            self.store.update_phone({
                "home_page": {"value": page, "observed_at": timestamp},
            }, timestamp)
            phone = self.store.snapshot({}, timestamp)["phone"]
            self.assertEqual(expected, phone["foreground_app"]["display_value"])
            self.assertEqual("com.aion.chat", phone["foreground_app"]["value"])
            self.assertNotIn("home_page", phone)
            self.assertIn(f"前台为 {expected}", self.store.prompt({}, timestamp))
        self.store.update_phone({
            "home_page": {"value": "theater", "observed_at": 103},
        }, 105)
        self.assertEqual("在小家里", self.store.snapshot({}, 105)["phone"]
                         ["foreground_app"]["display_value"])

    def test_theater_page_does_not_override_other_apps_lock_or_expired_state(self):
        for app, screen, now, expected in (
            ("com.tencent.mm", "on", 102, "微信"),
            ("com.aion.chat", "off", 102, "在小家里"),
            ("com.aion.chat", "on", 192, "在小家里"),
        ):
            with self.subTest(app=app, screen=screen, now=now):
                store = DeviceContextStore()
                store.update_phone({
                    "screen": {"value": screen, "observed_at": 100},
                    "foreground_app": {"value": app, "observed_at": 100},
                    "home_page": {"value": "theater", "observed_at": 101},
                }, 101)
                self.assertEqual(expected, store.snapshot({}, now)["phone"]
                                 ["foreground_app"]["display_value"])

    def test_new_value_overwrites_old_value_while_heartbeat_keeps_since(self):
        self.store.update_phone(
            {"posture": {"value": "portrait", "observed_at": 100}}, 100
        )
        self.store.update_phone(
            {"posture": {"value": "portrait", "observed_at": 110}}, 110
        )
        self.assertEqual(100, self.store.slots["posture"]["since"])

        self.store.update_phone(
            {"posture": {"value": "face_down", "observed_at": 120}}, 120
        )

        self.assertEqual("face_down", self.store.slots["posture"]["value"])
        self.assertEqual(120, self.store.slots["posture"]["since"])

    def test_phone_state_older_than_thirty_minutes_is_not_current(self):
        self.store.update_phone(
            {"posture": {"value": "face_up", "observed_at": 100}}, 100
        )

        self.assertNotIn("posture", self.store.snapshot({}, 1901)["phone"])

    def test_recent_pc_input_beats_possible_bed_phone(self):
        self.store.update_phone(
            {
                "screen": {"value": "on", "observed_at": 100},
                "posture": {"value": "landscape_left", "observed_at": 100},
                "motion": {"value": "slight", "observed_at": 100},
                "light": {"value": "dark", "observed_at": 100},
                "foreground_app": {"value": "哔哩哔哩", "observed_at": 100},
            },
            100,
        )

        snapshot = self.store.snapshot(
            {"display": "on", "idle_seconds": 8, "app": "VS Code"}, 110
        )

        self.assertEqual("computer_active", snapshot["primary"]["state"])

    def test_idle_pc_window_is_not_active_work(self):
        snapshot = self.store.snapshot(
            {"display": "on", "idle_seconds": 601, "app": "VS Code"}, 100
        )

        self.assertNotEqual(
            "computer_active", (snapshot.get("primary") or {}).get("state")
        )

    def test_prompt_contains_pc_and_phone_observations_without_primary_conclusion(self):
        self.store.update_phone(
            {
                "screen": {"value": "on", "observed_at": 100},
                "posture": {"value": "portrait", "observed_at": 100},
                "motion": {"value": "moving", "observed_at": 100},
                "light": {"value": "dim", "observed_at": 100},
                "proximity": {"value": "near", "observed_at": 100},
                "foreground_app": {
                    "value": "com.xingin.xhs",
                    "observed_at": 100,
                },
            },
            100,
        )

        prompt = self.store.prompt(
            {"display": "on", "idle_seconds": 8, "app": "ChatGPT"}, 110
        )

        self.assertEqual(
            "\n".join(
                [
                    "【设备当前状态】",
                    "电脑：8 秒前有键鼠输入，前台为 ChatGPT。",
                    "手机：亮屏、竖屏、移动中、环境较暗、顶部被遮挡或贴近物体、前台为 小红书。",
                    "更新时间：08:01",
                    "数据来自设备观测，请结合当前时间和对话上下文自行判断用户当前状态。",
                ]
            ),
            prompt,
        )
        self.assertNotIn("正在电脑前操作", prompt)

        self.store.update_phone(
            {"proximity": {"value": "far", "observed_at": 111}}, 111
        )
        prompt = self.store.prompt(
            {"display": "on", "idle_seconds": 9, "app": "ChatGPT"}, 112
        )
        self.assertNotIn("顶部被遮挡或贴近物体", prompt)
        self.assertNotIn("距离传感器", prompt)

    def test_phone_foreground_app_must_be_newer_than_current_screen_on_cycle(self):
        self.store.update_phone(
            {
                "screen": {"value": "on", "observed_at": 100},
                "foreground_app": {
                    "value": "com.xingin.xhs",
                    "observed_at": 100,
                },
            },
            100,
        )
        self.assertIn("前台为 小红书", self.store.prompt({}, 110))

        self.store.update_phone(
            {"screen": {"value": "off", "observed_at": 120}}, 120
        )
        self.assertNotIn("前台为", self.store.prompt({}, 121))

        self.store.update_phone(
            {"screen": {"value": "on", "observed_at": 140}}, 140
        )
        self.assertNotIn("前台为", self.store.prompt({}, 141))

        self.store.update_phone(
            {
                "foreground_app": {
                    "value": "com.bbk.launcher2",
                    "observed_at": 150,
                }
            },
            150,
        )
        self.assertIn("前台为 vivo 桌面", self.store.prompt({}, 151))

    def test_known_foreground_apps_have_readable_display_names(self):
        expected_names = {
            "com.xingin.xhs": "小红书",
            "tv.danmaku.bili": "哔哩哔哩",
            "com.phoenix.read": "红果短剧",
            "com.ss.android.ugc.aweme": "抖音",
        }

        for index, (package_name, display_name) in enumerate(
            expected_names.items(), start=100
        ):
            with self.subTest(package_name=package_name):
                self.store.update_phone(
                    {
                        "screen": {"value": "on", "observed_at": index},
                        "foreground_app": {
                            "value": package_name,
                            "observed_at": index,
                        },
                    },
                    index,
                )

                snapshot = self.store.snapshot({}, index + 1)
                foreground = snapshot["phone"]["foreground_app"]
                self.assertEqual(package_name, foreground["value"])
                self.assertEqual(display_name, foreground.get("display_value"))
                self.assertIn(
                    f"前台为 {display_name}", self.store.prompt({}, index + 1)
                )

    def test_unknown_foreground_app_keeps_package_name_as_display_name(self):
        self.store.update_phone(
            {
                "screen": {"value": "on", "observed_at": 100},
                "foreground_app": {
                    "value": "com.example.unknown",
                    "observed_at": 100,
                },
            },
            100,
        )

        foreground = self.store.snapshot({}, 101)["phone"]["foreground_app"]
        self.assertEqual("com.example.unknown", foreground.get("display_value"))
        self.assertIn("前台为 com.example.unknown", self.store.prompt({}, 101))

    def test_notification_update_replaces_same_key_and_keeps_other_key(self):
        self.store.upsert_notification(
            {"key": "order", "title": "外卖", "text": "商家已接单"}, 100
        )
        self.store.upsert_notification(
            {"key": "chat", "title": "妈妈", "text": "到家告诉我"}, 101
        )
        self.store.upsert_notification(
            {"key": "order", "title": "外卖", "text": "骑手已取餐"}, 102
        )

        self.assertEqual("骑手已取餐", self.store.notifications["order"]["text"])
        self.assertIn("chat", self.store.notifications)

    def test_prompt_is_empty_without_evidence_and_respects_hard_budget(self):
        self.assertEqual("", self.store.prompt({}, 100))
        for index in range(30):
            self.store.upsert_notification(
                {
                    "key": str(index),
                    "title": f"通知{index}",
                    "text": "内容" * 80,
                },
                100,
            )

        self.assertLessEqual(len(self.store.prompt({}, 101)), 800)


if __name__ == "__main__":
    unittest.main()
