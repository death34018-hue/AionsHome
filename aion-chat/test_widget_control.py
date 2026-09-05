import asyncio
import tempfile
import unittest
from pathlib import Path

from widget_control import (
    WidgetAssetCatalog,
    WidgetControlStore,
    build_widget_control_prompt,
    extract_widget_command,
)


class WidgetControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "小组件"
        self.states = self.root / "状态"
        self.states.mkdir(parents=True)
        for name in [
            "Main-平静.png", "Main-困倦.PNG", "Second-平静.png",
            "Second-不耐烦.png", "Main-.png", "Main-坏.jpg",
        ]:
            (self.states / name).write_bytes(b"png")
        (self.root / "横幅.png").write_bytes(b"banner")
        self.catalog = WidgetAssetCatalog(
            self.root,
            name_provider=lambda: {"aion": "Main", "connor": "Second"},
        )
        self.store = WidgetControlStore(
            Path(self.tmp.name) / "state.json", self.catalog
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_catalog_isolates_configured_actor_states(self):
        snapshot = self.catalog.snapshot()
        self.assertEqual(snapshot["aion"]["states"], ["困倦", "平静"])
        self.assertEqual(snapshot["connor"]["states"], ["不耐烦", "平静"])
        self.assertTrue(snapshot["aion"]["assets"]["困倦"]["path"].endswith("Main-困倦.PNG"))

    def test_catalog_exposes_versioned_banner_from_widget_root(self):
        banner = self.catalog.banner_asset()
        self.assertTrue(banner["path"].endswith("横幅.png"))
        self.assertTrue(banner["version"])

    def test_prompt_is_short_and_actor_specific(self):
        prompt = build_widget_control_prompt("aion", self.catalog)
        self.assertIn("可用状态：困倦、平静。", prompt)
        self.assertIn("【小组件:状态】", prompt)
        self.assertIn("【横幅:内容】", prompt)
        self.assertNotIn("不耐烦", prompt)
        self.assertNotIn("横幅会持续", prompt)
        self.assertLessEqual(len(prompt.splitlines()), 4)

    def test_extract_strips_invalid_tags_and_banner_wins(self):
        cleaned, action = extract_widget_command(
            "正文【小组件:不存在】【小组件:困倦】【横幅: 记得喝水 】",
            "aion",
            self.catalog,
        )
        self.assertEqual(cleaned, "正文")
        self.assertEqual(action, {"type": "banner", "content": "记得喝水"})

    def test_extract_accepts_fullwidth_colon_from_ai_reply(self):
        cleaned, action = extract_widget_command(
            "起床啦【小组件：困倦】",
            "aion",
            self.catalog,
        )
        self.assertEqual(cleaned, "起床啦")
        self.assertEqual(action, {"type": "state", "state": "困倦"})

    def test_extract_accepts_all_square_and_corner_bracket_pairs(self):
        cases = [
            ("[小组件:困倦]", {"type": "state", "state": "困倦"}),
            ("[小组件:困倦】", {"type": "state", "state": "困倦"}),
            ("【小组件:困倦]", {"type": "state", "state": "困倦"}),
            ("【小组件:困倦】", {"type": "state", "state": "困倦"}),
            ("[横幅:记得喝水]", {"type": "banner", "content": "记得喝水"}),
            ("[横幅:记得喝水】", {"type": "banner", "content": "记得喝水"}),
            ("【横幅:记得喝水]", {"type": "banner", "content": "记得喝水"}),
            ("【横幅:记得喝水】", {"type": "banner", "content": "记得喝水"}),
        ]

        for command, expected_action in cases:
            with self.subTest(command=command):
                cleaned, action = extract_widget_command(
                    f"正文{command}", "aion", self.catalog
                )
                self.assertEqual(cleaned, "正文")
                self.assertEqual(action, expected_action)

    async def test_banner_owner_state_clears_but_other_actor_state_does_not(self):
        await self.store.show_banner("aion", "来看我")
        await self.store.set_actor_state("connor", "不耐烦")
        state = await self.store.get_state()
        self.assertEqual(state["banner"]["content"], "来看我")
        self.assertEqual(state["actor_states"]["connor"], "不耐烦")

        await self.store.set_actor_state("aion", "困倦")
        state = await self.store.get_state()
        self.assertEqual(state["banner"], {"content": "", "owner_actor_id": ""})
        self.assertEqual(state["actor_states"]["aion"], "困倦")

    async def test_new_banner_overwrites_and_state_reloads(self):
        await self.store.show_banner("aion", "第一条")
        await self.store.show_banner("connor", "第二条")
        reloaded = WidgetControlStore(self.store.path, self.catalog)
        state = await reloaded.get_state()
        self.assertEqual(state["banner"], {
            "content": "第二条", "owner_actor_id": "connor"
        })
        self.assertEqual(state["revision"], 2)

    async def test_apply_reply_executes_without_capability_gate(self):
        cleaned = await self.store.process_reply("晚安【小组件:困倦】", "aion")
        self.assertEqual(cleaned, "晚安")
        self.assertEqual((await self.store.get_state())["actor_states"]["aion"], "困倦")

    def test_shared_processor_is_wired_before_visible_reply_cleanup(self):
        base = Path(__file__).parent
        private_source = (base / "routes" / "chat.py").read_text(encoding="utf-8")
        room_source = (base / "routes" / "chatroom.py").read_text(encoding="utf-8")
        background_source = (base / "schedule.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(
            private_source.count("process_widget_control_commands(full_text, \"aion\")"),
            3,
        )
        self.assertIn(
            "process_widget_control_commands(full_text, who_identity)", room_source
        )
        self.assertIn("process_widget_control_commands(", background_source)
        self.assertIn('"connor" if sender == "connor" else "aion"', background_source)


if __name__ == "__main__":
    unittest.main()
