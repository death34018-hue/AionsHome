import sys
import tempfile
import unittest
from pathlib import Path

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class MessageIngressDedupeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"

    def tearDown(self):
        self.tmp.cleanup()

    async def test_reserve_rejects_same_message_inside_window(self):
        from message_dedup import (
            build_message_dedupe_key,
            ensure_message_ingress_dedupe_table,
            reserve_message_ingress,
        )

        key = build_message_dedupe_key(
            target_type="chatroom",
            target_id="room-1",
            sender="user",
            content="same text",
            attachments=["/uploads/a.png"],
        )

        async with aiosqlite.connect(self.db_path) as db:
            await ensure_message_ingress_dedupe_table(db)
            duplicate = await reserve_message_ingress(
                db,
                dedupe_key=key,
                target_type="chatroom",
                target_id="room-1",
                message_id="cm_1_u",
                now=1000.0,
            )
            self.assertIsNone(duplicate)
            await db.commit()

        async with aiosqlite.connect(self.db_path) as db:
            await ensure_message_ingress_dedupe_table(db)
            duplicate = await reserve_message_ingress(
                db,
                dedupe_key=key,
                target_type="chatroom",
                target_id="room-1",
                message_id="cm_2_u",
                now=1008.0,
            )

        self.assertIsNotNone(duplicate)
        self.assertEqual(duplicate["message_id"], "cm_1_u")

    async def test_reserve_allows_same_message_after_window(self):
        from message_dedup import (
            build_message_dedupe_key,
            ensure_message_ingress_dedupe_table,
            reserve_message_ingress,
        )

        key = build_message_dedupe_key(
            target_type="private",
            target_id="conv-1",
            sender="user",
            content="same text",
            attachments=[],
        )

        async with aiosqlite.connect(self.db_path) as db:
            await ensure_message_ingress_dedupe_table(db)
            first_duplicate = await reserve_message_ingress(
                db,
                dedupe_key=key,
                target_type="private",
                target_id="conv-1",
                message_id="msg_1",
                now=1000.0,
                window_seconds=20.0,
            )
            second_duplicate = await reserve_message_ingress(
                db,
                dedupe_key=key,
                target_type="private",
                target_id="conv-1",
                message_id="msg_2",
                now=1021.0,
                window_seconds=20.0,
            )

        self.assertIsNone(first_duplicate)
        self.assertIsNone(second_duplicate)

    def test_key_normalizes_attachment_dict_order(self):
        from message_dedup import build_message_dedupe_key

        left = build_message_dedupe_key(
            target_type="private",
            target_id="conv-1",
            sender="user",
            content="hello",
            attachments=[{"url": "/uploads/a.png", "type": "image"}],
        )
        right = build_message_dedupe_key(
            target_type="private",
            target_id="conv-1",
            sender="user",
            content="hello",
            attachments=[{"type": "image", "url": "/uploads/a.png"}],
        )

        self.assertEqual(left, right)

if __name__ == "__main__":
    unittest.main()
