import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from routes import autonomy as routes


class AutonomyRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_rest_is_exposed_as_a_configurable_action(self):
        with patch.object(routes, "autonomy_status_payload", new=AsyncMock(return_value={"roles": []})):
            payload = await routes.read_autonomy_status()
        self.assertIn("rest", [action["key"] for action in payload["actions"]])

    async def test_unknown_actor_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            await routes.read_actor_config("nobody")
        self.assertEqual(404, raised.exception.status_code)

    async def test_update_targets_only_requested_actor(self):
        expected = {"actor": "connor", "enabled": True}
        with patch.object(routes, "update_actor_config", new=AsyncMock(return_value=expected)) as update, \
             patch.object(routes.manager, "broadcast", new=AsyncMock()):
            result = await routes.update_actor(
                "connor", routes.ActorConfigUpdate(enabled=True, min_interval_minutes=1, max_interval_minutes=9999)
            )
        self.assertEqual(expected, result["config"])
        self.assertEqual("connor", update.await_args.args[0])

    async def test_relationship_date_update_persists_on_requested_actor(self):
        self.assertTrue(hasattr(routes, "update_relationship_date"))
        expected = {"actor": "aion", "relationship_started_on": "2025-06-09"}
        with patch.object(routes, "update_actor_config", new=AsyncMock(return_value=expected)) as update, \
             patch.object(routes.manager, "broadcast", new=AsyncMock()):
            result = await routes.update_relationship_date(
                "aion", routes.RelationshipDateUpdate(started_on="2025-06-09")
            )
        self.assertEqual(expected, result["config"])
        self.assertEqual("aion", update.await_args.args[0])
        self.assertEqual("2025-06-09", update.await_args.kwargs["relationship_started_on"])

    async def test_relationship_date_rejects_a_future_day(self):
        tomorrow = date.today() + timedelta(days=1)
        with patch.object(routes, "update_actor_config", new=AsyncMock()) as update:
            with self.assertRaises(HTTPException) as raised:
                await routes.update_relationship_date(
                    "aion", routes.RelationshipDateUpdate(started_on=tomorrow)
                )
        self.assertEqual(422, raised.exception.status_code)
        update.assert_not_awaited()

    async def test_manual_run_names_requested_actor(self):
        with patch.object(routes.idle_autonomy_mgr, "run_actor_once", new=AsyncMock(return_value={"ok": True, "actor": "aion"})) as run:
            result = await routes.run_actor_once("aion")
        self.assertEqual("aion", result["actor"])
        run.assert_awaited_once_with("aion", manual=True)


if __name__ == "__main__":
    unittest.main()
