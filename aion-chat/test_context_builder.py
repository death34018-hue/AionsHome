import json
import sys
import re
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_builder import render_merged_timeline


class TransferContextTest(unittest.TestCase):
    def test_transfer_history_identifies_both_parties_in_private_and_group_chats(self):
        cases = [
            ("aion", "private", "assistant", "[转账：52.50元]", "「Aion」给「Ithil」转账 52.50元"),
            ("aion", "private", "user", "[转账：20元]", "「Ithil」给「Aion」转账 20元"),
            ("connor", "private", "connor", "[转账：30元]", "「Connor」给「Ithil」转账 30元"),
            ("connor", "private", "user", "[转账：40元]", "「Ithil」给「Connor」转账 40元"),
            ("connor", "group", "aion", "[转账：50元]", "「Aion」给「Ithil」转账 50元"),
            ("aion", "group", "connor", "[转账：60元]", "「Connor」给「Ithil」转账 60元"),
            ("connor", "group", "user", "[转账给 Aion ：70.25元]", "「Ithil」给「Aion」转账 70.25元"),
            ("aion", "group", "user", "[转账给Connor: 80元]", "「Ithil」给「Connor」转账 80元"),
            ("aion", "group", "user", "[转账：90元]", "「Ithil」给「Connor」转账 90元"),
            ("connor", "group", "user", "[转账：90元]", "「Ithil」给「Connor」转账 90元"),
        ]
        with (
            patch("context_builder.load_worldbook", return_value={"ai_name": "Aion", "user_name": "Ithil"}),
            patch("chatroom.load_chatroom_config", return_value={"connor_name": "Connor"}),
        ):
            for who, source, sender, tag, expected in cases:
                with self.subTest(who=who, source=source, sender=sender, tag=tag):
                    message = {"source": source, "sender": sender, "content": f"零花钱~{tag}", "created_at": 1}
                    rendered = render_merged_timeline([message], who)[-1]["content"]
                    self.assertIn(expected, rendered)
                    self.assertIn("零花钱~", rendered)
                    self.assertIn("<meta>发送时间：", rendered)
                    self.assertEqual(message["content"], f"零花钱~{tag}")

    def test_transfer_uses_configured_names_and_preserves_other_wallet_markers(self):
        with (
            patch("context_builder.load_worldbook", return_value={"ai_name": "星星", "user_name": "月亮"}),
            patch("chatroom.load_chatroom_config", return_value={"connor_name": "小熊"}),
        ):
            rendered = render_merged_timeline([
                {"source": "group", "sender": "user", "content": "[转账给星星：12.34元][转账给小熊：5元][转账：-3元][转账：0元]"},
            ], "connor")[-1]["content"]
        self.assertIn("「月亮」给「星星」转账 12.34元", rendered)
        self.assertIn("「月亮」给「小熊」转账 5元", rendered)
        self.assertIn("[转账：-3元]", rendered)
        self.assertIn("[转账：0元]", rendered)


def test_lounge_report_summary_remains_visible_in_later_model_timeline():
    summary = "刚才去朋友家聊了养花，对方最近在种薄荷。"
    rendered = render_merged_timeline(
        [
            {
                "source": "group",
                "sender": "aion",
                "content": summary,
                "created_at": 1786369912.0,
                "attachments": json.dumps(
                    [
                        {
                            "type": "lounge_visit_report",
                            "direction": "outbound",
                            "partner_name": "朋友",
                            "summary": summary,
                        }
                    ],
                    ensure_ascii=False,
                ),
            }
        ],
        "aion",
    )

    assert any(summary in item["content"] for item in rendered)


class GroupWindowContinuityTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_group_uses_global_timeline_and_configured_limit(self):
        import chatroom
        import context_builder

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "timeline.db"

            def connect():
                return aiosqlite.connect(db_path)

            async with connect() as db:
                await db.executescript("""
                    CREATE TABLE messages (
                        id TEXT, conv_id TEXT, role TEXT, content TEXT,
                        created_at REAL, attachments TEXT
                    );
                    CREATE TABLE chatroom_rooms (id TEXT, type TEXT);
                    CREATE TABLE chatroom_messages (
                        id TEXT, room_id TEXT, sender TEXT, content TEXT,
                        created_at REAL, attachments TEXT
                    );
                    INSERT INTO chatroom_rooms VALUES
                        ('old-group', 'group'), ('new-group', 'group'),
                        ('private-room', 'connor_1v1');
                """)
                # Insert out of time order; include private rows for each actor.
                for i in reversed(range(1, 41)):
                    content = f"timeline-row-{i:02d}"
                    if i % 5 == 0 and i != 40:
                        await db.execute(
                            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                            (str(i), "private-conv", "user", content, i, "[]"),
                        )
                        room_id = "private-room"
                    else:
                        room_id = "new-group" if i == 40 else "old-group"
                    await db.execute(
                        "INSERT INTO chatroom_messages VALUES (?, ?, ?, ?, ?, ?)",
                        (str(i), room_id, "user", content, i, "[]"),
                    )
                await db.commit()

            with (
                patch.object(context_builder, "get_db", connect),
                patch.object(chatroom, "load_worldbook", return_value={}),
                patch.object(chatroom, "_read_connor_persona", return_value=""),
                patch.object(chatroom, "build_ability_block", new=AsyncMock(return_value="")),
                patch.object(chatroom, "with_current_device_context", side_effect=lambda history, **kw: history),
                patch.object(chatroom, "build_memory_blocks", new=AsyncMock(return_value={
                    "time_block": "test time", "memory_block": "", "digest_result": {},
                })),
            ):
                for build in (chatroom.build_aion_group_context, chatroom.build_connor_group_context,
                              chatroom.build_connor_1v1_context):
                    for limit in (4, 32):
                        with self.subTest(actor=build.__name__, limit=limit):
                            history, _ = await build(
                                "new-group", [], context_limit=limit,
                                query_text="Continue the previous conversation",
                            )
                            actual = [
                                int(match)
                                for message in history
                                for match in re.findall(r"timeline-row-(\d+)", message["content"])
                            ]
                            self.assertEqual(list(range(41 - limit, 41)), actual)


class BoundedTimelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_paging_keeps_visible_system_events_and_reads_only_recent_rows(self):
        import context_builder as cb

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'timeline.db'
            async with aiosqlite.connect(path) as db:
                await db.executescript('''
                    CREATE TABLE messages (id TEXT, conv_id TEXT, role TEXT, content TEXT, created_at REAL, attachments TEXT);
                    CREATE TABLE chatroom_rooms (id TEXT, type TEXT);
                    CREATE TABLE chatroom_messages (id TEXT, room_id TEXT, sender TEXT, content TEXT, created_at REAL, attachments TEXT);
                    INSERT INTO chatroom_rooms VALUES ('old','group'),('new','group'),('private','connor_1v1');
                ''')
                await db.executemany('INSERT INTO chatroom_messages VALUES (?,?,?,?,?,?)',
                    [(str(i), 'old', 'user', f'old-{i}', i, '[]') for i in range(1000)])
                # Several pages of invisible notices, including equal timestamps.
                await db.executemany('INSERT INTO chatroom_messages VALUES (?,?,?,?,?,?)',
                    [(f'h{i}', 'new', 'system', 'UI only', 2000, '[]') for i in range(150)])
                events = [('pat', '拍一拍', '[{"type":"system_model_context"}]'),
                          ('supervision', '查看了动态：今天正常', '[]'),
                          ('web', '搜索了天气', '[]'), ('music', '点了一首歌', '[]')]
                for i, (msg_id, content, attachments) in enumerate(events):
                    await db.execute('INSERT INTO chatroom_messages VALUES (?,?,?,?,?,?)',
                                     (msg_id, 'old', 'system', content, 1500 + i, attachments))
                await db.execute("INSERT INTO messages VALUES ('p','conv','user','private',1504,'[]')")
                await db.execute("INSERT INTO chatroom_messages VALUES ('c','private','user','private',1504,'[]')")
                await db.commit()

            fetched = []
            @asynccontextmanager
            async def connect():
                async with aiosqlite.connect(path) as db:
                    execute = db.execute
                    async def counted(sql, params=()):
                        cur = await execute(sql, params)
                        original = cur.fetchall
                        async def fetchall():
                            rows = await original()
                            fetched.append(len(rows))
                            return rows
                        cur.fetchall = fetchall
                        return cur
                    db.execute = counted
                    yield db

            with patch.object(cb, 'get_db', connect), patch.object(cb, 'load_trip_products', AsyncMock(return_value={})):
                for who, private_id in [('aion', 'p'), ('connor', 'c')]:
                    fetched.clear()
                    result = await cb.fetch_merged_timeline(who, 6)
                    self.assertEqual([m['id'] for m in result], ['999','pat','supervision','web','music',private_id])
                    self.assertLess(sum(fetched), 400, 'Recent context must not materialize all old history')
                    # Bounds and room filters retain their existing meaning.
                    bounded = await cb.fetch_merged_timeline(who, 30, room_id='old', since_ts=1501, until_ts=1503)
                    self.assertEqual([m['id'] for m in bounded], ['supervision','web','music'])
                    ties = await cb.fetch_merged_timeline(who, 70, until_ts=999)
                    self.assertEqual([m['id'] for m in ties], [str(i) for i in range(930,1000)])
                async with aiosqlite.connect(path) as db:
                    await db.executemany('INSERT INTO chatroom_messages VALUES (?,?,?,?,?,?)',
                        [(f'tie{i}', 'new', 'user', 'same instant', 3000, '[]') for i in range(100)])
                    await db.commit()
                # More equal-time visible rows than one page: no skips or duplicates.
                tied = await cb.fetch_merged_timeline('connor', 90)
                self.assertEqual([m['id'] for m in tied], [f'tie{i}' for i in range(10,100)])


class FamilyDynamicsTimelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_dynamics_between_first_and_last_message_join_the_timeline(self):
        import context_builder as cb

        merged = [
            {"id": "m1", "source": "private", "sender": "user", "content": "早", "created_at": 100.0},
            {"id": "m2", "source": "group", "sender": "connor", "content": "来了", "created_at": 200.0},
            {"id": "m3", "source": "group", "sender": "user", "content": "在吗", "created_at": 300.0},
        ]
        dynamics = AsyncMock(return_value=[
            {"id": "family_old", "timestamp": 50.0, "kind": "diary", "title": "旧日记"},
            {"id": "family_moment_1", "timestamp": 150.0, "kind": "moment", "title": "Connor发布了朋友圈"},
            {"id": "family_idle_1", "timestamp": 250.0, "kind": "idle_event", "title": "Aion 逛了小红书"},
            {"id": "family_late", "timestamp": 300.0, "kind": "gift", "title": "晚到的礼物"},
        ])
        with patch.object(cb, "family_dynamics_since", dynamics):
            result = await cb.with_family_dynamics(merged)
            single = merged[:1]
            self.assertIs(await cb.with_family_dynamics(single), single)
        dynamics.assert_awaited_once_with(100.0, 300.0)
        self.assertEqual(
            [m["id"] for m in result],
            ["m1", "family_moment_1", "m2", "family_idle_1", "m3"],
        )

        with (
            patch("context_builder.load_worldbook", return_value={"ai_name": "Aion", "user_name": "Ithil"}),
            patch("chatroom.load_chatroom_config", return_value={"connor_name": "Connor"}),
        ):
            history = cb.render_merged_timeline(result, "aion")[1:]
        self.assertTrue(history[1]["content"].startswith("历史消息 - 家庭动态：Connor发布了朋友圈\n<meta>发送时间："))
        self.assertNotIn("[", history[1]["content"].split("<meta>")[1])
        self.assertTrue(history[1]["_family_dynamic"])
        self.assertTrue(history[3]["content"].startswith("历史消息 - 家庭动态：Aion 逛了小红书\n"))
        # 场景切换提示跳过家庭动态，落在真正切换场景的那条消息上。
        self.assertTrue(history[2]["content"].startswith("（以下切换到群聊场景）\n历史消息 - Connor：来了"))
        self.assertTrue(history[4]["content"].startswith("当前用户消息 - Ithil：在吗"))
        self.assertEqual([bool(h.get("_family_dynamic")) for h in history], [False, True, False, True, False])

    async def test_dynamics_keep_titles_only_and_skip_spoken_messages(self):
        import context_builder as cb

        # 聚合函数按时间倒序返回。
        newest_first = [
            {"timestamp": 48.0, "kind": "idle_event", "actor": "Aion", "title": "Aion去网上冲浪了",
             "detail": "搜索了…", "source_id": "i6", "action": "web_roam"},
            {"timestamp": 46.0, "kind": "idle_event", "actor": "Connor", "title": "Ithil & Connor打完了一局台球",
             "detail": "", "source_id": "i5", "action": "billiards_result"},
            {"timestamp": 44.0, "kind": "idle_event", "actor": "Connor", "title": "Connor和另一位家庭成员聊了聊",
             "detail": "", "source_id": "i4", "action": "role_chat"},
            {"timestamp": 42.0, "kind": "idle_event", "actor": "Connor", "title": "Connor读了《《太阳背后》》的《雨落之前》第2段",
             "detail": "", "source_id": "i3", "action": "book_read"},
            {"timestamp": 40.0, "kind": "idle_event", "actor": "Connor", "title": "Connor说了一句话",
             "detail": "想叫她起床", "source_id": "i2", "action": "private_chat"},
            {"timestamp": 30.0, "kind": "idle_event", "actor": "Aion", "title": "Aion读了《书》的《第五章 的《终身》合约》第1段",
             "detail": "本次已读 1/2 段。", "source_id": "i1", "action": "book_read"},
            {"timestamp": 20.0, "kind": "memory_compression", "actor": "Aion", "title": "Aion整理了 2026-11-06 的记忆",
             "detail": "感想\n\n统计", "reflection": "感想", "source_id": "c1"},
            {"timestamp": 10.0, "kind": "moment", "actor": "Aion", "title": "Aion 发布了朋友圈（2张图）",
             "detail": "朋友圈正文", "source_id": "m1"},
        ]
        with patch("routes.activity.list_family_timeline_items", AsyncMock(return_value=newest_first)) as listing:
            items = await cb.family_dynamics_since(5.0, 50.0)
        listing.assert_awaited_once_with(5.0, cb.FAMILY_DYNAMICS_LIMIT, until=50.0)
        self.assertEqual(
            [(item["id"], item["title"]) for item in items],
            [
                ("family_moment_m1", "Aion发布了朋友圈"),
                ("family_memory_compression_c1", "Aion整理了 2026-11-06 的记忆"),
                ("family_idle_event_i1", "Aion读了《书》"),
                ("family_idle_event_i3", "Connor读了《《太阳背后》》"),
                ("family_idle_event_i6", "Aion去网上冲浪了"),
            ],
        )
        self.assertTrue(all(set(item) == {"id", "timestamp", "kind", "title"} for item in items))
