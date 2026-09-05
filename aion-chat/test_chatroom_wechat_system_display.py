import asyncio
import json
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
        self.assertIn(
            {"type": "system_model_context"},
            queued["message"]["attachments"],
        )
        stored_attachments = json.loads(fake_db.executed[0][1][-1])
        self.assertIn({"type": "system_model_context"}, stored_attachments)
        self.assertIn(
            {"type": "system_notice_order", "after_msg_id": "msg-1"},
            stored_attachments,
        )
        broadcast.assert_awaited_once()
        event = broadcast.await_args.args[0]
        self.assertEqual(event["type"], "chatroom_msg_created")
        self.assertEqual(event["data"]["room_id"], "room-1")
        self.assertEqual(event["data"]["sender"], "system")

    async def test_background_tool_system_message_is_marked_for_model_context(self):
        import schedule

        fake_db = _FakeDb()
        broadcast = AsyncMock()
        with patch.object(schedule, "get_db", return_value=_FakeDbContext(fake_db)), \
             patch.object(schedule.manager, "broadcast", broadcast):
            await schedule._chatroom_sys_msg(
                "room-1",
                "【Partner X】锁定了小红书 60 分钟",
                after_msg_id="cm-source",
            )

        stored_attachments = json.loads(fake_db.executed[0][1][-1])
        self.assertIn({"type": "system_model_context"}, stored_attachments)
        self.assertIn(
            {"type": "system_notice_order", "after_msg_id": "cm-source"},
            stored_attachments,
        )
        event = broadcast.await_args.args[0]
        self.assertIn(
            {"type": "system_model_context"},
            event["data"]["attachments"],
        )


class ChatroomSystemModelContextTests(unittest.TestCase):
    def render_text(self, message):
        from context_builder import render_merged_timeline

        rendered = render_merged_timeline([message], "connor")
        return "\n".join(str(item.get("content") or "") for item in rendered)

    def test_structured_tool_result_enters_next_participant_context(self):
        text = self.render_text({
            "source": "group",
            "sender": "system",
            "content": "【Partner X】锁定了小红书 60 分钟",
            "created_at": 1_700_000_000,
            "attachments": [{"type": "system_model_context"}],
        })
        self.assertIn("系统事件", text)
        self.assertIn("锁定了小红书 60 分钟", text)

    def test_unmarked_unrelated_system_noise_stays_out_of_context(self):
        text = self.render_text({
            "source": "group",
            "sender": "system",
            "content": "内部连接诊断已刷新",
            "created_at": 1_700_000_000,
            "attachments": [],
        })
        self.assertNotIn("内部连接诊断已刷新", text)

    def test_legacy_keyword_system_message_remains_visible(self):
        text = self.render_text({
            "source": "group",
            "sender": "system",
            "content": "Partner X 点了一首歌",
            "created_at": 1_700_000_000,
            "attachments": [],
        })
        self.assertIn("系统事件", text)
        self.assertIn("点了一首歌", text)


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

    def test_mobile_system_notices_align_with_ai_bubble_edge(self):
        css = (ROOT / "static" / "chatroom.css").read_text(encoding="utf-8")
        mobile = self._css_block(css, "@media (max-width: 700px) {\n  .app {")

        self.assertIn("  .system-event-msg {", mobile)
        system_notice = self._css_block(mobile, ".system-event-msg")
        self.assertIn("box-sizing: border-box;", system_notice)
        self.assertIn("align-self: flex-start;", system_notice)
        self.assertIn("width: calc(100% - var(--system-notice-left-offset));", system_notice)
        self.assertIn("max-width: calc(100% - var(--system-notice-left-offset));", system_notice)
        self.assertIn("margin-left: var(--system-notice-left-offset);", system_notice)
        self.assertIn("padding: 6px 4px;", system_notice)

    def test_desktop_system_notices_align_with_ai_bubble_edge(self):
        css = (ROOT / "static" / "chatroom.css").read_text(encoding="utf-8")
        html = (ROOT / "static" / "chatroom.html").read_text(encoding="utf-8")
        system_notice = self._css_block(css, "\n.system-event-msg {\n")

        self.assertIn("box-sizing: border-box;", system_notice)
        self.assertIn("width: min(85%, calc(100% - var(--system-notice-left-offset) - 8px));", system_notice)
        self.assertIn("margin-left: var(--system-notice-left-offset);", system_notice)
        self.assertIn("flex: 1;", css)
        self.assertIn("position: absolute;", css)
        self.assertIn("overflow-wrap: anywhere;", css)
        self.assertRegex(html, r'chatroom\.css\?v=[^"\s]+')


if __name__ == "__main__":
    unittest.main()
