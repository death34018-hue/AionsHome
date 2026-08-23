import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from routes import autonomy as routes


class AutonomyRouteTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_manual_run_names_requested_actor(self):
        with patch.object(routes.idle_autonomy_mgr, "run_actor_once", new=AsyncMock(return_value={"ok": True, "actor": "aion"})) as run:
            result = await routes.run_actor_once("aion")
        self.assertEqual("aion", result["actor"])
        run.assert_awaited_once_with("aion", manual=True)


if __name__ == "__main__":
    unittest.main()
