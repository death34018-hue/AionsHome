import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class OpenClawWeixinAdapterTests(unittest.TestCase):
    def _state_home(self, tmp: Path) -> Path:
        home = tmp / ".openclaw"
        accounts = home / "openclaw-weixin" / "accounts"
        accounts.mkdir(parents=True)
        (accounts / "bot-1.json").write_text(
            json.dumps({
                "userId": "owner@im.wechat",
                "baseUrl": "https://ilink.example.test",
                "token": "bot-token",
            }),
            encoding="utf-8",
        )
        (accounts / "bot-1.context-tokens.json").write_text(
            json.dumps({"friend@im.wechat": "ctx-token"}),
            encoding="utf-8",
        )
        (accounts / "bot-1.sync.json").write_text(
            json.dumps({"get_updates_buf": "sync-cursor"}),
            encoding="utf-8",
        )
        return home

    def test_load_accounts_uses_filename_account_id_and_context_tokens(self):
        from openclaw_weixin import load_accounts, load_context_tokens, select_account_for_recipient

        with tempfile.TemporaryDirectory() as td:
            home = self._state_home(Path(td))

            accounts = load_accounts(home)
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0].account_id, "bot-1")
            self.assertEqual(accounts[0].user_id, "owner@im.wechat")
            self.assertEqual(accounts[0].base_url, "https://ilink.example.test")
            self.assertEqual(accounts[0].token, "bot-token")

            self.assertEqual(load_context_tokens("bot-1", home), {"friend@im.wechat": "ctx-token"})
            account, context_token = select_account_for_recipient("friend@im.wechat", openclaw_home=home)
            self.assertEqual(account.account_id, "bot-1")
            self.assertEqual(context_token, "ctx-token")

    def test_build_text_message_body_matches_weixin_sendmessage_shape(self):
        from openclaw_weixin import build_text_message_body

        body = build_text_message_body(
            "friend@im.wechat",
            "hello from AionsHome",
            context_token="ctx-token",
            client_id="fixed-client-id",
        )

        self.assertEqual(body["msg"]["to_user_id"], "friend@im.wechat")
        self.assertEqual(body["msg"]["client_id"], "fixed-client-id")
        self.assertEqual(body["msg"]["message_type"], 2)
        self.assertEqual(body["msg"]["message_state"], 2)
        self.assertEqual(body["msg"]["context_token"], "ctx-token")
        self.assertEqual(body["msg"]["item_list"], [{"type": 1, "text_item": {"text": "hello from AionsHome"}}])

    def test_extract_text_from_message_ignores_non_text_items(self):
        from openclaw_weixin import extract_text_from_message

        message = {
            "item_list": [
                {"type": 2, "image_item": {"media": {"full_url": "https://example.test/img.png"}}},
                {"type": 1, "text_item": {"text": "first"}},
                {"type": 1, "text_item": {"text": "second"}},
            ]
        }

        self.assertEqual(extract_text_from_message(message), "first\nsecond")


