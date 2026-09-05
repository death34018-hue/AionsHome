import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import autonomy
from album import AlbumStore
from test_album import sample_image


class AutonomyAlbumTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AlbumStore(Path(self.temp.name))
        self.photos = [self.store.save_photo(sample_image(), source="upload") for _ in range(2)]
        self.store.save_photo(sample_image(), source="generated", actor="aion")

    async def test_selected_photos_are_attached_and_only_successful_reads_are_remembered(self):
        self.assertTrue(hasattr(autonomy, "_run_album_browse"))
        with patch.object(autonomy, "get_album_store", return_value=self.store), \
             patch.object(autonomy, "_actor_context", new=AsyncMock(return_value=[])), \
             patch.object(autonomy, "_select_action", new=AsyncMock(return_value={"action": "album_browse", "reason": "想看看照片"})), \
             patch.object(autonomy, "append_idle_event", new=AsyncMock(return_value={"id": "event-1"})) as event, \
             patch.object(autonomy, "_call_actor", new=AsyncMock(return_value='{"reflection":"这些照片让我想起温柔的日常。"}')) as call:
            payload = await autonomy._run_actor_once("aion")
            self.assertTrue(payload["ok"])
            result = payload["result"]
            messages = call.await_args.args[1]
            self.assertEqual(len(messages[-1]["attachments"]), 2)
            self.assertEqual({a["url"] for a in messages[-1]["attachments"]}, {p["url"] for p in self.photos})
            self.assertEqual(result["reflection"], "这些照片让我想起温柔的日常。")
            self.assertEqual(event.await_args.args[1], "wake_summary")
            self.assertEqual(event.await_args.args[3], result["reflection"])
            self.assertEqual(len(event.await_args.kwargs["metadata"]["album_photos"]), 2)
            self.assertFalse(self.store.has_unseen_photos("aion"))
            self.assertTrue(self.store.has_unseen_photos("connor"))
            self.assertEqual((await autonomy._run_album_browse("aion"))["photos"], [])
            self.assertEqual(call.await_count, 1)

    async def test_optional_album_message_uses_existing_actor_delivery_without_photos(self):
        for actor in ("aion", "connor"):
            with self.subTest(actor=actor), \
                 patch.object(autonomy, "get_album_store", return_value=self.store), \
                 patch.object(autonomy, "_actor_context", new=AsyncMock(return_value=[])), \
                 patch.object(autonomy, "_select_action", new=AsyncMock(return_value={"action": "album_browse"})), \
                 patch.object(autonomy, "append_idle_event", new=AsyncMock(return_value={"id": "event-1"})) as event, \
                 patch.object(autonomy, "_call_actor", new=AsyncMock(return_value=json.dumps({
                     "reflection": "安静的日常。", "share": True, "share_message": "  想和你再一起散散步。  "}))) as call, \
                 patch.object(autonomy, "_save_private_message", new=AsyncMock(return_value={"id": "message-1"})) as send:
                payload = await autonomy._run_actor_once(actor)
                self.assertTrue(payload["ok"])
                send.assert_awaited_once_with(actor, "想和你再一起散散步。", attachments=[])
                self.assertEqual(call.await_count, 1)
                self.assertEqual(payload["result"]["reflection"], "安静的日常。")
                self.assertTrue(event.await_args.kwargs["metadata"]["shared"])
                self.assertFalse(self.store.has_unseen_photos(actor))

    async def test_album_message_requires_explicit_true_and_nonempty_text(self):
        for fields in ({}, {"share": False, "share_message": "不要发送"},
                       {"share": "false", "share_message": "不要发送"},
                       {"share": True, "share_message": "  "}):
            with self.subTest(fields=fields), \
                 patch.object(autonomy, "get_album_store", return_value=self.store), \
                 patch.object(autonomy, "_actor_context", new=AsyncMock(return_value=[])), \
                 patch.object(autonomy, "_call_actor", new=AsyncMock(return_value=json.dumps({"reflection": "静静看看。", **fields}))), \
                 patch.object(autonomy, "_save_private_message", new=AsyncMock()) as send:
                self.store.save_photo(sample_image(), source="upload")
                result = await autonomy._run_album_browse("aion")
                self.assertEqual(result["reflection"], "静静看看。")
                send.assert_not_awaited()

    async def test_album_delivery_failure_preserves_reflection_and_successful_read(self):
        with patch.object(autonomy, "get_album_store", return_value=self.store), \
             patch.object(autonomy, "_actor_context", new=AsyncMock(return_value=[])), \
             patch.object(autonomy, "_select_action", new=AsyncMock(return_value={"action": "album_browse"})), \
             patch.object(autonomy, "append_idle_event", new=AsyncMock(return_value={"id": "event-1"})) as event, \
             patch.object(autonomy, "_call_actor", new=AsyncMock(return_value=json.dumps({
                 "reflection": "这些瞬间值得留下。", "share": True, "share_message": "想你了。"}))), \
             patch.object(autonomy, "_save_private_message", new=AsyncMock(side_effect=RuntimeError("offline"))):
            payload = await autonomy._run_actor_once("aion")
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["result"]["reflection"], "这些瞬间值得留下。")
            self.assertTrue(payload["result"].get("share_error"))
            self.assertIn(payload["result"]["share_error"], event.await_args.args[3])
            self.assertFalse(event.await_args.kwargs["metadata"]["shared"])
            self.assertFalse(self.store.has_unseen_photos("aion"))

    async def test_failed_model_call_leaves_photos_unseen(self):
        self.assertTrue(hasattr(autonomy, "_run_album_browse"))
        with patch.object(autonomy, "get_album_store", return_value=self.store), \
             patch.object(autonomy, "_actor_context", new=AsyncMock(return_value=[])), \
             patch.object(autonomy, "_call_actor", new=AsyncMock(side_effect=RuntimeError("offline"))):
            with self.assertRaises(RuntimeError):
                await autonomy._run_album_browse("aion")
        self.assertEqual(len(self.store.random_unseen_photos("aion")), 2)

    async def test_album_option_disappears_when_read_and_returns_for_new_photo(self):
        self.assertIn("album_browse", autonomy.ACTION_DEFS)
        config = {"actions": {"album_browse": True}}
        with patch.object(autonomy, "get_album_store", return_value=self.store), \
             patch.object(autonomy, "get_actor_config", new=AsyncMock(return_value=config)), \
             patch.object(autonomy, "recent_niche_index", new=AsyncMock(return_value=[])), \
             patch.object(autonomy, "_ask_actor_json", new=AsyncMock(return_value={"action": "album_browse"})) as ask:
            self.assertEqual((await autonomy._select_action("aion"))["action"], "album_browse")
            self.store.mark_viewed("aion", [p["id"] for p in self.photos])
            self.assertEqual((await autonomy._select_action("aion"))["action"], "rest")
            self.assertNotIn("- album_browse:", ask.await_args.args[1])
            self.store.save_photo(sample_image(), source="upload")
            self.assertEqual((await autonomy._select_action("aion"))["action"], "album_browse")
            self.assertEqual(len(self.store.random_unseen_photos("aion")), 1)
