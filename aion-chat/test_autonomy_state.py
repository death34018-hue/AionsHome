import json
import time
import unittest

import aiosqlite

import autonomy_state


class AutonomyStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(":memory:")
        self.db.row_factory = aiosqlite.Row
        await autonomy_state.ensure_autonomy_tables(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    async def test_new_configs_start_disabled_and_updates_persist(self):
        aion = await autonomy_state.get_actor_config("aion", db=self.db)
        connor = await autonomy_state.get_actor_config("connor", db=self.db)
        self.assertFalse(aion["enabled"])
        self.assertFalse(connor["enabled"])

        await autonomy_state.update_actor_config("aion", enabled=True, db=self.db)

        self.assertTrue((await autonomy_state.get_actor_config("aion", db=self.db))["enabled"])
        self.assertFalse((await autonomy_state.get_actor_config("connor", db=self.db))["enabled"])

    async def test_rest_defaults_on_for_legacy_config_and_can_be_disabled_per_actor(self):
        await self.db.execute("UPDATE autonomy_actor_configs SET actions_json='{}'")
        await self.db.commit()
        self.assertTrue((await autonomy_state.get_actor_config("connor", db=self.db))["actions"].get("rest"))
        await autonomy_state.update_actor_config("connor", actions={"rest": False}, db=self.db)
        self.assertFalse((await autonomy_state.get_actor_config("connor", db=self.db))["actions"]["rest"])
        self.assertTrue((await autonomy_state.get_actor_config("aion", db=self.db))["actions"]["rest"])

    async def test_relationship_date_starts_unset_and_persists_per_actor(self):
        aion = await autonomy_state.get_actor_config("aion", db=self.db)
        connor = await autonomy_state.get_actor_config("connor", db=self.db)
        self.assertIn("relationship_started_on", aion)
        self.assertIsNone(aion["relationship_started_on"])
        self.assertIsNone(connor["relationship_started_on"])

        await autonomy_state.update_actor_config(
            "aion", relationship_started_on="2025-06-09", db=self.db
        )

        self.assertEqual(
            "2025-06-09",
            (await autonomy_state.get_actor_config("aion", db=self.db))["relationship_started_on"],
        )
        self.assertIsNone(
            (await autonomy_state.get_actor_config("connor", db=self.db))["relationship_started_on"]
        )

    async def test_each_actor_keeps_an_independent_persistent_wake_timer(self):
        await autonomy_state.update_actor_config(
            "aion", enabled=True, min_interval_minutes=60, max_interval_minutes=180, db=self.db
        )
        await autonomy_state.update_actor_config(
            "connor", enabled=True, min_interval_minutes=60, max_interval_minutes=180, db=self.db
        )

        await autonomy_state.schedule_actor_wake(
            "aion", anchor_at=1_000, delay_minutes=60, db=self.db
        )
        await autonomy_state.schedule_actor_wake(
            "connor", anchor_at=1_000, delay_minutes=150, db=self.db
        )

        aion = await autonomy_state.get_actor_config("aion", db=self.db)
        connor = await autonomy_state.get_actor_config("connor", db=self.db)
        self.assertEqual(4_600, aion["next_wake_at"])
        self.assertEqual(10_000, connor["next_wake_at"])

    async def test_new_user_message_refreshes_each_actor_timer_from_message_time(self):
        for actor in autonomy_state.ACTOR_IDS:
            await autonomy_state.update_actor_config(
                actor, enabled=True, min_interval_minutes=60, max_interval_minutes=180, db=self.db
            )
            await autonomy_state.schedule_actor_wake(
                actor, anchor_at=1_000, delay_minutes=60, db=self.db
            )

        self.assertTrue(await autonomy_state.refresh_actor_wake_for_user(
            "aion", latest_user_at=2_000, delay_minutes=70, db=self.db
        ))
        self.assertTrue(await autonomy_state.refresh_actor_wake_for_user(
            "connor", latest_user_at=2_000, delay_minutes=160, db=self.db
        ))

        aion = await autonomy_state.get_actor_config("aion", db=self.db)
        connor = await autonomy_state.get_actor_config("connor", db=self.db)
        self.assertEqual(6_200, aion["next_wake_at"])
        self.assertEqual(11_600, connor["next_wake_at"])

    async def test_old_user_message_does_not_keep_rerolling_timer(self):
        await autonomy_state.update_actor_config("aion", enabled=True, db=self.db)
        await autonomy_state.schedule_actor_wake(
            "aion", anchor_at=2_000, delay_minutes=60, db=self.db
        )

        refreshed = await autonomy_state.refresh_actor_wake_for_user(
            "aion", latest_user_at=1_999, delay_minutes=180, db=self.db
        )

        config = await autonomy_state.get_actor_config("aion", db=self.db)
        self.assertFalse(refreshed)
        self.assertEqual(5_600, config["next_wake_at"])

    async def test_due_wake_claim_only_clears_requested_actor_timer(self):
        for actor in autonomy_state.ACTOR_IDS:
            await autonomy_state.update_actor_config(actor, enabled=True, db=self.db)
            await autonomy_state.schedule_actor_wake(
                actor, anchor_at=1_000, delay_minutes=5, db=self.db
            )

        claimed = await autonomy_state.claim_due_wake("aion", now=1_301, db=self.db)

        self.assertIsNotNone(claimed)
        self.assertIsNone((await autonomy_state.get_actor_config("aion", db=self.db))["next_wake_at"])
        self.assertEqual(
            1_300,
            (await autonomy_state.get_actor_config("connor", db=self.db))["next_wake_at"],
        )

    async def test_autonomy_prompt_no_longer_requests_state_packets(self):
        await autonomy_state.update_actor_config("aion", enabled=True, db=self.db)

        self.assertEqual("", await autonomy_state.autonomy_prompt_text("aion"))

    async def test_actor_config_does_not_offer_redundant_monitor_action(self):
        config = await autonomy_state.get_actor_config("aion", db=self.db)

        self.assertNotIn("cam_check", autonomy_state.ACTION_IDS)
        self.assertNotIn("cam_check", config["actions"])

    async def test_valid_state_block_is_removed_and_decoded(self):
        raw = '正文\n<autonomy_state>{"state":"惦记着锅","facts":["锅还在蒸"],"next":{"after_minutes":30}}</autonomy_state>'

        clean, payload, error = autonomy_state.consume_state_block(raw)

        self.assertEqual("正文", clean)
        self.assertEqual("惦记着锅", payload["state"])
        self.assertEqual(30, payload["next"]["after_minutes"])
        self.assertEqual("", error)

    async def test_unclosed_state_block_with_complete_json_is_recovered_and_hidden(self):
        raw = '正文\n<autonomy_state>{"state":"继续惦记","next":{"after_minutes":20}}قراءة المزيد'

        clean, payload, error = autonomy_state.consume_state_block(raw)

        self.assertEqual("正文", clean)
        self.assertEqual("继续惦记", payload["state"])
        self.assertEqual(20, payload["next"]["after_minutes"])
        self.assertEqual("recovered unclosed autonomy state", error)

    async def test_unclosed_truncated_state_block_is_hidden_even_when_unrecoverable(self):
        raw = '正文\n<autonomy_state>{"state":"被截断了"'

        clean, payload, error = autonomy_state.consume_state_block(raw)

        self.assertEqual("正文", clean)
        self.assertIsNone(payload)
        self.assertIn("invalid unclosed autonomy state", error)

    async def test_sync_sanitizer_hides_unclosed_state_block(self):
        raw = '正文\n<autonomy_state>{"state":"继续惦记"}'

        self.assertEqual("正文", autonomy_state.strip_autonomy_state(raw))

    async def test_state_validation_caps_live_items(self):
        payload = {
            "state": "清醒",
            "facts": ["一", "二", "三"],
            "guesses": ["甲", "乙", "丙"],
            "intentions": [{"type": "goal", "text": str(i)} for i in range(5)],
            "next": {"after_minutes": 1},
        }

        normalized, error = autonomy_state.normalize_state_payload(payload, 5, 1440)

        self.assertEqual(["一", "二"], normalized["facts"])
        self.assertEqual(["甲", "乙"], normalized["guesses"])
        self.assertEqual(3, len(normalized["intentions"]))
        self.assertEqual(5, normalized["next"]["after_minutes"])
        self.assertIn("trimmed", error)

    async def test_state_validation_preserves_short_intentions_and_drops_contact(self):
        payload = {
            "state": "等下次醒来再判断",
            "intentions": [
                "确认首次自主唤醒是否成功",
                {"type": "followup", "text": "继续刚才没聊完的话题"},
            ],
            "next": {"after_minutes": 30},
            "contact": "silent",
        }

        normalized, _ = autonomy_state.normalize_state_payload(payload, 5, 1440)

        self.assertEqual(
            [
                {"type": "goal", "text": "确认首次自主唤醒是否成功"},
                {"type": "followup", "text": "继续刚才没聊完的话题"},
            ],
            normalized["intentions"],
        )
        self.assertNotIn("contact", normalized)

    async def test_disabling_cancels_current_packet_but_keeps_history(self):
        await autonomy_state.update_actor_config("aion", enabled=True, db=self.db)
        packet = await autonomy_state.record_persona_state(
            "aion", {"state": "想出去", "next": {"after_minutes": 30}},
            "chat", time.time(), db=self.db,
        )

        await autonomy_state.update_actor_config("aion", enabled=False, db=self.db)

        self.assertIsNone(await autonomy_state.get_current_packet("aion", db=self.db))
        rows = await autonomy_state.list_packets("aion", db=self.db)
        self.assertEqual(packet["id"], rows[0]["id"])
        self.assertEqual("cancelled_by_disable", rows[0]["status"])
        self.assertIsNone(rows[0]["wake_at"])

    async def test_startup_marks_overdue_packet_without_waking(self):
        await autonomy_state.update_actor_config("connor", enabled=True, db=self.db)
        await autonomy_state.record_persona_state(
            "connor", {"state": "等着", "next": {"after_minutes": 5}},
            "chat", time.time() - 600, db=self.db,
        )

        changed = await autonomy_state.expire_overdue_packets(self.db, now=time.time())

        self.assertEqual(1, changed)
        self.assertIsNone(await autonomy_state.get_current_packet("connor", db=self.db))
        self.assertEqual("missed_during_downtime", (await autonomy_state.list_packets("connor", db=self.db))[0]["status"])

    async def test_startup_marks_interrupted_running_packet_abandoned(self):
        await autonomy_state.update_actor_config("connor", enabled=True, db=self.db)
        await autonomy_state.record_persona_state(
            "connor", {"state": "正在醒来", "next": {"after_minutes": 5}},
            "chat", time.time() - 301, db=self.db,
        )
        packet = await autonomy_state.claim_due_packet("connor", now=time.time(), db=self.db)

        changed = await autonomy_state.expire_overdue_packets(self.db, now=time.time())

        rows = await autonomy_state.list_packets("connor", db=self.db)
        self.assertEqual(1, changed)
        self.assertEqual(packet["id"], rows[0]["id"])
        self.assertEqual("abandoned", rows[0]["status"])

    async def test_disabling_also_cancels_claimed_running_packet(self):
        await autonomy_state.update_actor_config("aion", enabled=True, db=self.db)
        await autonomy_state.record_persona_state(
            "aion", {"state": "准备执行", "next": {"after_minutes": 5}},
            "chat", time.time() - 301, db=self.db,
        )
        await autonomy_state.claim_due_packet("aion", now=time.time(), db=self.db)

        await autonomy_state.update_actor_config("aion", enabled=False, db=self.db)

        rows = await autonomy_state.list_packets("aion", db=self.db)
        self.assertEqual("cancelled_by_disable", rows[0]["status"])

    async def test_due_packet_can_be_claimed_only_once(self):
        await autonomy_state.update_actor_config("aion", enabled=True, db=self.db)
        packet = await autonomy_state.record_persona_state(
            "aion", {"state": "准备醒", "next": {"after_minutes": 5}},
            "chat", time.time() - 301, db=self.db,
        )

        first = await autonomy_state.claim_due_packet("aion", now=time.time(), db=self.db)
        second = await autonomy_state.claim_due_packet("aion", now=time.time(), db=self.db)

        self.assertEqual(packet["id"], first["id"])
        self.assertEqual("running", first["status"])
        self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main()
