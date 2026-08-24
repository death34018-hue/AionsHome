import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite

from english_corner import ensure_english_corner_tables, set_card_status
from family_events import (
    build_user_timeline_items,
    list_grouped_user_events,
    record_user_event,
)


class FamilyUserEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "family-events.sqlite3"

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_continuous_events_merge_by_kind_within_thirty_minutes(self):
        for index, created_at in enumerate((100.0, 700.0, 1300.0), start=1):
            await record_user_event(
                "english_card_learned",
                source_id=f"card-{index}",
                created_at=created_at,
                db_path=self.db_path,
            )
        for index, created_at in enumerate((200.0, 800.0, 1400.0), start=1):
            await record_user_event(
                "seeky_feed",
                source_id=f"feed-{index}",
                metadata={"subject_name": "Configured Pet"},
                created_at=created_at,
                db_path=self.db_path,
            )
        await record_user_event(
            "english_card_learned",
            source_id="card-later",
            created_at=3201.0,
            db_path=self.db_path,
        )

        groups = await list_grouped_user_events(
            since=0,
            limit=20,
            db_path=self.db_path,
        )

        self.assertEqual(
            [(item["kind"], item["count"], item["timestamp"]) for item in groups],
            [
                ("english_card_learned", 1, 3201.0),
                ("seeky_feed", 3, 1400.0),
                ("english_card_learned", 3, 1300.0),
            ],
        )
        timeline = build_user_timeline_items(groups, "Configured User")
        self.assertEqual(timeline[0]["title"], "Configured User 学习了英语，完成了 1 张卡片")
        self.assertEqual(timeline[1]["title"], "Configured User 给 Configured Pet 喂了食")
        self.assertNotIn("3 次", timeline[1]["title"])

    async def test_marking_an_english_card_learned_records_only_the_transition(self):
        async with aiosqlite.connect(self.db_path) as db:
            await ensure_english_corner_tables(db)
            pack = await db.execute(
                "INSERT INTO english_learning_packs "
                "(request_id,generator,model_key,tts_voice,context_total,context_limit,created_at) "
                "VALUES ('request-1','aion','','',0,0,1)",
            )
            card_id = (
                await db.execute(
                    "INSERT INTO english_learning_cards "
                    "(pack_id,position,title,status,learned_at,updated_at) "
                    "VALUES (?,0,'A useful phrase','learning',NULL,1)",
                    (pack.lastrowid,),
                )
            ).lastrowid
            await db.commit()

        await set_card_status(card_id, "learned", db_path=self.db_path)
        await set_card_status(card_id, "learned", db_path=self.db_path)

        groups = await list_grouped_user_events(
            since=0,
            limit=20,
            db_path=self.db_path,
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "english_card_learned")
        self.assertEqual(groups[0]["count"], 1)

    async def test_continuous_aquarium_cleaning_is_one_configured_name_memory(self):
        for index, created_at in enumerate((100.0, 300.0, 600.0), start=1):
            await record_user_event(
                "seeky_clean",
                source_id=f"clean-{index}",
                metadata={"subject_name": "Configured Pet"},
                created_at=created_at,
                db_path=self.db_path,
            )

        groups = await list_grouped_user_events(
            since=0,
            limit=20,
            db_path=self.db_path,
        )
        timeline = build_user_timeline_items(groups, "Configured User")

        self.assertEqual(len(timeline), 1)
        self.assertEqual(
            timeline[0]["title"],
            "Configured User 给 Configured Pet 清理了水族箱",
        )

    async def test_timeline_logging_failure_does_not_undo_learning(self):
        async with aiosqlite.connect(self.db_path) as db:
            await ensure_english_corner_tables(db)
            pack = await db.execute(
                "INSERT INTO english_learning_packs "
                "(request_id,generator,model_key,tts_voice,context_total,context_limit,created_at) "
                "VALUES ('request-2','aion','','',0,0,1)",
            )
            card_id = (
                await db.execute(
                    "INSERT INTO english_learning_cards "
                    "(pack_id,position,title,status,learned_at,updated_at) "
                    "VALUES (?,0,'Another phrase','learning',NULL,1)",
                    (pack.lastrowid,),
                )
            ).lastrowid
            await db.commit()

        with patch(
            "english_corner.record_user_event",
            new=AsyncMock(side_effect=OSError("timeline unavailable")),
        ):
            learned = await set_card_status(card_id, "learned", db_path=self.db_path)

        self.assertEqual(learned["status"], "learned")
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT status FROM english_learning_cards WHERE id=?",
                    (card_id,),
                )
            ).fetchone()
        self.assertEqual(row[0], "learned")


if __name__ == "__main__":
    unittest.main()
