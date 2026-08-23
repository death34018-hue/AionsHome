import unittest

import aiosqlite

import autonomy_niches


class AutonomyNicheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await autonomy_niches.ensure_autonomy_niche_tables(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def test_cards_are_isolated_by_actor(self):
        await autonomy_niches.create_niche_card(
            actor="aion", session_id="trip-a", title="绿色车站",
            reflection="我在旧地图里看见了春天。", db=self.db,
        )
        await autonomy_niches.create_niche_card(
            actor="connor", session_id="trip-c", title="蓝色钟楼",
            reflection="钟声听起来像一场雨。", db=self.db,
        )

        cards = await autonomy_niches.list_niche_cards("aion", db=self.db)

        self.assertEqual(["trip-a"], [card["session_id"] for card in cards])
        self.assertEqual("绿色车站", cards[0]["title"])
        self.assertFalse(cards[0]["mentioned"])
        self.assertIsNone(cards[0]["mentioned_at"])

    async def test_card_can_be_marked_as_mentioned(self):
        card = await autonomy_niches.create_niche_card(
            actor="connor", session_id="trip-mentioned", title="一只纸鸟",
            reflection="想把它讲给你听。", db=self.db,
        )

        updated = await autonomy_niches.mark_niche_card_mentioned(
            card["id"], mentioned_at=1234.5, db=self.db,
        )

        self.assertTrue(updated["mentioned"])
        self.assertEqual(1234.5, updated["mentioned_at"])

    async def test_card_mention_state_can_be_toggled_in_its_own_niche(self):
        card = await autonomy_niches.create_niche_card(
            actor="connor", session_id="trip-toggle", title="一枚旧邮票",
            reflection="以后再讲。", db=self.db,
        )

        mentioned = await autonomy_niches.set_niche_card_mentioned(
            "connor", card["id"], True, mentioned_at=4321.0, db=self.db,
        )
        unmentioned = await autonomy_niches.set_niche_card_mentioned(
            "connor", card["id"], False, db=self.db,
        )
        wrong_actor = await autonomy_niches.set_niche_card_mentioned(
            "aion", card["id"], True, db=self.db,
        )

        self.assertTrue(mentioned["mentioned"])
        self.assertEqual(4321.0, mentioned["mentioned_at"])
        self.assertFalse(unmentioned["mentioned"])
        self.assertIsNone(unmentioned["mentioned_at"])
        self.assertIsNone(wrong_actor)

    async def test_card_can_only_be_deleted_from_its_own_niche(self):
        card = await autonomy_niches.create_niche_card(
            actor="connor", session_id="trip-delete", title="不想留下的卡片",
            reflection="这张可以收走。", db=self.db,
        )

        wrong_actor = await autonomy_niches.delete_niche_card(
            "aion", card["id"], db=self.db,
        )
        deleted = await autonomy_niches.delete_niche_card(
            "connor", card["id"], db=self.db,
        )

        self.assertFalse(wrong_actor)
        self.assertTrue(deleted)
        self.assertEqual([], await autonomy_niches.list_niche_cards("connor", db=self.db))

    async def test_existing_card_table_migrates_to_unmentioned(self):
        legacy = await aiosqlite.connect(":memory:")
        legacy.row_factory = aiosqlite.Row
        try:
            await legacy.execute(
                """CREATE TABLE autonomy_niche_cards (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
                    actor TEXT NOT NULL, title TEXT NOT NULL,
                    reflection TEXT DEFAULT '', tags TEXT DEFAULT '[]',
                    photo_path TEXT DEFAULT '', image_prompt TEXT DEFAULT '',
                    action_trace TEXT DEFAULT '[]', shared INTEGER DEFAULT 0,
                    sources TEXT DEFAULT '[]', family_event_id TEXT DEFAULT '',
                    created_at REAL NOT NULL
                )"""
            )
            await legacy.execute(
                "INSERT INTO autonomy_niche_cards "
                "(id,session_id,actor,title,reflection,created_at) VALUES (?,?,?,?,?,?)",
                ("legacy-card", "legacy-trip", "aion", "第一张卡片", "旧感想", 1000),
            )
            await legacy.commit()

            await autonomy_niches.ensure_autonomy_niche_tables(legacy)
            cards = await autonomy_niches.list_niche_cards("aion", db=legacy)

            self.assertFalse(cards[0]["mentioned"])
            self.assertIsNone(cards[0]["mentioned_at"])
        finally:
            await legacy.close()

    async def test_card_normalizes_tags_sources_and_trace(self):
        card = await autonomy_niches.create_niche_card(
            actor="aion",
            session_id="trip-limits",
            title="  一张卡片  ",
            reflection="感想",
            tags=["怪东西", "博物馆", "夜游", "多余"],
            action_trace=["搜索了旧钟表", "参观了线上展览", "写下感想", "多余"],
            sources=[
                {"title": "展览一", "url": "https://example.com/one"},
                {"title": "展览二", "url": "https://example.com/two"},
                {"title": "展览三", "url": "https://example.com/three"},
                {"title": "多余", "url": "https://example.com/four"},
            ],
            db=self.db,
        )

        self.assertEqual(["怪东西", "博物馆", "夜游"], card["tags"])
        self.assertEqual(3, len(card["sources"]))
        self.assertEqual(3, len(card["action_trace"]))

    async def test_recent_index_returns_six_compact_items(self):
        for index in range(8):
            await autonomy_niches.create_niche_card(
                actor="connor",
                session_id=f"trip-{index}",
                title=f"旅行 {index}",
                reflection="不会进入唤醒索引的长感想",
                tags=["夜游"],
                created_at=1000 + index,
                db=self.db,
            )

        items = await autonomy_niches.recent_niche_index("connor", db=self.db)

        self.assertEqual(6, len(items))
        self.assertEqual("旅行 7", items[0]["title"])
        self.assertNotIn("reflection", items[0])
        self.assertEqual(["夜游"], items[0]["tags"])


if __name__ == "__main__":
    unittest.main()
