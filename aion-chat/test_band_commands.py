import contextlib
import pathlib
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite

import band_commands


SCHEMA = """
CREATE TABLE health_miband_commands (
    id TEXT PRIMARY KEY,
    pattern TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    source_msg_id TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    acknowledged_at REAL
);
"""


class BandCommandTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.temp.name) / "commands.db"
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def asyncTearDown(self):
        self.temp.cleanup()

    def get_db(self):
        path = self.db_path

        @contextlib.asynccontextmanager
        async def manager():
            db = await aiosqlite.connect(path)
            try:
                yield db
            finally:
                await db.close()

        return manager()

    def test_extracts_first_command_and_hides_all_protocol_text(self):
        cleaned, pattern, note = band_commands.extract_band_vibration(
            "看我一下 [band_vibrate:CALL] 后面又写 [BAND_VIBRATE:single]"
        )
        self.assertEqual("call", pattern)
        self.assertEqual("", note)
        self.assertEqual("看我一下  后面又写", cleaned)

    def test_disabled_capability_still_hides_but_does_not_execute(self):
        cleaned, pattern, note = band_commands.extract_band_vibration(
            "宝宝 [BAND_VIBRATE:single]", enabled=False
        )
        self.assertEqual("宝宝", cleaned)
        self.assertIsNone(pattern)
        self.assertEqual("", note)

    def test_extracts_note_commands_normalizes_text_and_executes_only_first(self):
        cleaned, pattern, note = band_commands.extract_band_vibration(
            "正文 [BAND_NOTE_SINGLE:  滚起来\n活动！ ] 后面 "
            "[BAND_NOTE_CALL:第二张] [BAND_VIBRATE:call]"
        )
        self.assertEqual("正文  后面", cleaned)
        self.assertEqual("single", pattern)
        self.assertEqual("滚起来 活动！", note)

    def test_empty_note_is_hidden_but_not_executed_and_long_note_is_truncated(self):
        cleaned, pattern, note = band_commands.extract_band_vibration(
            "正文 [BAND_NOTE_CALL:   ]"
        )
        self.assertEqual("正文", cleaned)
        self.assertIsNone(pattern)
        self.assertEqual("", note)

        _, pattern, note = band_commands.extract_band_vibration(
            f"[BAND_NOTE_SINGLE:{'鬣' * 81}]"
        )
        self.assertEqual("single", pattern)
        self.assertEqual("鬣" * 80, note)

    async def test_command_is_durable_for_ten_minutes_and_acknowledged_once(self):
        broadcaster = AsyncMock()
        with patch.object(band_commands, "get_db", self.get_db), \
             patch.object(band_commands.manager, "broadcast", broadcaster), \
             patch.object(band_commands, "is_capability_enabled", return_value=True), \
             patch.object(band_commands, "resolve_band_sender_name", return_value="星澜"), \
             patch.object(band_commands.time, "time", return_value=1_000.0):
            cleaned = await band_commands.process_band_vibration(
                "快看消息 [BAND_NOTE_CALL:快看我！] [BAND_NOTE_SINGLE:第二张]",
                source_type="private",
                source_id="conv-1",
                source_msg_id="msg-1",
                sender="aion",
            )
            pending = await band_commands.list_pending_band_commands(now=1_599.0)

        self.assertEqual("快看消息", cleaned)
        self.assertEqual(1, len(pending))
        self.assertEqual("call", pending[0]["pattern"])
        self.assertEqual("快看我！", pending[0]["note"])
        self.assertEqual("星澜", pending[0]["sender_name"])
        self.assertEqual(1_600.0, pending[0]["expires_at"])
        broadcaster.assert_awaited_once()
        event = broadcaster.await_args.args[0]
        self.assertEqual("mi_band_command", event["type"])
        self.assertEqual(pending[0]["id"], event["data"]["id"])

        with patch.object(band_commands, "get_db", self.get_db):
            acknowledged = await band_commands.acknowledge_band_command(pending[0]["id"], now=1_200.0)
            after_ack = await band_commands.list_pending_band_commands(now=1_300.0)
        self.assertTrue(acknowledged)
        self.assertEqual([], after_ack)

    async def test_expired_and_duplicate_source_message_are_not_replayed(self):
        with patch.object(band_commands, "get_db", self.get_db), \
             patch.object(band_commands.manager, "broadcast", AsyncMock()), \
             patch.object(band_commands, "is_capability_enabled", return_value=True), \
             patch.object(band_commands.time, "time", return_value=2_000.0):
            await band_commands.process_band_vibration(
                "一 [BAND_VIBRATE:single]", source_type="camera", source_id="cam", source_msg_id="same"
            )
            await band_commands.process_band_vibration(
                "二 [BAND_VIBRATE:single]", source_type="camera", source_id="cam", source_msg_id="same"
            )
            before_expiry = await band_commands.list_pending_band_commands(now=2_599.0)
            after_expiry = await band_commands.list_pending_band_commands(now=2_600.0)

        self.assertEqual(1, len(before_expiry))
        self.assertEqual([], after_expiry)
    async def test_message_attachment_is_built_from_persisted_command_and_deduped(self):
        with patch.object(band_commands, "get_db", self.get_db), \
             patch.object(band_commands.manager, "broadcast", AsyncMock()), \
             patch.object(band_commands, "is_capability_enabled", return_value=True), \
             patch.object(band_commands, "resolve_band_sender_name", return_value="星澜"), \
             patch.object(band_commands.time, "time", return_value=3_000.0):
            await band_commands.process_band_vibration(
                "想你 [BAND_NOTE_SINGLE:起来让我抱一下]",
                source_type="private",
                source_id="conv-1",
                source_msg_id="msg-single",
                sender="aion",
            )

        existing = [{"type": "music", "id": "song-1"}]
        with patch.object(band_commands, "get_db", self.get_db):
            attached = await band_commands.with_band_vibration_attachment("msg-single", existing)
            attached_twice = await band_commands.with_band_vibration_attachment("msg-single", attached)
            untouched = await band_commands.with_band_vibration_attachment("missing", existing)

        expected_note = {
            "type": "band_vibration",
            "pattern": "single",
            "note": "起来让我抱一下",
            "sender_name": "星澜",
            "label": "手环轻震：起来让我抱一下",
        }
        self.assertEqual(existing + [expected_note], attached)
        self.assertEqual(attached, attached_twice)
        self.assertEqual(existing, untouched)
        self.assertEqual(existing, [{"type": "music", "id": "song-1"}])

    def test_call_attachment_uses_urgent_label(self):
        self.assertEqual(
            {
                "type": "band_vibration",
                "pattern": "call",
                "label": "手环震动：紧急呼叫！",
            },
            band_commands.band_vibration_attachment("call"),
        )

    def test_note_attachment_uses_note_without_sender_in_interface_label(self):
        self.assertEqual(
            {
                "type": "band_vibration",
                "pattern": "call",
                "note": "滚起来活动！",
                "sender_name": "星澜",
                "label": "手环呼唤：滚起来活动！",
            },
            band_commands.band_vibration_attachment(
                "call", note="滚起来活动！", sender_name="星澜"
            ),
        )


if __name__ == "__main__":
    unittest.main()
