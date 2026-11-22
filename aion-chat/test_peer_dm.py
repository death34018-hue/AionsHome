import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
from fastapi import FastAPI

import peer_dm


class PeerConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "chat.db"
        self.enterContext(patch.object(peer_dm, "get_db", lambda: aiosqlite.connect(self.path)))
        self.enterContext(patch.object(peer_dm, "_notify", new_callable=AsyncMock))
        self.enterContext(patch("chatroom.connor_1v1_on_message"))
        async with aiosqlite.connect(self.path) as db:
            await db.executescript("""
                CREATE TABLE chatroom_rooms (id TEXT PRIMARY KEY, title TEXT, type TEXT,
                    created_at REAL, updated_at REAL);
                CREATE TABLE chatroom_messages (id TEXT PRIMARY KEY, sender TEXT, room_id TEXT,
                    content TEXT, created_at REAL);
                CREATE TABLE chatroom_digest_anchors (room_id TEXT PRIMARY KEY, anchor_ts REAL);
            """)
            await peer_dm.init_storage(db)
            await db.commit()

    async def messages(self):
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall("SELECT sender,content FROM chatroom_messages ORDER BY rowid")
            return rows

    async def test_opener_counts_towards_eight_rounds_and_replays_only_once(self):
        with patch.object(peer_dm, "_next_reply", AsyncMock(return_value=("接着聊", False))):
            await asyncio.gather(*(peer_dm.start_peer_dm("aion", "先商量一下", trigger_id="group:one") for _ in range(2)))
            await peer_dm.wait_for_replies()
            await peer_dm.resume_pending()
            await peer_dm.wait_for_replies()
        rows = await self.messages()
        self.assertEqual([actor for actor, _ in rows], ["aion", "connor"] * 8)
        self.assertEqual(rows[0][1], "先商量一下")

    async def test_either_actor_can_finish_with_words_or_silently(self):
        with patch.object(peer_dm, "_next_reply", AsyncMock(side_effect=[
            ("蛋糕我来订", False), ("那我准备卡片，就这么定了", True), ("", True),
        ])):
            await peer_dm.start_peer_dm("connor", "生日怎么安排？", trigger_id="night:one")
            await peer_dm.wait_for_replies()
            await peer_dm.start_peer_dm("aion", "我已订好蛋糕", trigger_id="night:two")
            await peer_dm.wait_for_replies()
        self.assertEqual(await self.messages(), [
            ("connor", "生日怎么安排？"), ("aion", "蛋糕我来订"), ("connor", "那我准备卡片，就这么定了"),
            ("aion", "我已订好蛋糕"),
        ])

    async def test_pending_turn_resumes_with_a_real_room_and_read_only_http_history(self):
        from routes import chatroom as routes
        self.enterContext(patch.object(routes, "get_db", peer_dm.get_db))
        with patch.object(peer_dm, "_schedule"):
            await peer_dm.start_peer_dm("aion", "你挑礼物好吗？", trigger_id="patrol:one")
        seen = []
        from system_logs import logged_ai_call, list_logs

        @logged_ai_call("test")
        async def model(messages, model_key=None, meta=None):
            seen.append(messages[-1]["content"])
            yield json.dumps({"message": "好，我来挑。[DRAW:蓝色封面]", "end": True}, ensure_ascii=False)

        with (patch("autonomy._actor_context", AsyncMock(return_value=[])),
              patch("context_builder.fetch_merged_timeline", AsyncMock(return_value=[])),
              patch("chatroom.recall_chatroom_memories", AsyncMock(return_value=[])),
              patch.object(routes, "_stream_connor_model", model)):
            await peer_dm.resume_pending()
            await peer_dm.wait_for_replies()
        self.assertEqual(len(seen), 1)
        self.assertIn("觉得话题结束了，就主动结束这一轮私聊", seen[0])
        self.assertIn("你挑礼物好吗？", seen[0])
        entry, = list_logs(peer_dm.ROOM_ID)
        self.assertEqual(entry["actor"], "connor")
        self.assertEqual(entry["prompt_messages"][-1]["content"], seen[0])

        app = FastAPI()
        app.include_router(routes.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = "/api/chatroom/rooms/cr_peer_dm"
            rows = (await client.get(path + "/messages")).json()
            self.assertEqual([row["content"] for row in rows], ["你挑礼物好吗？", "好，我来挑。[DRAW:蓝色封面]"])
            self.assertEqual((await client.post(path + "/send", json={"content": "插话"})).status_code, 403)
            self.assertEqual((await client.delete(f"/api/chatroom/messages/{rows[0]['id']}")).status_code, 403)
            self.assertEqual((await client.delete(path)).status_code, 403)
