import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from routes import autonomy as autonomy_routes


class AutonomyNicheRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_niche_route_returns_only_requested_actor_cards(self):
        cards = [{"id": "card-1", "actor": "aion", "title": "小旅行"}]
        with patch.object(
            autonomy_routes, "list_niche_cards", new=AsyncMock(return_value=cards)
        ) as read_cards:
            payload = await autonomy_routes.read_niche_cards("aion", limit=12)

        self.assertEqual({"actor": "aion", "cards": cards}, payload)
        read_cards.assert_awaited_once_with("aion", limit=12)

    async def test_niche_route_rejects_unknown_actor(self):
        with self.assertRaises(HTTPException) as raised:
            await autonomy_routes.read_niche_cards("someone", limit=12)

        self.assertEqual(404, raised.exception.status_code)

    async def test_delete_niche_card_returns_not_found_when_card_is_absent(self):
        with patch.object(
            autonomy_routes, "delete_niche_card", new=AsyncMock(return_value=False)
        ) as delete_card:
            with self.assertRaises(HTTPException) as raised:
                await autonomy_routes.remove_niche_card("missing", actor="connor")

        self.assertEqual(404, raised.exception.status_code)
        delete_card.assert_awaited_once_with("connor", "missing")

    async def test_delete_niche_card_returns_success(self):
        with patch.object(
            autonomy_routes, "delete_niche_card", new=AsyncMock(return_value=True)
        ) as delete_card:
            payload = await autonomy_routes.remove_niche_card("card-1", actor="aion")

        self.assertEqual({"ok": True, "id": "card-1"}, payload)
        delete_card.assert_awaited_once_with("aion", "card-1")

    async def test_update_niche_card_mention_state(self):
        card = {"id": "card-1", "actor": "aion", "mentioned": True}
        with patch.object(
            autonomy_routes,
            "set_niche_card_mentioned",
            new=AsyncMock(return_value=card),
        ) as set_mentioned:
            payload = await autonomy_routes.update_niche_card(
                "card-1",
                actor="aion",
                body=autonomy_routes.NicheCardUpdate(mentioned=True),
            )

        self.assertEqual({"ok": True, "card": card}, payload)
        set_mentioned.assert_awaited_once_with("aion", "card-1", True)


if __name__ == "__main__":
    unittest.main()