class WeChatBindingTests(unittest.TestCase):
    def test_public_bindings_reports_context_age_and_state(self):
        from wechat_bridge import create_wechat_binding, public_wechat_bindings

        settings = {}
        create_wechat_binding(
            source_type="aion_private",
            source_id="conv-1",
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="ctx-token",
            settings=settings,
            now=1000,
        )

        rows = public_wechat_bindings(settings=settings, now=1310, stale_after_seconds=300)

        self.assertEqual(rows[0]["context_age_seconds"], 310)
        self.assertEqual(rows[0]["context_state"], "stale")

    def test_dispatch_records_openclaw_send_attempt_without_message_content(self):
        from wechat_bridge import create_wechat_binding, dispatch_wechat_message
        import config

        settings = {"wechat_bridge_enabled": True, "wechat_bridge_transport": "openclaw"}
        create_wechat_binding(
            source_type="aion_private",
            source_id="conv-1",
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="ctx-token",
            settings=settings,
            now=1000,
        )

        send_text = AsyncMock(return_value={"ok": True, "response": {"ret": 0}})
        with patch.object(config, "SETTINGS", settings), \
             patch.object(config, "save_settings"), \
             patch("wechat_bridge.time.time", return_value=1310), \
             patch("openclaw_weixin.send_text_message", send_text):
            result = asyncio.run(dispatch_wechat_message(
                content="secret reminder",
                source_type="aion_private",
                source_id="conv-1",
                sender="aion",
                source_msg_id="msg-1",
            ))

        self.assertTrue(result["ok"])
        last_send = settings["wechat_bridge_last_send"]
        self.assertTrue(last_send["ok"])
        self.assertEqual(last_send["context_age_seconds"], 310)
        self.assertEqual(last_send["content_len"], len("secret reminder"))
        self.assertNotIn("content", last_send)

    def test_create_binding_replaces_existing_sender_binding(self):
        from wechat_bridge import create_wechat_binding, find_wechat_binding_for_sender

        settings = {}
        create_wechat_binding(
            source_type="aion_private",
            source_id="conv-old",
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="old-token",
            settings=settings,
            now=1000,
        )
        create_wechat_binding(
            source_type="chatroom",
            source_id="room-new",
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="new-token",
            settings=settings,
            now=1010,
        )

        binding = find_wechat_binding_for_sender("bot-1", "friend@im.wechat", settings=settings)
        self.assertEqual(binding["source_type"], "chatroom")
        self.assertEqual(binding["source_id"], "room-new")
        self.assertEqual(list(settings["wechat_bridge_bindings"].keys()), ["chatroom:room-new"])

    def test_pending_binding_consumes_code_and_creates_binding(self):
        from wechat_bridge import (
            consume_wechat_binding_code,
            create_wechat_pending_binding,
            find_wechat_binding_for_route,
            find_wechat_binding_for_sender,
        )

        settings = {}
        pending = create_wechat_pending_binding(
            source_type="aion_private",
            source_id="conv-1",
            code="ABC123",
            now=1000,
            ttl_seconds=300,
            settings=settings,
        )
        self.assertEqual(pending["code"], "ABC123")

        binding = consume_wechat_binding_code(
            "ABC123",
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="ctx-token",
            now=1010,
            settings=settings,
        )

        self.assertEqual(binding["source_type"], "aion_private")
        self.assertEqual(binding["source_id"], "conv-1")
        self.assertEqual(binding["account_id"], "bot-1")
        self.assertEqual(binding["wechat_user_id"], "friend@im.wechat")
        self.assertEqual(binding["context_token"], "ctx-token")
        self.assertEqual(settings.get("wechat_bridge_pending_bindings"), {})
        self.assertEqual(find_wechat_binding_for_route("aion_private", "conv-1", settings=settings), binding)
        self.assertEqual(find_wechat_binding_for_sender("bot-1", "friend@im.wechat", settings=settings), binding)


class OpenClawRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_choose_latest_binding_route_prefers_most_recent_chatroom_or_private(self):
        from wechat_openclaw_runtime import choose_latest_binding_route

        self.assertEqual(
            choose_latest_binding_route(
                private_id="conv-old",
                private_updated_at=100,
                room_id="room-new",
                room_updated_at=200,
            ),
            {"source_type": "chatroom", "source_id": "room-new"},
        )
        self.assertEqual(
            choose_latest_binding_route(
                private_id="conv-new",
                private_updated_at=300,
                room_id="room-old",
                room_updated_at=200,
            ),
            {"source_type": "aion_private", "source_id": "conv-new"},
        )

    async def test_handle_binding_message_consumes_code_and_sends_confirmation(self):
        from wechat_openclaw_runtime import OpenClawWeixinBridgeRuntime
        from wechat_bridge import create_wechat_pending_binding, find_wechat_binding_for_sender

        settings = {"wechat_bridge_enabled": True, "wechat_bridge_transport": "openclaw"}
        create_wechat_pending_binding(
            source_type="aion_private",
            source_id="conv-1",
            code="ABC123",
            now=1000,
            ttl_seconds=300,
            settings=settings,
        )
        sent = []
        runtime = OpenClawWeixinBridgeRuntime(
            settings=settings,
            save_settings=lambda data: None,
            inbound_handler=AsyncMock(),
            send_text=lambda **kwargs: sent.append(kwargs),
            now=lambda: 1010,
        )

        handled = await runtime.handle_text_message(
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="ctx-token",
            text="绑定 ABC123",
        )

        self.assertTrue(handled)
        self.assertEqual(runtime.inbound_handler.await_count, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("bound", sent[0]["content"].lower())
        binding = find_wechat_binding_for_sender("bot-1", "friend@im.wechat", settings=settings)
        self.assertEqual(binding["source_id"], "conv-1")

    async def test_handle_default_binding_message_uses_default_route_without_manual_id(self):
        from wechat_openclaw_runtime import OpenClawWeixinBridgeRuntime
        from wechat_bridge import find_wechat_binding_for_sender

        settings = {"wechat_bridge_enabled": True, "wechat_bridge_transport": "openclaw"}
        sent = []
        runtime = OpenClawWeixinBridgeRuntime(
            settings=settings,
            save_settings=lambda data: None,
            inbound_handler=AsyncMock(),
            send_text=lambda **kwargs: sent.append(kwargs),
            default_route_resolver=AsyncMock(return_value={
                "source_type": "aion_private",
                "source_id": "conv-default",
            }),
            now=lambda: 1010,
        )

        handled = await runtime.handle_text_message(
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="ctx-token",
            text="绑定 AionsHome",
        )

        self.assertTrue(handled)
        self.assertEqual(runtime.inbound_handler.await_count, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("bound", sent[0]["content"].lower())
        binding = find_wechat_binding_for_sender("bot-1", "friend@im.wechat", settings=settings)
        self.assertEqual(binding["source_type"], "aion_private")
        self.assertEqual(binding["source_id"], "conv-default")

    async def test_handle_bound_message_routes_to_existing_wechat_inbound_handler(self):
        from wechat_openclaw_runtime import OpenClawWeixinBridgeRuntime
        from wechat_bridge import create_wechat_binding

        settings = {"wechat_bridge_enabled": True, "wechat_bridge_transport": "openclaw"}
        create_wechat_binding(
            source_type="aion_private",
            source_id="conv-1",
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="old-token",
            settings=settings,
            now=1000,
        )
        inbound = AsyncMock()
        runtime = OpenClawWeixinBridgeRuntime(
            settings=settings,
            save_settings=lambda data: None,
            inbound_handler=inbound,
            send_text=AsyncMock(),
            now=lambda: 1010,
        )

        handled = await runtime.handle_text_message(
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="new-token",
            text="from wechat",
        )

        self.assertTrue(handled)
        inbound.assert_awaited_once()
        payload = inbound.await_args.kwargs
        self.assertEqual(payload["content"], "from wechat")
        self.assertEqual(payload["source_type"], "aion_private")
        self.assertEqual(payload["source_id"], "conv-1")
        self.assertTrue(payload["auto_reply"])
        binding = settings["wechat_bridge_bindings"]["aion_private:conv-1"]
        self.assertEqual(binding["context_token"], "new-token")

    async def test_mode_enable_is_consumed_without_calling_ai(self):
        from wechat_bridge import create_wechat_binding
        from wechat_mode import find_wechat_mode_for_sender
        from wechat_openclaw_runtime import OpenClawWeixinBridgeRuntime

        settings = {"wechat_bridge_enabled": True, "wechat_bridge_transport": "openclaw"}
        create_wechat_binding(
            source_type="chatroom",
            source_id="room-1",
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="ctx",
            settings=settings,
        )
        inbound = AsyncMock()
        sent = []
        runtime = OpenClawWeixinBridgeRuntime(
            settings=settings,
            save_settings=lambda value: None,
            inbound_handler=inbound,
            send_text=lambda **kwargs: sent.append(kwargs),
            private_route_resolver=AsyncMock(return_value={
                "source_type": "aion_private",
                "source_id": "conv-1",
            }),
            now=lambda: 100,
        )

        handled = await runtime.handle_text_message(
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="ctx-new",
            text="［微信模式开启］",
        )

        self.assertTrue(handled)
        inbound.assert_not_awaited()
        self.assertEqual([item["content"] for item in sent], ["微信模式已开启。"])
        mode = find_wechat_mode_for_sender(settings, "bot-1", "friend@im.wechat")
        self.assertEqual(mode["inbound_route"], {
            "source_type": "chatroom",
            "source_id": "room-1",
        })
        self.assertEqual(mode["outbound_routes"], [
            {"source_type": "chatroom", "source_id": "room-1"},
            {"source_type": "aion_private", "source_id": "conv-1"},
        ])

    async def test_normal_message_in_enabled_mode_keeps_bound_route_and_is_plain(self):
        from wechat_bridge import create_wechat_binding
        from wechat_mode import set_wechat_mode
        from wechat_openclaw_runtime import OpenClawWeixinBridgeRuntime

        settings = {"wechat_bridge_enabled": True, "wechat_bridge_transport": "openclaw"}
        create_wechat_binding(
            source_type="chatroom",
            source_id="room-1",
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="ctx",
            settings=settings,
        )
        set_wechat_mode(
            settings,
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            inbound_route={"source_type": "chatroom", "source_id": "room-1"},
            outbound_routes=[{"source_type": "chatroom", "source_id": "room-1"}],
            enabled=True,
            now=100,
        )
        inbound = AsyncMock()
        runtime = OpenClawWeixinBridgeRuntime(
            settings=settings,
            save_settings=lambda value: None,
            inbound_handler=inbound,
            now=lambda: 110,
        )

        await runtime.handle_text_message(
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="ctx-new",
            text="下一条正常消息",
        )

        inbound.assert_awaited_once_with(
            content="下一条正常消息",
            source_type="chatroom",
            source_id="room-1",
            auto_reply=True,
            mark_channel=False,
        )

    async def test_mode_disable_is_idempotent_and_never_calls_ai(self):
        from wechat_bridge import create_wechat_binding
        from wechat_mode import find_wechat_mode_for_sender, set_wechat_mode
        from wechat_openclaw_runtime import OpenClawWeixinBridgeRuntime

        settings = {"wechat_bridge_enabled": True, "wechat_bridge_transport": "openclaw"}
        create_wechat_binding(
            source_type="chatroom",
            source_id="room-1",
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            context_token="ctx",
            settings=settings,
        )
        set_wechat_mode(
            settings,
            account_id="bot-1",
            wechat_user_id="friend@im.wechat",
            inbound_route={"source_type": "chatroom", "source_id": "room-1"},
            outbound_routes=[{"source_type": "chatroom", "source_id": "room-1"}],
            enabled=True,
            now=100,
        )
        inbound = AsyncMock()
        sent = []
        runtime = OpenClawWeixinBridgeRuntime(
            settings=settings,
            save_settings=lambda value: None,
            inbound_handler=inbound,
            send_text=lambda **kwargs: sent.append(kwargs["content"]),
            now=lambda: 110,
        )

        for _ in range(2):
            await runtime.handle_text_message(
                account_id="bot-1",
                wechat_user_id="friend@im.wechat",
                context_token="ctx-new",
                text="[微信模式关闭]",
            )

        inbound.assert_not_awaited()
        self.assertEqual(sent, ["微信模式已关闭。", "微信模式已关闭。"])
        self.assertFalse(
            find_wechat_mode_for_sender(settings, "bot-1", "friend@im.wechat")["enabled"]
        )


if __name__ == "__main__":
    unittest.main()
