import unittest
from unittest.mock import AsyncMock, patch

from routes import chatroom as chatroom_routes


class ChatroomVoiceConnorModelTests(unittest.IsolatedAsyncioTestCase):
    async def _capture_voice_route_model(self, room_type: str) -> str:
        captured = []
        body = chatroom_routes.MsgSend(
            content="语音转写",
            voice_attachments=[{
                "type": "voice",
                "url": "/uploads/voice.webm",
                "duration": 2.0,
                "transcript": "语音转写",
            }],
        )

        async def fake_save_msg(*args, **kwargs):
            return {"id": "user-voice", "duplicate": False}

        async def fake_load_room_and_messages(*args, **kwargs):
            return (
                {
                    "id": "room-voice",
                    "type": room_type,
                    "context_minutes": 30,
                },
                [{
                    "id": "user-voice",
                    "sender": "user",
                    "content": "语音转写",
                    "attachments": body.voice_attachments,
                }],
            )

        async def fake_private_reply(*args, connor_model_key, **kwargs):
            captured.append(connor_model_key)

        async def fake_group_replies(
            room_id,
            room,
            msgs,
            model_key,
            connor_model_key,
            queue,
            context_limit,
            **kwargs,
        ):
            captured.append(connor_model_key)

        with (
            patch.object(chatroom_routes, "_save_msg", new=fake_save_msg),
            patch.object(
                chatroom_routes,
                "_load_room_and_messages",
                new=fake_load_room_and_messages,
            ),
            patch.object(
                chatroom_routes,
                "record_chatroom_active",
                new=AsyncMock(),
            ),
            patch.object(
                chatroom_routes,
                "load_chatroom_config",
                return_value={
                    "connor_model": "Codex-Sol",
                    "reply_order": "random",
                },
            ),
            patch.object(
                chatroom_routes,
                "_generate_connor_reply",
                new=fake_private_reply,
            ),
            patch.object(
                chatroom_routes,
                "_generate_group_replies",
                new=fake_group_replies,
            ),
            patch.object(chatroom_routes.cam, "reset_patrol_timer"),
        ):
            response = await chatroom_routes.send_message("room-voice", body)
            async for _chunk in response.body_iterator:
                pass

        self.assertEqual(captured, ["Codex-Sol"])
        return captured[0]

    async def test_group_voice_uses_configured_connor_model_when_payload_omits_it(self):
        await self._capture_voice_route_model("group")

    async def test_private_voice_uses_configured_connor_model_when_payload_omits_it(self):
        await self._capture_voice_route_model("connor_1v1")

    def test_removed_legacy_codex_alias_falls_back_to_configured_model(self):
        with (
            patch.object(
                chatroom_routes,
                "MODELS",
                {"Codex-Sol": {"provider": "codex_cli", "model": "gpt-5.6-sol"}},
            ),
            patch.object(
                chatroom_routes,
                "load_chatroom_config",
                return_value={"connor_model": "Codex-Sol"},
            ),
        ):
            resolved = chatroom_routes._resolve_connor_model("Codex")

        self.assertEqual(resolved, "Codex-Sol")

    def test_removed_legacy_codex_config_falls_back_to_available_cli_model(self):
        with (
            patch.object(
                chatroom_routes,
                "MODELS",
                {"Codex-Sol": {"provider": "codex_cli", "model": "gpt-5.6-sol"}},
            ),
            patch.object(
                chatroom_routes,
                "load_chatroom_config",
                return_value={"connor_model": "Codex"},
            ),
        ):
            resolved = chatroom_routes._resolve_connor_model(None)

        self.assertEqual(resolved, "Codex-Sol")


if __name__ == "__main__":
    unittest.main()
