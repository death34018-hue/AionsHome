import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _FakeDb:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, params=()):
        self.executed.append((sql, params))

    async def commit(self):
        pass


class _FakeDbContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class ChatroomWeChatSystemDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_wechat_system_message_is_broadcast_for_realtime_frontend_display(self):
        import routes.chatroom as chatroom_routes

        fake_db = _FakeDb()
        queue = asyncio.Queue()
        broadcast = AsyncMock()

        with patch.object(chatroom_routes, "get_db", return_value=_FakeDbContext(fake_db)), \
             patch.object(chatroom_routes.manager, "broadcast", broadcast):
            await chatroom_routes._chatroom_sys_msg(
                "room-1",
                "本条为微信消息：Companion：你看到的话回我一下",
                queue,
                after_msg_id="msg-1",
            )

        queued = await queue.get()
        self.assertEqual(queued["type"], "system_msg")
        self.assertEqual(queued["message"]["sender"], "system")
        self.assertEqual(queued["message"]["content"], "本条为微信消息：Companion：你看到的话回我一下")
        broadcast.assert_awaited_once()
        event = broadcast.await_args.args[0]
        self.assertEqual(event["type"], "chatroom_msg_created")
        self.assertEqual(event["data"]["room_id"], "room-1")
        self.assertEqual(event["data"]["sender"], "system")


class ChatroomFrontendSystemDisplayTests(unittest.TestCase):
    def _css_block(self, css, marker):
        start = css.index(marker)
        open_pos = css.index("{", start)
        depth = 0
        for i in range(open_pos, len(css)):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    return css[open_pos + 1:i]
        self.fail(f"CSS block for {marker!r} was not closed")

    def test_streamed_system_messages_are_deduped_by_message_id(self):
        js = (ROOT / "static" / "chatroom.js").read_text(encoding="utf-8")
        marker = "case 'system_msg':"
        start = js.index(marker)
        end = js.index("case 'memory_record':", start)
        branch = js[start:end]

        self.assertIn("data.message.id", branch)
        self.assertIn('data-msg-id="${data.message.id}"', branch)
        self.assertIn("appendMessage(data.message)", branch)

    def test_mobile_system_notices_use_full_chat_width(self):
        css = (ROOT / "static" / "chatroom.css").read_text(encoding="utf-8")
        mobile = self._css_block(css, "@media (max-width: 700px) {\n  .app {")

        self.assertIn("  .system-event-msg {", mobile)
        system_notice = self._css_block(mobile, ".system-event-msg")
        self.assertIn("box-sizing: border-box;", system_notice)
        self.assertIn("align-self: stretch;", system_notice)
        self.assertIn("width: 100%;", system_notice)
        self.assertIn("max-width: none;", system_notice)
        self.assertIn("margin: 4px 0;", system_notice)
        self.assertIn("padding: 6px 4px;", system_notice)

    def test_desktop_system_notices_match_message_row_width(self):
        css = (ROOT / "static" / "chatroom.css").read_text(encoding="utf-8")
        html = (ROOT / "static" / "chatroom.html").read_text(encoding="utf-8")
        system_notice = self._css_block(css, "\n.system-event-msg {\n")

        self.assertIn("box-sizing: border-box;", system_notice)
        self.assertIn("width: min(85%, calc(100% - 16px));", system_notice)
        self.assertIn("margin: 4px auto;", system_notice)
        self.assertIn("flex: none;", css)
        self.assertIn("position: absolute;", css)
        self.assertIn("overflow-wrap: anywhere;", css)
        self.assertIn("chatroom.css?v=system-event-mobile-width-20260706b", html)


if __name__ == "__main__":
    unittest.main()
