import unittest
import asyncio
import copy
import sqlite3
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite
import httpx
from fastapi import FastAPI

import context_builder
import database
import english_corner
from english_corner import context_limit_options, learning_day_start, normalize_actor


class EnglishCornerRuleTests(unittest.TestCase):
    def test_learning_day_starts_previous_day_before_five(self):
        now = datetime(2026, 7, 25, 4, 59, 0)

        start = datetime.fromtimestamp(learning_day_start(now))

        self.assertEqual(start, datetime(2026, 7, 24, 5, 0, 0))

    def test_learning_day_starts_today_at_five(self):
        now = datetime(2026, 7, 25, 5, 0, 0)

        start = datetime.fromtimestamp(learning_day_start(now))

        self.assertEqual(start, datetime(2026, 7, 25, 5, 0, 0))

    def test_context_options_use_tens_and_actual_total(self):
        self.assertEqual(
            context_limit_options(86),
            {"options": [10, 20, 30, 40, 50, 60, 70, 80, 86], "default": 50},
        )
        self.assertEqual(
            context_limit_options(34),
            {"options": [10, 20, 30, 34], "default": 34},
        )
        self.assertEqual(context_limit_options(0), {"options": [0], "default": 0})

    def test_context_options_default_to_all_below_fifty(self):
        self.assertEqual(
            context_limit_options(49),
            {"options": [10, 20, 30, 40, 49], "default": 49},
        )

    def test_normalize_actor_accepts_only_supported_actor_ids(self):
        self.assertEqual(normalize_actor("aion"), "aion")
        self.assertEqual(normalize_actor("connor"), "connor")
        with self.assertRaisesRegex(ValueError, "actor"):
            normalize_actor("user")


class EnglishCornerGenerationParserTests(unittest.TestCase):
    VALID_PAYLOAD = r"""
    {
      "cards": [
        {
          "title": "  Breakfast plans  ",
          "utterances": [
            {
              "speaker": "user",
              "english": "  Should we make pancakes?  ",
              "translation": "  我们要做煎饼吗？  "
            },
            {
              "speaker": "aion",
              "english": "Only if we add blueberries.",
              "translation": "除非我们加蓝莓。"
            }
          ],
          "vocabulary": [
            {
              "term": "  only if  ",
              "ipa": "  /ˈəʊnli ɪf/  ",
              "part_of_speech": "  phrase  ",
              "meaning": "  只有在……的条件下  "
            }
          ]
        },
        {
          "title": "Finding the keys",
          "utterances": [
            {
              "speaker": "connor",
              "english": "Have you checked the coat pocket?",
              "translation": "你检查过外套口袋了吗？"
            },
            {
              "speaker": "user",
              "english": "That was the first place I looked.",
              "translation": "我第一个找的就是那里。"
            },
            {
              "speaker": "aion",
              "english": "Let's check the kitchen counter once more.",
              "translation": "我们再检查一次厨房台面吧。"
            }
          ],
          "vocabulary": [
            {
              "term": "coat pocket",
              "ipa": "/kəʊt ˈpɒkɪt/",
              "part_of_speech": "noun",
              "meaning": "外套口袋"
            }
          ]
        },
        {
          "title": "A quiet evening",
          "utterances": [
            {
              "speaker": "aion",
              "english": "Let's keep the lights low tonight.",
              "translation": "今晚把灯光调暗一点吧。"
            },
            {
              "speaker": "connor",
              "english": "That sounds wonderfully peaceful.",
              "translation": "听起来会非常宁静。"
            }
          ],
          "vocabulary": [
            {
              "term": "keep the lights low",
              "ipa": "/kiːp ðə laɪts ləʊ/",
              "part_of_speech": "phrase",
              "meaning": "让灯光保持昏暗"
            }
          ]
        }
      ]
    }
    """

    def test_accepts_exactly_three_complete_cards_and_normalizes_edges(self):
        """Catches parsers that skip cardinality/field validation or retain padding."""
        parsed = english_corner.parse_generation_payload(self.VALID_PAYLOAD)

        self.assertEqual(len(parsed["cards"]), 3)
        self.assertEqual(parsed["cards"][0]["title"], "Breakfast plans")
        self.assertEqual(
            parsed["cards"][0]["utterances"][0],
            {
                "speaker": "user",
                "english": "Should we make pancakes?",
                "translation": "我们要做煎饼吗？",
            },
        )
        self.assertEqual(
            parsed["cards"][0]["vocabulary"][0],
            {
                "term": "only if",
                "ipa": "/ˈəʊnli ɪf/",
                "part_of_speech": "phrase",
                "meaning": "只有在……的条件下",
            },
        )

    def test_removes_one_optional_outer_json_fence(self):
        """Catches valid model JSON being rejected solely because of one outer fence."""
        parsed = english_corner.parse_generation_payload(
            f"\n```json\n{self.VALID_PAYLOAD}\n```\n"
        )

        self.assertEqual([card["title"] for card in parsed["cards"]], [
            "Breakfast plans",
            "Finding the keys",
            "A quiet evening",
        ])

    def test_rejects_wrong_card_or_utterance_count(self):
        """Catches partial packs and cards outside the required two-to-three turns."""
        two_cards = r"""
        {
          "cards": [
            {
              "title": "One",
              "utterances": [
                {"speaker": "user", "english": "One.", "translation": "一。"},
                {"speaker": "aion", "english": "Two.", "translation": "二。"}
              ],
              "vocabulary": [
                {"term": "one", "ipa": "/wʌn/", "part_of_speech": "number", "meaning": "一"}
              ]
            },
            {
              "title": "Two",
              "utterances": [
                {"speaker": "connor", "english": "Three.", "translation": "三。"},
                {"speaker": "user", "english": "Four.", "translation": "四。"}
              ],
              "vocabulary": [
                {"term": "four", "ipa": "/fɔːr/", "part_of_speech": "number", "meaning": "四"}
              ]
            }
          ]
        }
        """
        one_utterance = self.VALID_PAYLOAD.replace(
            """,
            {
              "speaker": "aion",
              "english": "Only if we add blueberries.",
              "translation": "除非我们加蓝莓。"
            }""",
            "",
            1,
        )

        with self.assertRaises(english_corner.EnglishCornerValidationError):
            english_corner.parse_generation_payload(two_cards)
        with self.assertRaises(english_corner.EnglishCornerValidationError):
            english_corner.parse_generation_payload(one_utterance)

    def test_rejects_unknown_or_disallowed_speakers(self):
        """Catches free-text identities escaping the stable role registry."""
        unknown = self.VALID_PAYLOAD.replace('"speaker": "connor"', '"speaker": "guest"', 1)

        with self.assertRaises(english_corner.EnglishCornerValidationError):
            english_corner.parse_generation_payload(unknown)
        with self.assertRaises(english_corner.EnglishCornerValidationError):
            english_corner.parse_generation_payload(
                self.VALID_PAYLOAD,
                allowed_speakers={"user", "aion"},
            )

    def test_rejects_blank_bilingual_text_and_incomplete_vocabulary(self):
        """Catches semantically empty lines or vocabulary fields invented by repair."""
        blank_translation = self.VALID_PAYLOAD.replace(
            '"translation": "除非我们加蓝莓。"',
            '"translation": "   "',
            1,
        )
        missing_ipa = self.VALID_PAYLOAD.replace(
            '"ipa": "/kəʊt ˈpɒkɪt/",',
            "",
            1,
        )

        with self.assertRaises(english_corner.EnglishCornerValidationError):
            english_corner.parse_generation_payload(blank_translation)
        with self.assertRaises(english_corner.EnglishCornerValidationError):
            english_corner.parse_generation_payload(missing_ipa)

    def test_rejects_malformed_partial_or_explained_output(self):
        """Catches non-JSON, missing semantic fields, and prose repair attempts."""
        invalid_values = (
            '{"cards": [}',
            '{"cards": [{}, {}, {}]}',
            f"Here is the result:\n{self.VALID_PAYLOAD}",
            f"```json\n{self.VALID_PAYLOAD}\n```\nextra",
        )

        for raw in invalid_values:
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(english_corner.EnglishCornerValidationError):
                    english_corner.parse_generation_payload(raw)


class EnglishCornerGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "generation.sqlite3"
        async with aiosqlite.connect(self.db_path) as db:
            await english_corner.ensure_english_corner_tables(db)
            await db.execute(
                """
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            await db.executemany(
                "INSERT INTO conversations VALUES (?, ?, ?)",
                [
                    ("older", "Older-Main-Model", 10.0),
                    ("latest", "Latest-Main-Model", 20.0),
                ],
            )
            await db.commit()
        self.open_db_patch = patch.object(
            english_corner,
            "_open_english_corner_db",
            new=lambda db_path=None: aiosqlite.connect(self.db_path),
        )
        self.application_db_patch = patch(
            "database.get_db",
            new=lambda: aiosqlite.connect(self.db_path),
        )
        self.tts_config_patch = patch.object(
            english_corner,
            "_load_tts_voice_config",
            return_value={"tts_aion_voice": "", "tts_connor_voice": ""},
            create=True,
        )
        self.open_db_patch.start()
        self.application_db_patch.start()
        self.tts_config_patch.start()

    async def asyncTearDown(self):
        self.tts_config_patch.stop()
        self.application_db_patch.stop()
        self.open_db_patch.stop()
        self.temp_dir.cleanup()

    def test_prompt_requires_structured_independent_daily_cards_and_dynamic_roles(self):
        """Catches hardcoded identities or prompts that permit ambiguous free-form output."""
        messages = english_corner.build_generation_messages(
            "connor",
            [
                {"role": "user", "content": "Older rendered context"},
                {"role": "user", "content": "Newer rendered context"},
            ],
            {
                "user": {"name": "Configured User", "persona": "likes quiet mornings"},
                "aion": {"name": "Configured Main", "persona": "warm and playful"},
                "connor": {
                    "name": "Configured Second",
                    "persona": "dry humor and practical care",
                },
            },
        )
        prompt = "\n".join(message["content"] for message in messages)

        for fragment in (
            '"user": {"name": "Configured User", "persona": "likes quiet mornings"}',
            '"aion": {"name": "Configured Main", "persona": "warm and playful"}',
            '"connor": {"name": "Configured Second", "persona": "dry humor and practical care"}',
            '"cards"',
            "恰好 3 张卡片",
            "2 或 3 句对话",
            '"speaker"',
            '"english"',
            '"translation"',
            '"term"',
            '"ipa"',
            '"part_of_speech"',
            '"meaning"',
            "CET-4",
            "自然、可复用的日常英语",
            "生活气和趣味性",
            "调侃",
            "小意外",
            "亲密互动",
            "三张卡片使用不同的趣味机制",
            "禁止写成英语教材",
            "禁止把三个人写成互相客套的陌生人",
            "各自独立",
            "灵感",
            "自由改编",
            "零上下文",
            "不是真实记忆",
            "不得写回",
            "稳定 speaker ID",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, prompt)
        self.assertLess(
            prompt.index("Older rendered context"),
            prompt.index("Newer rendered context"),
        )

    async def test_context_options_count_the_selected_actor_learning_day(self):
        """Catches context choices that use the wrong actor, cutoff, or default."""
        count = AsyncMock(return_value=86)
        now = datetime(2026, 7, 25, 10, 30, 0)

        with patch("context_builder.count_merged_timeline", new=count):
            result = await english_corner.get_context_options("connor", now=now)

        self.assertEqual(result["actor"], "connor")
        self.assertEqual(
            datetime.fromtimestamp(result["learning_day_start"]),
            datetime(2026, 7, 25, 5, 0, 0),
        )
        self.assertEqual(
            datetime.fromtimestamp(result["learning_day_end"]),
            now,
        )
        self.assertEqual(result["context_total"], 86)
        self.assertEqual(
            result["options"],
            [10, 20, 30, 40, 50, 60, 70, 80, 86],
        )
        self.assertEqual(result["default"], 50)
        count.assert_awaited_once_with(
            "connor",
            since_ts=datetime(2026, 7, 25, 5, 0, 0).timestamp(),
            until_ts=now.timestamp(),
        )

    async def test_generation_uses_the_snapshot_that_offered_all_messages(self):
        """Catches a new row invalidating an offered all-N option before POST."""
        snapshot_time = datetime(2026, 7, 25, 11, 0, 0)
        later_time = datetime(2026, 7, 25, 11, 1, 0)
        count_bounds = []

        async def count_at_bound(actor, *, since_ts=None, until_ts=None):
            count_bounds.append((actor, since_ts, until_ts))
            return 34 if until_ts <= snapshot_time.timestamp() else 35

        timeline = [
            {
                "source": "private",
                "sender": "user",
                "content": f"snapshot message {index}",
                "created_at": snapshot_time.timestamp() - 34 + index,
                "attachments": "[]",
            }
            for index in range(34)
        ]
        fetch = AsyncMock(return_value=timeline)
        main_call = AsyncMock(
            return_value=EnglishCornerGenerationParserTests.VALID_PAYLOAD
        )

        with (
            patch(
                "context_builder.count_merged_timeline",
                new=count_at_bound,
            ),
            patch("context_builder.fetch_merged_timeline", new=fetch),
            patch(
                "context_builder.render_merged_timeline",
                return_value=[
                    {"role": "user", "content": "Frozen snapshot context"}
                ],
            ),
            patch(
                "config.load_worldbook",
                return_value={"user_persona": "", "ai_persona": ""},
            ),
            patch(
                "chatroom.get_chatroom_names",
                return_value=(
                    "Configured User",
                    "Configured Main",
                    "Configured Second",
                ),
            ),
            patch(
                "chatroom.load_chatroom_config",
                return_value={"connor_model": "Configured-Second-Model"},
            ),
            patch("chatroom._read_connor_persona", return_value=""),
            patch("ai_providers.simple_ai_call", new=main_call),
        ):
            options = await english_corner.get_context_options(
                "aion",
                now=snapshot_time,
            )
            saved = await english_corner.generate_learning_pack(
                "aion",
                options["context_total"],
                "frozen-options-snapshot",
                now=later_time,
                snapshot_end=options["learning_day_end"],
            )

        self.assertEqual(options["context_total"], 34)
        self.assertEqual(saved["context_total"], 34)
        self.assertEqual(saved["context_limit"], 34)
        self.assertEqual(
            saved["learning_day_end"],
            options["learning_day_end"],
        )
        self.assertEqual(
            [bound[2] for bound in count_bounds],
            [snapshot_time.timestamp(), snapshot_time.timestamp()],
        )
        fetch.assert_awaited_once_with(
            "aion",
            34,
            since_ts=datetime(2026, 7, 25, 5, 0, 0).timestamp(),
            until_ts=snapshot_time.timestamp(),
        )
        main_call.assert_awaited_once()

    async def test_main_actor_calls_latest_main_route_once_and_persists_metadata(self):
        """Catches wrong routing, multiple model calls, unrendered context, or lost metadata."""
        count = AsyncMock(return_value=2)
        timeline = [
            {
                "source": "private",
                "sender": "user",
                "content": "raw older",
                "created_at": 100.0,
                "attachments": "[]",
            },
            {
                "source": "group",
                "sender": "connor",
                "content": "raw newer",
                "created_at": 200.0,
                "attachments": "[]",
            },
        ]
        fetch = AsyncMock(return_value=timeline)
        rendered = [
            {"role": "user", "content": "Rendered older message"},
            {"role": "user", "content": "Rendered newer message"},
        ]
        main_call = AsyncMock(
            return_value=EnglishCornerGenerationParserTests.VALID_PAYLOAD
        )
        second_call = AsyncMock(
            side_effect=AssertionError("second-AI route must not be called")
        )
        now = datetime(2026, 7, 25, 11, 0, 0)

        with (
            patch("context_builder.count_merged_timeline", new=count),
            patch("context_builder.fetch_merged_timeline", new=fetch),
            patch("context_builder.render_merged_timeline", return_value=rendered) as render,
            patch(
                "config.load_worldbook",
                return_value={
                    "user_name": "Configured User",
                    "user_persona": "user profile",
                    "ai_name": "Configured Main",
                    "ai_persona": "main profile",
                },
            ),
            patch(
                "chatroom.get_chatroom_names",
                return_value=("Configured User", "Configured Main", "Configured Second"),
            ),
            patch(
                "chatroom.load_chatroom_config",
                return_value={
                    "connor_name": "Configured Second",
                    "connor_model": "Configured-Second-Model",
                },
            ),
            patch("chatroom._read_connor_persona", return_value="second profile"),
            patch("ai_providers.simple_ai_call", new=main_call),
            patch("chatroom.simple_connor_cli_call", new=second_call),
        ):
            first = await english_corner.generate_learning_pack(
                "aion",
                2,
                "main-generation-request",
                now=now,
            )
            retried = await english_corner.generate_learning_pack(
                "connor",
                0,
                "main-generation-request",
                now=datetime(2026, 7, 25, 12, 0, 0),
            )

        self.assertEqual(retried, first)
        self.assertEqual(first["generator"], "aion")
        self.assertEqual(first["model_key"], "Latest-Main-Model")
        self.assertEqual(first["context_total"], 2)
        self.assertEqual(first["context_limit"], 2)
        self.assertEqual(first["context_start"], 100.0)
        self.assertEqual(first["context_end"], 200.0)
        self.assertEqual(
            datetime.fromtimestamp(first["learning_day_start"]),
            datetime(2026, 7, 25, 5, 0, 0),
        )
        self.assertEqual(
            datetime.fromtimestamp(first["learning_day_end"]),
            now,
        )
        self.assertEqual(len(first["cards"]), 3)
        count.assert_awaited_once()
        fetch.assert_awaited_once_with(
            "aion",
            2,
            since_ts=datetime(2026, 7, 25, 5, 0, 0).timestamp(),
            until_ts=now.timestamp(),
        )
        render.assert_called_once_with(timeline, "aion")
        main_call.assert_awaited_once()
        second_call.assert_not_awaited()
        called_messages, called_model = main_call.await_args.args[:2]
        called_prompt = "\n".join(message["content"] for message in called_messages)
        self.assertEqual(called_model, "Latest-Main-Model")
        self.assertLess(
            called_prompt.index("Rendered older message"),
            called_prompt.index("Rendered newer message"),
        )
        self.assertIn("Configured Main", called_prompt)
        self.assertIn("main profile", called_prompt)

    async def test_concurrent_duplicate_generation_coalesces_context_and_ai_work(self):
        """Catches simultaneous retries calling context and AI more than once."""
        count = AsyncMock(return_value=2)
        timeline = [
            {
                "source": "private",
                "sender": "user",
                "content": "raw older",
                "created_at": 100.0,
                "attachments": "[]",
            },
            {
                "source": "group",
                "sender": "aion",
                "content": "raw newer",
                "created_at": 200.0,
                "attachments": "[]",
            },
        ]
        fetch = AsyncMock(return_value=timeline)
        ai_calls = 0

        async def delayed_main_call(
            messages,
            model_key,
            temperature=None,
            *,
            trace_label="",
        ):
            nonlocal ai_calls
            ai_calls += 1
            await asyncio.sleep(0.05)
            return EnglishCornerGenerationParserTests.VALID_PAYLOAD

        with (
            patch("context_builder.count_merged_timeline", new=count),
            patch("context_builder.fetch_merged_timeline", new=fetch),
            patch(
                "context_builder.render_merged_timeline",
                return_value=[
                    {"role": "user", "content": "Rendered older message"},
                    {"role": "user", "content": "Rendered newer message"},
                ],
            ),
            patch(
                "config.load_worldbook",
                return_value={
                    "user_name": "Configured User",
                    "user_persona": "user profile",
                    "ai_name": "Configured Main",
                    "ai_persona": "main profile",
                },
            ),
            patch(
                "chatroom.get_chatroom_names",
                return_value=("Configured User", "Configured Main", "Configured Second"),
            ),
            patch(
                "chatroom.load_chatroom_config",
                return_value={
                    "connor_name": "Configured Second",
                    "connor_model": "Configured-Second-Model",
                },
            ),
            patch("chatroom._read_connor_persona", return_value="second profile"),
            patch("ai_providers.simple_ai_call", new=delayed_main_call),
        ):
            first, duplicate = await asyncio.gather(
                english_corner.generate_learning_pack(
                    "aion",
                    2,
                    "concurrent-generation-request",
                    now=datetime(2026, 7, 25, 11, 0, 0),
                ),
                english_corner.generate_learning_pack(
                    "connor",
                    0,
                    "concurrent-generation-request",
                    now=datetime(2026, 7, 25, 12, 0, 0),
                ),
            )

        self.assertEqual(duplicate, first)
        self.assertEqual(ai_calls, 1)
        count.assert_awaited_once()
        fetch.assert_awaited_once()
        self.assertEqual(first["generator"], "aion")

    async def test_second_actor_uses_configured_route_once_with_zero_context(self):
        """Catches zero-context fetches or accidental use of the main model route."""
        count = AsyncMock(return_value=34)
        fetch = AsyncMock(side_effect=AssertionError("zero context must not be fetched"))
        main_call = AsyncMock(side_effect=AssertionError("main route must not be called"))
        second_call = AsyncMock(
            return_value=EnglishCornerGenerationParserTests.VALID_PAYLOAD
        )

        with (
            patch("context_builder.count_merged_timeline", new=count),
            patch("context_builder.fetch_merged_timeline", new=fetch),
            patch("context_builder.render_merged_timeline") as render,
            patch(
                "config.load_worldbook",
                return_value={
                    "user_name": "Configured User",
                    "user_persona": "user profile",
                    "ai_name": "Configured Main",
                    "ai_persona": "main profile",
                },
            ),
            patch(
                "chatroom.get_chatroom_names",
                return_value=("Configured User", "Configured Main", "Configured Second"),
            ),
            patch(
                "chatroom.load_chatroom_config",
                return_value={
                    "connor_name": "Configured Second",
                    "connor_model": "Configured-Second-Model",
                },
            ),
            patch("chatroom._read_connor_persona", return_value="second profile"),
            patch("ai_providers.simple_ai_call", new=main_call),
            patch("chatroom.simple_connor_cli_call", new=second_call),
        ):
            saved = await english_corner.generate_learning_pack(
                "connor",
                0,
                "second-generation-request",
                now=datetime(2026, 7, 25, 9, 0, 0),
            )

        self.assertEqual(saved["generator"], "connor")
        self.assertEqual(saved["model_key"], "Configured-Second-Model")
        self.assertEqual(saved["context_total"], 34)
        self.assertEqual(saved["context_limit"], 0)
        self.assertIsNone(saved["context_start"])
        self.assertIsNone(saved["context_end"])
        count.assert_awaited_once()
        fetch.assert_not_awaited()
        render.assert_not_called()
        main_call.assert_not_awaited()
        second_call.assert_awaited_once()
        called_prompt = second_call.await_args.args[0]
        self.assertIn("零上下文", called_prompt)
        self.assertIn("自由创作", called_prompt)
        self.assertEqual(
            second_call.await_args.kwargs["model_key"],
            "Configured-Second-Model",
        )

    async def test_rejects_impossible_context_limit_before_fetch_or_ai(self):
        """Catches arbitrary context limits bypassing the offered ten-step choices."""
        count = AsyncMock(return_value=34)
        fetch = AsyncMock()
        main_call = AsyncMock()
        second_call = AsyncMock()

        with (
            patch("context_builder.count_merged_timeline", new=count),
            patch("context_builder.fetch_merged_timeline", new=fetch),
            patch("ai_providers.simple_ai_call", new=main_call),
            patch("chatroom.simple_connor_cli_call", new=second_call),
        ):
            with self.assertRaises(english_corner.EnglishCornerValidationError):
                await english_corner.generate_learning_pack(
                    "aion",
                    25,
                    "invalid-limit-request",
                    now=datetime(2026, 7, 25, 9, 0, 0),
                )

        count.assert_awaited_once()
        fetch.assert_not_awaited()
        main_call.assert_not_awaited()
        second_call.assert_not_awaited()
        self.assertIsNone(
            await english_corner.get_pack_by_request_id("invalid-limit-request")
        )


class EnglishCornerTimelineTests(unittest.IsolatedAsyncioTestCase):
    """Real-SQLite coverage for the time-bounded English-corner context."""

    cutoff = 100.0

    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "timeline.sqlite3"
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    conv_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    attachments TEXT DEFAULT '[]'
                );
                CREATE TABLE chatroom_rooms (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    type TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE chatroom_messages (
                    id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    attachments TEXT DEFAULT '[]'
                );
                """
            )
            await db.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, '[]')",
                [
                    ("main-old", "main-conv", "user", "old private", 99.0),
                    ("main-cutoff", "main-conv", "assistant", "cutoff private", 100.0),
                    ("main-new", "main-conv", "user", "new private", 105.0),
                ],
            )
            await db.executemany(
                "INSERT INTO chatroom_rooms VALUES (?, ?, ?, ?, ?)",
                [
                    ("connor-room", "Connor", "connor_1v1", 1.0, 1.0),
                    ("group-room", "Group", "group", 1.0, 1.0),
                ],
            )
            await db.executemany(
                "INSERT INTO chatroom_messages VALUES (?, ?, ?, ?, ?, '[]')",
                [
                    ("connor-old", "connor-room", "user", "old Connor private", 90.0),
                    ("connor-new", "connor-room", "connor", "new Connor private", 106.0),
                    ("group-old", "group-room", "user", "old group", 98.0),
                    ("group-new", "group-room", "aion", "new group", 110.0),
                ],
            )
            await db.commit()

        self.get_db_patch = patch(
            "context_builder.get_db", new=lambda: aiosqlite.connect(self.db_path)
        )
        self.get_db_patch.start()

    async def asyncTearDown(self):
        self.get_db_patch.stop()
        self.temp_dir.cleanup()

    async def test_count_uses_the_same_cutoff_visible_sources_for_each_actor(self):
        self.assertEqual(
            await context_builder.count_merged_timeline("aion", since_ts=self.cutoff),
            3,
        )
        self.assertEqual(
            await context_builder.count_merged_timeline("connor", since_ts=self.cutoff),
            2,
        )

    async def test_fetch_returns_latest_cutoff_rows_in_chronological_order(self):
        timeline = await context_builder.fetch_merged_timeline(
            "aion", 2, since_ts=self.cutoff
        )

        self.assertEqual(
            [message["content"] for message in timeline],
            ["new private", "new group"],
        )

    async def test_filters_apply_to_both_count_and_fetch(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, '[]')",
                ("other-conv", "other-conv", "user", "other private", 107.0),
            )
            await db.execute(
                "INSERT INTO chatroom_rooms VALUES (?, ?, ?, ?, ?)",
                ("other-group", "Other group", "group", 1.0, 1.0),
            )
            await db.execute(
                "INSERT INTO chatroom_messages VALUES (?, ?, ?, ?, ?, '[]')",
                ("other-group-new", "other-group", "user", "other group", 108.0),
            )
            await db.commit()

        timeline = await context_builder.fetch_merged_timeline(
            "aion",
            10,
            conv_id="main-conv",
            room_id="group-room",
            since_ts=self.cutoff,
        )

        self.assertEqual(
            [message["content"] for message in timeline],
            ["cutoff private", "new private", "new group"],
        )
        self.assertEqual(
            await context_builder.count_merged_timeline(
                "aion",
                conv_id="main-conv",
                room_id="group-room",
                since_ts=self.cutoff,
            ),
            3,
        )

    async def test_model_visible_system_filter_precedes_limit_and_until_bound(self):
        """Catches hidden/future system rows consuming the latest-N snapshot."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        "system-keyword",
                        "main-conv",
                        "system",
                        "搜索了今晚的菜单",
                        111.0,
                        "[]",
                    ),
                    (
                        "system-explicit",
                        "main-conv",
                        "system",
                        "explicit model context",
                        112.0,
                        '[{"type":"system_model_context"}]',
                    ),
                    (
                        "system-hidden",
                        "main-conv",
                        "system",
                        "internal maintenance event",
                        113.0,
                        "[]",
                    ),
                    (
                        "latest-visible",
                        "main-conv",
                        "user",
                        "latest visible",
                        114.0,
                        "[]",
                    ),
                    (
                        "future-visible",
                        "main-conv",
                        "user",
                        "future visible",
                        999.0,
                        "[]",
                    ),
                ],
            )
            await db.commit()

        snapshot = await context_builder.fetch_merged_timeline(
            "aion",
            3,
            conv_id="main-conv",
            room_id="group-room",
            since_ts=self.cutoff,
            until_ts=114.0,
        )

        self.assertEqual(
            [message["content"] for message in snapshot],
            [
                "搜索了今晚的菜单",
                "explicit model context",
                "latest visible",
            ],
        )
        self.assertEqual(
            await context_builder.count_merged_timeline(
                "aion",
                conv_id="main-conv",
                room_id="group-room",
                since_ts=self.cutoff,
                until_ts=114.0,
            ),
            6,
        )
        rendered = context_builder.render_merged_timeline(snapshot, "aion")
        self.assertEqual(len(rendered), 4)
        rendered_text = "\n".join(item["content"] for item in rendered)
        self.assertNotIn("internal maintenance event", rendered_text)
        self.assertNotIn("future visible", rendered_text)

    async def test_rows_inserted_after_options_do_not_change_snapshot(self):
        """Catches count/fetch races that expand a generation after options load."""
        now = datetime(2026, 7, 25, 11, 0, 0)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, '[]')",
                (
                    "present-before-options",
                    "main-conv",
                    "user",
                    "present before options",
                    now.timestamp() - 1,
                ),
            )
            await db.commit()
        options = await english_corner.get_context_options("aion", now=now)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, '[]')",
                (
                    "inserted-after-options",
                    "main-conv",
                    "user",
                    "inserted after options",
                    now.timestamp() + 1,
                ),
            )
            await db.commit()

        snapshot = await context_builder.fetch_merged_timeline(
            "aion",
            options["context_total"],
            since_ts=options["learning_day_start"],
            until_ts=options["learning_day_end"],
        )

        self.assertEqual(len(snapshot), options["context_total"])
        self.assertNotIn(
            "inserted after options",
            [message["content"] for message in snapshot],
        )


class EnglishCornerPersistenceSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_pack_schema_migrates_and_keeps_a_private_voice_snapshot(self):
        """Catches new packs losing the selected voice across later retries."""
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "voice-snapshot.sqlite3"
            async with aiosqlite.connect(db_path) as db:
                await english_corner.ensure_english_corner_tables(db)
                cursor = await db.execute(
                    "PRAGMA table_info(english_learning_packs)"
                )
                columns = {
                    row[1]: {
                        "not_null": row[3],
                        "default": row[4],
                    }
                    for row in await cursor.fetchall()
                }

        self.assertIn("tts_voice", columns)
        self.assertEqual(columns["tts_voice"]["not_null"], 1)
        self.assertEqual(columns["tts_voice"]["default"], "''")

    async def test_bootstrap_migrates_learning_day_end_onto_existing_pack_table(self):
        """Catches Task 4 metadata reads breaking databases created by Task 3."""
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "task-three-schema.sqlite3"
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    """
                    CREATE TABLE english_learning_packs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_id TEXT NOT NULL UNIQUE,
                        generator TEXT NOT NULL,
                        model_key TEXT NOT NULL DEFAULT '',
                        learning_day_start REAL,
                        context_total INTEGER NOT NULL DEFAULT 0,
                        context_limit INTEGER NOT NULL DEFAULT 0,
                        context_start REAL,
                        context_end REAL,
                        created_at REAL NOT NULL
                    )
                    """
                )

                await english_corner.ensure_english_corner_tables(db)

                cursor = await db.execute(
                    "PRAGMA table_info(english_learning_packs)"
                )
                columns = {row[1] for row in await cursor.fetchall()}
                self.assertIn("learning_day_end", columns)

    async def test_bootstrap_creates_only_normalized_learning_tables_with_indexes(self):
        """Catches missing tables, child foreign keys, or query indexes."""
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "english-corner.sqlite3"
            async with aiosqlite.connect(db_path) as db:
                bootstrap = getattr(english_corner, "ensure_english_corner_tables", None)
                self.assertTrue(callable(bootstrap), "table bootstrap must exist")

                await bootstrap(db)

                cursor = await db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name LIKE 'english_learning_%'"
                )
                table_names = {row[0] for row in await cursor.fetchall()}
                self.assertEqual(
                    table_names,
                    {
                        "english_learning_packs",
                        "english_learning_cards",
                        "english_learning_utterances",
                        "english_learning_vocabulary",
                        "english_learning_audio",
                    },
                )

                for child_table in (
                    "english_learning_cards",
                    "english_learning_utterances",
                    "english_learning_vocabulary",
                    "english_learning_audio",
                ):
                    cursor = await db.execute(
                        f"PRAGMA foreign_key_list({child_table})"
                    )
                    self.assertGreater(
                        len(await cursor.fetchall()),
                        0,
                        f"{child_table} must have a foreign key",
                    )

                cursor = await db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND name LIKE 'idx_english_learning_%'"
                )
                index_names = {row[0] for row in await cursor.fetchall()}
                self.assertTrue(
                    {
                        "idx_english_learning_cards_status_order",
                        "idx_english_learning_packs_created",
                        "idx_english_learning_audio_utterance",
                    }.issubset(index_names)
                )

    async def test_runtime_foreign_keys_reject_orphaned_children(self):
        """Catches bootstraps that declare but do not enable foreign keys."""
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "foreign-keys.sqlite3"
            async with aiosqlite.connect(db_path) as db:
                await english_corner.ensure_english_corner_tables(db)

                with self.assertRaises(sqlite3.IntegrityError):
                    await db.execute(
                        """
                        INSERT INTO english_learning_cards (
                            pack_id, position, title, status, updated_at
                        ) VALUES (999, 0, 'Orphan', 'learning', 1.0)
                        """
                    )

    async def test_deleting_pack_cascades_through_all_child_tables(self):
        """Catches missing cascade behavior anywhere in the normalized tree."""
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "cascade.sqlite3"
            saved = await english_corner.save_learning_pack(
                copy.deepcopy(EnglishCornerPersistenceTests.PACK_PAYLOAD),
                request_id="cascade-pack",
                generator="aion",
                context_meta=dict(EnglishCornerPersistenceTests.CONTEXT_META),
                db_path=db_path,
            )
            first_utterance_id = saved["cards"][0]["utterances"][1]["id"]
            async with aiosqlite.connect(db_path) as db:
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute(
                    """
                    UPDATE english_learning_audio
                    SET voice = 'voice',
                        file_path = 'one.mp3',
                        status = 'ready',
                        error = NULL,
                        updated_at = 1.0
                    WHERE utterance_id = ?
                    """,
                    (first_utterance_id,),
                )
                try:
                    await db.execute(
                        "DELETE FROM english_learning_packs WHERE id = ?",
                        (saved["id"],),
                    )
                    await db.commit()
                except sqlite3.IntegrityError as exc:
                    await db.rollback()
                    self.fail(f"pack deletion did not cascade: {exc}")

                for table_name in (
                    "english_learning_packs",
                    "english_learning_cards",
                    "english_learning_utterances",
                    "english_learning_vocabulary",
                    "english_learning_audio",
                ):
                    cursor = await db.execute(f"SELECT COUNT(*) FROM {table_name}")
                    self.assertEqual(
                        (await cursor.fetchone())[0],
                        0,
                        table_name,
                    )

    async def test_database_init_bootstraps_english_corner_tables(self):
        """Catches startup paths that omit the focused English-corner schema."""
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "application.sqlite3"
            with patch.object(database, "DB_PATH", db_path):
                await database.init_db()

            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name LIKE 'english_learning_%'"
                )
                table_names = {row[0] for row in await cursor.fetchall()}
            self.assertEqual(
                table_names,
                {
                    "english_learning_packs",
                    "english_learning_cards",
                    "english_learning_utterances",
                    "english_learning_vocabulary",
                    "english_learning_audio",
                },
            )


class EnglishCornerPersistenceTests(unittest.IsolatedAsyncioTestCase):
    PACK_PAYLOAD = {
        "model_key": "configured-model",
        "cards": [
            {
                "title": "Breakfast plans",
                "utterances": [
                    {
                        "speaker": "user",
                        "english": "Should we make pancakes?",
                        "translation": "我们要做煎饼吗？",
                    },
                    {
                        "speaker": "aion",
                        "english": "Only if we add blueberries.",
                        "translation": "除非我们加蓝莓。",
                    },
                ],
                "vocabulary": [
                    {
                        "term": "only if",
                        "ipa": "/ˈəʊnli ɪf/",
                        "part_of_speech": "phrase",
                        "meaning": "只有在……的条件下",
                    }
                ],
            },
            {
                "title": "Finding the keys",
                "utterances": [
                    {
                        "speaker": "connor",
                        "english": "Have you checked the coat pocket?",
                        "translation": "你检查过外套口袋了吗？",
                    },
                    {
                        "speaker": "user",
                        "english": "That was the first place I looked.",
                        "translation": "我第一个找的就是那里。",
                    },
                ],
                "vocabulary": [
                    {
                        "term": "coat pocket",
                        "ipa": "/kəʊt ˈpɒkɪt/",
                        "part_of_speech": "noun",
                        "meaning": "外套口袋",
                    }
                ],
            },
            {
                "title": "A quiet evening",
                "utterances": [
                    {
                        "speaker": "aion",
                        "english": "Let's keep the lights low tonight.",
                        "translation": "今晚把灯光调暗一点吧。",
                    },
                    {
                        "speaker": "connor",
                        "english": "That sounds wonderfully peaceful.",
                        "translation": "听起来会非常宁静。",
                    },
                ],
                "vocabulary": [
                    {
                        "term": "keep the lights low",
                        "ipa": "/kiːp ðə laɪts ləʊ/",
                        "part_of_speech": "phrase",
                        "meaning": "让灯光保持昏暗",
                    }
                ],
            },
        ],
    }
    CONTEXT_META = {
        "model_key": "configured-model",
        "learning_day_start": 1_721_877_600.0,
        "context_total": 12,
        "context_limit": 10,
        "context_start": 1_721_878_000.0,
        "context_end": 1_721_879_000.0,
    }

    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "english-corner.sqlite3"
        async with aiosqlite.connect(self.db_path) as db:
            await english_corner.ensure_english_corner_tables(db)
            await db.executescript(
                """
                CREATE TABLE messages (id TEXT PRIMARY KEY, content TEXT);
                CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT);
                CREATE TABLE chatroom_messages (id TEXT PRIMARY KEY, content TEXT);
                INSERT INTO messages VALUES ('message-sentinel', 'unchanged');
                INSERT INTO memories VALUES ('memory-sentinel', 'unchanged');
                INSERT INTO chatroom_messages VALUES ('chat-sentinel', 'unchanged');
                """
            )
            await db.commit()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_save_persists_one_complete_pack_without_chat_or_memory_writes(self):
        """Catches partial pack inserts and writes across the data boundary."""
        save = getattr(english_corner, "save_learning_pack", None)
        self.assertTrue(callable(save), "pack repository must exist")

        saved = await save(
            copy.deepcopy(self.PACK_PAYLOAD),
            request_id="request-one",
            generator="aion",
            context_meta=dict(self.CONTEXT_META),
            db_path=self.db_path,
        )

        self.assertEqual(saved["request_id"], "request-one")
        self.assertEqual(len(saved["cards"]), 3)
        self.assertEqual(
            [card["position"] for card in saved["cards"]],
            [0, 1, 2],
        )
        self.assertEqual(
            sum(len(card["utterances"]) for card in saved["cards"]),
            6,
        )
        async with aiosqlite.connect(self.db_path) as db:
            expected_counts = {
                "english_learning_packs": 1,
                "english_learning_cards": 3,
                "english_learning_utterances": 6,
                "english_learning_vocabulary": 3,
                "english_learning_audio": 4,
                "messages": 1,
                "memories": 1,
                "chatroom_messages": 1,
            }
            for table_name, expected in expected_counts.items():
                cursor = await db.execute(f"SELECT COUNT(*) FROM {table_name}")
                self.assertEqual(
                    (await cursor.fetchone())[0],
                    expected,
                    table_name,
                )
            expected_sentinel_rows = {
                "messages": [("message-sentinel", "unchanged")],
                "memories": [("memory-sentinel", "unchanged")],
                "chatroom_messages": [("chat-sentinel", "unchanged")],
            }
            for table_name, expected_rows in expected_sentinel_rows.items():
                cursor = await db.execute(
                    f"SELECT id, content FROM {table_name} ORDER BY id"
                )
                self.assertEqual(
                    await cursor.fetchall(),
                    expected_rows,
                    table_name,
                )

    async def test_save_persists_the_selected_pack_voice_snapshot(self):
        """Catches generation-time voice selection being replaced by role voices."""
        saved = await english_corner.save_learning_pack(
            copy.deepcopy(self.PACK_PAYLOAD),
            request_id="single-voice-pack",
            generator="aion",
            tts_voice="speech:custom:english-trained",
            context_meta=dict(self.CONTEXT_META),
            db_path=self.db_path,
        )

        self.assertEqual(
            saved["tts_voice"],
            "speech:custom:english-trained",
        )
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT tts_voice
                FROM english_learning_packs
                WHERE request_id = ?
                """,
                ("single-voice-pack",),
            )
            self.assertEqual(
                (await cursor.fetchone())[0],
                "speech:custom:english-trained",
            )

    async def test_duplicate_request_returns_original_nested_pack(self):
        """Catches retries that duplicate or overwrite a previously saved pack."""
        original = await english_corner.save_learning_pack(
            copy.deepcopy(self.PACK_PAYLOAD),
            request_id="idempotent-request",
            generator="aion",
            context_meta=dict(self.CONTEXT_META),
            db_path=self.db_path,
        )
        changed_payload = copy.deepcopy(self.PACK_PAYLOAD)
        changed_payload["cards"][0]["title"] = "This retry must not overwrite"

        retried = await english_corner.save_learning_pack(
            changed_payload,
            request_id="idempotent-request",
            generator="connor",
            context_meta={**self.CONTEXT_META, "context_limit": 0},
            db_path=self.db_path,
        )

        self.assertEqual(retried, original)
        self.assertEqual(retried["cards"][0]["title"], "Breakfast plans")
        async with aiosqlite.connect(self.db_path) as db:
            for table_name, expected in (
                ("english_learning_packs", 1),
                ("english_learning_cards", 3),
                ("english_learning_utterances", 6),
            ):
                cursor = await db.execute(f"SELECT COUNT(*) FROM {table_name}")
                self.assertEqual((await cursor.fetchone())[0], expected)

    async def test_concurrent_duplicate_requests_create_exactly_one_pack(self):
        """Catches idempotency races when writers are not serialized."""

        async def save_duplicate():
            return await english_corner.save_learning_pack(
                copy.deepcopy(self.PACK_PAYLOAD),
                request_id="concurrent-request",
                generator="aion",
                context_meta=dict(self.CONTEXT_META),
                db_path=self.db_path,
            )

        results = await asyncio.gather(
            *(save_duplicate() for _ in range(12)),
            return_exceptions=True,
        )

        errors = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(errors, [], f"concurrent saves raised: {errors!r}")
        self.assertTrue(all(result == results[0] for result in results[1:]))
        async with aiosqlite.connect(self.db_path) as db:
            for table_name, expected in (
                ("english_learning_packs", 1),
                ("english_learning_cards", 3),
                ("english_learning_utterances", 6),
            ):
                cursor = await db.execute(f"SELECT COUNT(*) FROM {table_name}")
                self.assertEqual((await cursor.fetchone())[0], expected)

    async def test_mid_insert_failure_rolls_back_the_entire_pack(self):
        """Catches transaction handlers that leave a partially inserted pack."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                CREATE TRIGGER fail_third_english_card
                BEFORE INSERT ON english_learning_cards
                WHEN NEW.position = 2
                BEGIN
                    SELECT RAISE(ABORT, 'forced mid-pack failure');
                END;
                """
            )
            await db.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "mid-pack failure"):
            await english_corner.save_learning_pack(
                copy.deepcopy(self.PACK_PAYLOAD),
                request_id="mid-insert-failure",
                generator="aion",
                context_meta=dict(self.CONTEXT_META),
                db_path=self.db_path,
            )

        async with aiosqlite.connect(self.db_path) as db:
            for table_name in (
                "english_learning_packs",
                "english_learning_cards",
                "english_learning_utterances",
                "english_learning_vocabulary",
                "english_learning_audio",
            ):
                cursor = await db.execute(f"SELECT COUNT(*) FROM {table_name}")
                self.assertEqual((await cursor.fetchone())[0], 0, table_name)

    async def test_get_pack_returns_ordered_nested_audio_metadata(self):
        """Catches flattened, unordered, or detached nested pack reads."""
        saved = await english_corner.save_learning_pack(
            copy.deepcopy(self.PACK_PAYLOAD),
            request_id="nested-read",
            generator="connor",
            context_meta=dict(self.CONTEXT_META),
            db_path=self.db_path,
        )
        first_utterance = saved["cards"][0]["utterances"][1]
        first_utterance_id = first_utterance["id"]
        first_audio_id = first_utterance["audio"]["id"]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE english_learning_audio
                SET speaker = ?,
                    voice = ?,
                    file_path = ?,
                    status = ?,
                    error = ?,
                    updated_at = ?
                WHERE utterance_id = ?
                """,
                (
                    "aion",
                    "voice-snapshot",
                    "english-corner/one.mp3",
                    "ready",
                    None,
                    1234.5,
                    first_utterance_id,
                ),
            )
            await db.commit()

        get_pack = getattr(english_corner, "get_pack_by_request_id", None)
        self.assertTrue(callable(get_pack), "pack read repository must exist")
        loaded = await get_pack("nested-read", db_path=self.db_path)

        self.assertEqual(
            [card["title"] for card in loaded["cards"]],
            ["Breakfast plans", "Finding the keys", "A quiet evening"],
        )
        self.assertEqual(
            [item["position"] for item in loaded["cards"][0]["utterances"]],
            [0, 1],
        )
        self.assertEqual(
            [item["position"] for item in loaded["cards"][0]["vocabulary"]],
            [0],
        )
        self.assertEqual(
            loaded["cards"][0]["utterances"][1]["audio"],
            {
                "id": first_audio_id,
                "utterance_id": first_utterance_id,
                "speaker": "aion",
                "voice": "voice-snapshot",
                "file_path": "english-corner/one.mp3",
                "status": "ready",
                "error": None,
                "updated_at": 1234.5,
            },
        )
        self.assertIsNone(loaded["cards"][0]["utterances"][0]["audio"])
        self.assertIsNone(
            await get_pack("missing-request", db_path=self.db_path)
        )

    async def test_list_cards_paginates_in_pack_and_card_order(self):
        """Catches unstable ordering, wrong totals, and invalid status leakage."""
        await english_corner.save_learning_pack(
            copy.deepcopy(self.PACK_PAYLOAD),
            request_id="older-pack",
            generator="aion",
            context_meta=dict(self.CONTEXT_META),
            db_path=self.db_path,
        )
        await english_corner.save_learning_pack(
            copy.deepcopy(self.PACK_PAYLOAD),
            request_id="newer-pack",
            generator="connor",
            context_meta=dict(self.CONTEXT_META),
            db_path=self.db_path,
        )

        list_cards = getattr(english_corner, "list_cards", None)
        self.assertTrue(callable(list_cards), "card list repository must exist")
        page = await list_cards(
            "learning",
            limit=4,
            offset=1,
            db_path=self.db_path,
        )

        self.assertEqual(
            {key: page[key] for key in ("total", "limit", "offset")},
            {"total": 6, "limit": 4, "offset": 1},
        )
        self.assertEqual(
            [
                (item["pack"]["request_id"], item["position"])
                for item in page["items"]
            ],
            [
                ("newer-pack", 1),
                ("newer-pack", 2),
                ("older-pack", 0),
                ("older-pack", 1),
            ],
        )
        self.assertEqual(len(page["items"][0]["utterances"]), 2)
        self.assertEqual(len(page["items"][0]["vocabulary"]), 1)
        with self.assertRaisesRegex(
            english_corner.EnglishCornerValidationError, "status"
        ):
            await list_cards("archived", db_path=self.db_path)

    async def test_learning_transitions_update_timestamps_and_unlearned_count(self):
        """Catches stale counts or timestamps during learn/relearn transitions."""
        saved = await english_corner.save_learning_pack(
            copy.deepcopy(self.PACK_PAYLOAD),
            request_id="transition-pack",
            generator="aion",
            context_meta=dict(self.CONTEXT_META),
            db_path=self.db_path,
        )
        card = saved["cards"][0]
        set_status = getattr(english_corner, "set_card_status", None)
        get_count = getattr(english_corner, "get_unlearned_count", None)
        self.assertTrue(callable(set_status), "card transition repository must exist")
        self.assertTrue(callable(get_count), "unlearned count repository must exist")

        self.assertEqual(await get_count(db_path=self.db_path), 3)
        learned = await set_status(
            card["id"],
            "learned",
            db_path=self.db_path,
        )
        self.assertEqual(learned["status"], "learned")
        self.assertIsNotNone(learned["learned_at"])
        self.assertGreater(learned["updated_at"], card["updated_at"])
        self.assertEqual(await get_count(db_path=self.db_path), 2)

        relearned = await set_status(
            card["id"],
            "learning",
            db_path=self.db_path,
        )
        self.assertEqual(relearned["status"], "learning")
        self.assertIsNone(relearned["learned_at"])
        self.assertGreater(relearned["updated_at"], learned["updated_at"])
        self.assertEqual(await get_count(db_path=self.db_path), 3)

    async def test_unknown_card_and_status_raise_domain_exceptions(self):
        """Catches ambiguous database errors for invalid transition requests."""
        not_found_error = getattr(
            english_corner, "EnglishCornerNotFoundError", None
        )
        self.assertIsNotNone(
            not_found_error,
            "unknown cards need a dedicated domain exception",
        )

        with self.assertRaisesRegex(
            english_corner.EnglishCornerValidationError, "status"
        ):
            await english_corner.set_card_status(
                999,
                "archived",
                db_path=self.db_path,
            )
        with self.assertRaisesRegex(not_found_error, "999"):
            await english_corner.set_card_status(
                999,
                "learned",
                db_path=self.db_path,
            )


class EnglishCornerTtsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tts.sqlite3"
        self.audio_dir = Path(self.temp_dir.name) / "english-corner-audio"
        async with aiosqlite.connect(self.db_path) as db:
            await english_corner.ensure_english_corner_tables(db)
            await db.execute(
                """
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            await db.execute(
                "INSERT INTO conversations VALUES ('latest', 'TTS-Test-Model', 1.0)"
            )
            await db.commit()

        self.saved = await english_corner.save_learning_pack(
            copy.deepcopy(EnglishCornerPersistenceTests.PACK_PAYLOAD),
            request_id="tts-pack",
            generator="aion",
            context_meta=dict(EnglishCornerPersistenceTests.CONTEXT_META),
            db_path=self.db_path,
        )
        self.utterances = [
            utterance
            for card in self.saved["cards"]
            for utterance in card["utterances"]
        ]
        self.fail_texts = set()
        self.synthesized = []

        async def fake_synthesize(text, voice, output_path, **kwargs):
            output_path = Path(output_path)
            self.synthesized.append((text, voice, output_path, dict(kwargs)))
            if text in self.fail_texts:
                raise RuntimeError(f"provider rejected {text}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"ID3-test-audio")
            return {"segments": 1, "chars": len(text)}

        self.open_db_patch = patch.object(
            english_corner,
            "_open_english_corner_db",
            new=lambda db_path=None: aiosqlite.connect(
                str(db_path or self.db_path)
            ),
        )
        self.application_db_patch = patch(
            "database.get_db",
            new=lambda: aiosqlite.connect(self.db_path),
        )
        self.voice_config_patch = patch.object(
            english_corner,
            "_load_tts_voice_config",
            return_value={
                "tts_aion_voice": "voice-aion-v1",
                "tts_connor_voice": "voice-connor-v1",
            },
            create=True,
        )
        self.synthesize_patch = patch(
            "tts.synthesize_text_to_mp3",
            new=fake_synthesize,
        )
        self.open_db_patch.start()
        self.application_db_patch.start()
        self.voice_config = self.voice_config_patch.start()
        self.synthesize_patch.start()

    async def asyncTearDown(self):
        self.synthesize_patch.stop()
        self.voice_config_patch.stop()
        self.application_db_patch.stop()
        self.open_db_patch.stop()
        self.temp_dir.cleanup()

    async def test_prepare_uses_role_voices_stable_paths_and_cached_snapshots(self):
        """Catches wrong role voices, user audio, unstable paths, and cache churn."""
        prepare = getattr(english_corner, "prepare_pack_audio", None)
        self.assertTrue(callable(prepare), "pack TTS preparation must exist")

        prepared = await prepare(
            self.saved["id"],
            db_path=self.db_path,
            audio_dir=self.audio_dir,
        )

        ai_utterances = [
            utterance
            for utterance in self.utterances
            if utterance["speaker"] in {"aion", "connor"}
        ]
        user_ids = {
            utterance["id"]
            for utterance in self.utterances
            if utterance["speaker"] == "user"
        }
        self.assertEqual(prepared["ready"], len(ai_utterances))
        self.assertEqual(prepared["failed"], 0)
        self.assertEqual(len(self.synthesized), len(ai_utterances))
        by_id = {item["utterance_id"]: item for item in prepared["items"]}
        for utterance in ai_utterances:
            item = by_id[utterance["id"]]
            expected_voice = (
                "voice-aion-v1"
                if utterance["speaker"] == "aion"
                else "voice-connor-v1"
            )
            self.assertEqual(item["speaker"], utterance["speaker"])
            self.assertEqual(item["voice"], expected_voice)
            self.assertEqual(item["status"], "ready")
            self.assertEqual(item["error"], "")
            self.assertEqual(
                Path(item["file_path"]).resolve().parent,
                self.audio_dir.resolve(),
            )
            self.assertEqual(
                Path(item["file_path"]).name,
                f"utterance-{utterance['id']}.mp3",
            )
            self.assertTrue(Path(item["file_path"]).is_file())

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT utterance_id FROM english_learning_audio ORDER BY utterance_id"
            )
            audio_ids = {row[0] for row in await cursor.fetchall()}
        self.assertTrue(audio_ids.isdisjoint(user_ids))
        self.assertEqual(audio_ids, set(by_id))

        self.voice_config.return_value = {
            "tts_aion_voice": "voice-aion-v2",
            "tts_connor_voice": "voice-connor-v2",
        }
        cached = await prepare(
            self.saved["id"],
            db_path=self.db_path,
            audio_dir=Path(self.temp_dir.name) / "different-directory",
        )

        self.assertEqual(len(self.synthesized), len(ai_utterances))
        self.assertEqual(cached["cached"], len(ai_utterances))
        self.assertEqual(
            {item["voice"] for item in cached["items"]},
            {"voice-aion-v1", "voice-connor-v1"},
        )
        self.assertEqual(
            {Path(item["file_path"]).resolve().parent for item in cached["items"]},
            {self.audio_dir.resolve()},
        )

    async def test_new_pack_uses_one_selected_voice_for_every_speaker(self):
        """Catches user lines being skipped or AI lines reverting to role voices."""
        selected_voice = "speech:custom:english-trained"
        saved = await english_corner.save_learning_pack(
            copy.deepcopy(EnglishCornerPersistenceTests.PACK_PAYLOAD),
            request_id="unified-voice-pack",
            generator="aion",
            tts_voice=selected_voice,
            context_meta=dict(EnglishCornerPersistenceTests.CONTEXT_META),
            db_path=self.db_path,
        )
        all_utterances = [
            utterance
            for card in saved["cards"]
            for utterance in card["utterances"]
        ]

        prepared = await english_corner.prepare_pack_audio(
            saved["id"],
            db_path=self.db_path,
            audio_dir=self.audio_dir,
        )

        self.assertEqual(prepared["ready"], len(all_utterances))
        self.assertEqual(prepared["failed"], 0)
        self.assertEqual(len(self.synthesized), len(all_utterances))
        self.assertEqual(
            {voice for _, voice, _, _ in self.synthesized},
            {selected_voice},
        )
        self.assertEqual(
            {item["speaker"] for item in prepared["items"]},
            {"user", "aion", "connor"},
        )

    async def test_new_pack_user_line_can_fail_and_retry_in_isolation(self):
        """Catches the user speaker being rejected by targeted audio recovery."""
        selected_voice = "speech:custom:english-trained"
        saved = await english_corner.save_learning_pack(
            copy.deepcopy(EnglishCornerPersistenceTests.PACK_PAYLOAD),
            request_id="user-retry-pack",
            generator="connor",
            tts_voice=selected_voice,
            context_meta=dict(EnglishCornerPersistenceTests.CONTEXT_META),
            db_path=self.db_path,
        )
        user_utterance = next(
            utterance
            for card in saved["cards"]
            for utterance in card["utterances"]
            if utterance["speaker"] == "user"
        )
        self.fail_texts.add(user_utterance["english"])

        prepared = await english_corner.prepare_pack_audio(
            saved["id"],
            db_path=self.db_path,
            audio_dir=self.audio_dir,
        )
        failed = next(
            item
            for item in prepared["items"]
            if item["utterance_id"] == user_utterance["id"]
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["voice"], selected_voice)

        self.fail_texts.clear()
        self.synthesized.clear()
        retried = await english_corner.retry_utterance_audio(
            user_utterance["id"],
            db_path=self.db_path,
            audio_dir=self.audio_dir,
        )

        self.assertEqual(retried["status"], "ready")
        self.assertEqual(retried["voice"], selected_voice)
        self.assertEqual(
            self.synthesized,
            [
                (
                    user_utterance["english"],
                    selected_voice,
                    Path(failed["file_path"]),
                    {},
                )
            ],
        )

    async def test_missing_ai_audio_rows_are_persisted_failed_but_user_stays_null(self):
        """Catches partial/crashed preparation leaving AI utterances unrecoverable."""
        reloaded = await english_corner.get_pack_by_request_id(
            "tts-pack",
            db_path=self.db_path,
        )

        ai_utterances = [
            utterance
            for card in reloaded["cards"]
            for utterance in card["utterances"]
            if utterance["speaker"] in {"aion", "connor"}
        ]
        user_utterances = [
            utterance
            for card in reloaded["cards"]
            for utterance in card["utterances"]
            if utterance["speaker"] == "user"
        ]
        self.assertTrue(ai_utterances)
        self.assertTrue(
            all(
                utterance["audio"]
                and utterance["audio"]["status"] == "failed"
                for utterance in ai_utterances
            )
        )
        self.assertTrue(
            all(utterance["audio"] is None for utterance in user_utterances)
        )
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT utterance.speaker, audio.status
                FROM english_learning_utterances AS utterance
                LEFT JOIN english_learning_audio AS audio
                    ON audio.utterance_id = utterance.id
                ORDER BY utterance.id
                """
            )
            persisted = await cursor.fetchall()
        self.assertTrue(
            all(
                status == "failed"
                for speaker, status in persisted
                if speaker in {"aion", "connor"}
            )
        )
        self.assertTrue(
            all(
                status is None
                for speaker, status in persisted
                if speaker == "user"
            )
        )

    async def test_partial_failure_and_retry_are_isolated_to_one_utterance(self):
        """Catches batch rollback and retries that regenerate unrelated audio."""
        prepare = getattr(english_corner, "prepare_pack_audio", None)
        retry = getattr(english_corner, "retry_utterance_audio", None)
        self.assertTrue(callable(prepare), "pack TTS preparation must exist")
        self.assertTrue(callable(retry), "utterance TTS retry must exist")
        failed_utterance = next(
            utterance
            for utterance in self.utterances
            if utterance["speaker"] == "connor"
        )
        self.fail_texts.add(failed_utterance["english"])

        prepared = await prepare(
            self.saved["id"],
            db_path=self.db_path,
            audio_dir=self.audio_dir,
        )

        by_id = {item["utterance_id"]: item for item in prepared["items"]}
        self.assertEqual(prepared["failed"], 1)
        self.assertEqual(by_id[failed_utterance["id"]]["status"], "failed")
        self.assertEqual(
            by_id[failed_utterance["id"]]["error"],
            "TTS synthesis failed.",
        )
        self.assertFalse(Path(by_id[failed_utterance["id"]]["file_path"]).exists())
        self.assertTrue(
            all(
                item["status"] == "ready"
                for utterance_id, item in by_id.items()
                if utterance_id != failed_utterance["id"]
            )
        )

        self.fail_texts.clear()
        self.synthesized.clear()
        retried = await retry(
            failed_utterance["id"],
            db_path=self.db_path,
            audio_dir=Path(self.temp_dir.name) / "ignored-new-directory",
        )

        self.assertEqual(retried["status"], "ready")
        self.assertEqual(retried["error"], "")
        self.assertEqual(
            self.synthesized,
            [
                (
                    failed_utterance["english"],
                    "voice-connor-v1",
                    Path(by_id[failed_utterance["id"]]["file_path"]),
                    {},
                )
            ],
        )
        user_utterance = next(
            utterance
            for utterance in self.utterances
            if utterance["speaker"] == "user"
        )
        with self.assertRaisesRegex(
            english_corner.EnglishCornerValidationError,
            "user",
        ):
            await retry(
                user_utterance["id"],
                db_path=self.db_path,
                audio_dir=self.audio_dir,
            )
        with self.assertRaisesRegex(
            english_corner.EnglishCornerNotFoundError,
            "999999",
        ):
            await retry(
                999999,
                db_path=self.db_path,
                audio_dir=self.audio_dir,
            )

    async def test_provider_exception_secret_is_not_persisted(self):
        """Catches raw provider exception text being stored in the audio row."""
        provider_secret = "TTS_PROVIDER_SECRET_DO_NOT_STORE"
        ai_utterance = next(
            utterance
            for utterance in self.utterances
            if utterance["speaker"] == "aion"
        )
        synthesize = AsyncMock(
            side_effect=RuntimeError(
                f"authorization failed: {provider_secret}"
            )
        )

        with patch("tts.synthesize_text_to_mp3", new=synthesize):
            failed = await english_corner.retry_utterance_audio(
                ai_utterance["id"],
                db_path=self.db_path,
                audio_dir=self.audio_dir,
            )

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "TTS synthesis failed.")
        self.assertNotIn(provider_secret, failed["error"])
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT error FROM english_learning_audio WHERE utterance_id = ?",
                (ai_utterance["id"],),
            )
            stored_error = (await cursor.fetchone())[0]
        self.assertEqual(stored_error, "TTS synthesis failed.")
        self.assertNotIn(provider_secret, stored_error)

    async def test_missing_voice_marks_only_ai_rows_and_can_be_configured_later(self):
        """Catches fallback voices and missing-voice failures blocking other speakers."""
        prepare = getattr(english_corner, "prepare_pack_audio", None)
        retry = getattr(english_corner, "retry_utterance_audio", None)
        self.assertTrue(callable(prepare), "pack TTS preparation must exist")
        self.assertTrue(callable(retry), "utterance TTS retry must exist")
        self.voice_config.return_value = {
            "tts_aion_voice": "voice-aion-v1",
            "tts_connor_voice": "",
        }

        prepared = await prepare(
            self.saved["id"],
            db_path=self.db_path,
            audio_dir=self.audio_dir,
        )

        for item in prepared["items"]:
            if item["speaker"] == "aion":
                self.assertEqual(item["status"], "ready")
            else:
                self.assertEqual(item["status"], "failed")
                self.assertEqual(item["voice"], "")
                self.assertIn("voice", item["error"].lower())
        self.assertEqual(
            {voice for _, voice, _, _ in self.synthesized},
            {"voice-aion-v1"},
        )

        failed_connor = next(
            item for item in prepared["items"] if item["speaker"] == "connor"
        )
        self.voice_config.return_value = {
            "tts_aion_voice": "voice-aion-v2",
            "tts_connor_voice": "voice-connor-v2",
        }
        self.synthesized.clear()
        retried = await retry(
            failed_connor["utterance_id"],
            db_path=self.db_path,
            audio_dir=self.audio_dir,
        )

        self.assertEqual(retried["status"], "ready")
        self.assertEqual(retried["voice"], "voice-connor-v2")
        self.assertEqual(len(self.synthesized), 1)
        remaining_failed = [
            item
            for item in (
                await prepare(
                    self.saved["id"],
                    db_path=self.db_path,
                    audio_dir=self.audio_dir,
                )
            )["items"]
            if item["status"] == "failed"
        ]
        self.assertEqual(len(remaining_failed), 0)

    async def test_routes_retry_with_domain_statuses_and_serve_only_recorded_ready_path(self):
        """Catches ID-derived serving paths and ambiguous retry HTTP errors."""
        try:
            from routes import english_corner as english_corner_routes
        except ImportError:
            english_corner_routes = None
        self.assertIsNotNone(
            english_corner_routes,
            "English-corner audio router must exist",
        )
        retry_route = getattr(
            english_corner_routes,
            "retry_utterance_audio_route",
            None,
        )
        audio_route = getattr(
            english_corner_routes,
            "get_utterance_audio_route",
            None,
        )
        self.assertTrue(callable(retry_route), "audio retry route must exist")
        self.assertTrue(callable(audio_route), "audio serving route must exist")
        registered = {
            (route.path, method)
            for route in english_corner_routes.router.routes
            for method in route.methods
        }
        self.assertIn(
            ("/api/english-corner/audio/{utterance_id}/retry", "POST"),
            registered,
        )
        self.assertIn(
            ("/api/english-corner/audio/{utterance_id}", "GET"),
            registered,
        )
        self.assertIn(
            ("/api/english-corner/audio/{utterance_id}", "HEAD"),
            registered,
        )

        ai_utterance = next(
            utterance
            for utterance in self.utterances
            if utterance["speaker"] == "aion"
        )
        recorded_path = Path(self.temp_dir.name) / "stored-original-voice.mp3"
        recorded_path.write_bytes(b"ID3-recorded")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE english_learning_audio
                SET speaker = 'aion',
                    voice = 'historic-voice',
                    file_path = ?,
                    status = 'ready',
                    error = '',
                    updated_at = 1.0
                WHERE utterance_id = ?
                """,
                (str(recorded_path), ai_utterance["id"]),
            )
            await db.commit()

        response = await audio_route(
            ai_utterance["id"],
            db_path=self.db_path,
        )
        self.assertEqual(Path(response.path), recorded_path)
        self.assertEqual(response.media_type, "audio/mpeg")

        user_utterance = next(
            utterance
            for utterance in self.utterances
            if utterance["speaker"] == "user"
        )
        with self.assertRaises(Exception) as user_error:
            await retry_route(
                user_utterance["id"],
                db_path=self.db_path,
                audio_dir=self.audio_dir,
            )
        self.assertEqual(getattr(user_error.exception, "status_code", None), 400)
        with self.assertRaises(Exception) as missing_error:
            await retry_route(
                999999,
                db_path=self.db_path,
                audio_dir=self.audio_dir,
            )
        self.assertEqual(getattr(missing_error.exception, "status_code", None), 404)
        with self.assertRaises(Exception) as missing_audio_error:
            await audio_route(999999, db_path=self.db_path)
        self.assertEqual(
            getattr(missing_audio_error.exception, "status_code", None),
            404,
        )

    async def test_deleted_ready_file_becomes_persisted_retryable_404(self):
        """Catches stale ready metadata trapping playback in a permanent 404."""
        from routes import english_corner as english_corner_routes

        prepared = await english_corner.prepare_pack_audio(
            self.saved["id"],
            db_path=self.db_path,
            audio_dir=self.audio_dir,
        )
        target = prepared["items"][0]
        Path(target["file_path"]).unlink()

        with self.assertRaises(Exception) as missing_file:
            await english_corner_routes.get_utterance_audio_route(
                target["utterance_id"],
                db_path=self.db_path,
            )

        self.assertEqual(
            getattr(missing_file.exception, "status_code", None),
            404,
        )
        detail = getattr(missing_file.exception, "detail", None)
        self.assertEqual(
            set(detail),
            {"id", "utterance_id", "status", "message", "retry_url"},
        )
        self.assertEqual(detail["id"], target["id"])
        self.assertEqual(detail["utterance_id"], target["utterance_id"])
        self.assertEqual(detail["status"], "failed")
        self.assertIn("retry", detail["message"].lower())
        self.assertEqual(
            detail["retry_url"],
            (
                "/api/english-corner/audio/"
                f"{target['utterance_id']}/retry"
            ),
        )
        persisted = await english_corner.get_utterance_audio(
            target["utterance_id"],
            db_path=self.db_path,
        )
        self.assertEqual(persisted["status"], "failed")

        self.synthesized.clear()
        retried = await english_corner_routes.retry_utterance_audio_route(
            target["utterance_id"],
            db_path=self.db_path,
            audio_dir=self.audio_dir,
        )
        self.assertEqual(retried["status"], "ready")
        self.assertEqual(len(self.synthesized), 1)

    async def test_retry_that_still_fails_returns_safe_targeted_retry(self):
        """Catches HTTP 200 failed retries being mistaken for ready playback."""
        from routes import english_corner as english_corner_routes

        target = next(
            utterance
            for utterance in self.utterances
            if utterance["speaker"] == "aion"
        )
        self.fail_texts.add(target["english"])

        result = await english_corner_routes.retry_utterance_audio_route(
            target["id"],
            db_path=self.db_path,
            audio_dir=self.audio_dir,
        )

        self.assertEqual(
            set(result),
            {"id", "utterance_id", "status", "message", "retry_url"},
        )
        self.assertIsInstance(result["id"], int)
        self.assertEqual(result["utterance_id"], target["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("retry", result["message"].lower())
        self.assertEqual(
            result["retry_url"],
            f"/api/english-corner/audio/{target['id']}/retry",
        )
        self.assertNotIn("voice", result)
        self.assertNotIn("file_path", result)
        self.assertNotIn("error", result)

    async def test_generation_persists_pack_when_tts_fails_without_second_ai_call(self):
        """Catches TTS errors rolling back text or causing learning-card regeneration."""
        main_call = AsyncMock(
            return_value=EnglishCornerGenerationParserTests.VALID_PAYLOAD
        )
        count = AsyncMock(return_value=0)
        self.fail_texts.update(
            utterance["english"]
            for card in EnglishCornerPersistenceTests.PACK_PAYLOAD["cards"]
            for utterance in card["utterances"]
            if utterance["speaker"] in {"aion", "connor"}
        )
        self.fail_texts.add("Let's check the kitchen counter once more.")

        with (
            patch("context_builder.count_merged_timeline", new=count),
            patch(
                "config.load_worldbook",
                return_value={"user_persona": "", "ai_persona": ""},
            ),
            patch(
                "chatroom.get_chatroom_names",
                return_value=("Configured User", "Configured Main", "Configured Second"),
            ),
            patch("chatroom._read_connor_persona", return_value=""),
            patch("ai_providers.simple_ai_call", new=main_call),
        ):
            generated = await english_corner.generate_learning_pack(
                "aion",
                0,
                "tts-failure-generation",
                now=datetime(2026, 7, 25, 11, 0, 0),
            )
            duplicate = await english_corner.generate_learning_pack(
                "aion",
                0,
                "tts-failure-generation",
                now=datetime(2026, 7, 25, 12, 0, 0),
            )

        self.assertEqual(duplicate, generated)
        self.assertEqual(len(generated["cards"]), 3)
        main_call.assert_awaited_once()
        persisted = await english_corner.get_pack_by_request_id(
            "tts-failure-generation",
            db_path=self.db_path,
        )
        self.assertIsNotNone(persisted)
        ai_audio = [
            utterance["audio"]
            for card in persisted["cards"]
            for utterance in card["utterances"]
            if utterance["speaker"] in {"aion", "connor"}
        ]
        self.assertTrue(ai_audio)
        self.assertTrue(
            all(item is not None for item in ai_audio),
            "newly generated AI utterances must receive persistent audio rows",
        )
        self.assertTrue(all(item["status"] == "failed" for item in ai_audio))


class EnglishCornerApiTests(unittest.IsolatedAsyncioTestCase):
    """HTTP contracts backed by an isolated real SQLite database."""

    MODEL_SECRET = "MODEL_SECRET_DO_NOT_EXPOSE"
    VOICE_SECRET = "VOICE_SECRET_DO_NOT_EXPOSE"
    ERROR_SECRET = "ERROR_SECRET_DO_NOT_EXPOSE"
    PATH_SECRET = "PATH_SECRET_DO_NOT_EXPOSE"

    async def asyncSetUp(self):
        from routes import english_corner as english_corner_routes

        self.routes = english_corner_routes
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "api.sqlite3"
        self.audio_dir = Path(self.temp_dir.name) / "audio"
        async with aiosqlite.connect(self.db_path) as db:
            await english_corner.ensure_english_corner_tables(db)
            await db.execute(
                """
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            await db.execute(
                "INSERT INTO conversations VALUES ('api-conv', 'API-Model', 1.0)"
            )
            await db.commit()

        self.saved = await english_corner.save_learning_pack(
            copy.deepcopy(EnglishCornerPersistenceTests.PACK_PAYLOAD),
            request_id="api-seed",
            generator="aion",
            context_meta=dict(EnglishCornerPersistenceTests.CONTEXT_META),
            db_path=self.db_path,
        )
        self.open_db_patch = patch.object(
            english_corner,
            "_open_english_corner_db",
            new=lambda db_path=None: aiosqlite.connect(self.db_path),
        )
        self.application_db_patch = patch(
            "database.get_db",
            new=lambda: aiosqlite.connect(self.db_path),
        )
        self.tts_config_patch = patch.object(
            english_corner,
            "_load_tts_voice_config",
            return_value={"tts_aion_voice": "", "tts_connor_voice": ""},
        )
        self.open_db_patch.start()
        self.application_db_patch.start()
        self.tts_config_patch.start()

        self.app = FastAPI()
        self.app.include_router(self.routes.router)
        self.app.dependency_overrides[
            self.routes.get_english_corner_db_path
        ] = lambda: self.db_path
        self.app.dependency_overrides[
            self.routes.get_english_corner_audio_dir
        ] = lambda: self.audio_dir
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tts_config_patch.stop()
        self.application_db_patch.stop()
        self.open_db_patch.stop()
        self.temp_dir.cleanup()

    async def _seed_adversarial_internal_audio(self):
        ai_utterances = [
            utterance
            for card in self.saved["cards"]
            for utterance in card["utterances"]
            if utterance["speaker"] in {"aion", "connor"}
        ]
        ready_utterance, failed_utterance = ai_utterances[:2]
        ready_path = self.audio_dir / f"{self.PATH_SECRET}-ready.mp3"
        failed_path = self.audio_dir / f"{self.PATH_SECRET}-failed.mp3"
        ready_path.parent.mkdir(parents=True, exist_ok=True)
        ready_path.write_bytes(b"ID3-adversarial")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE english_learning_packs SET model_key = ? WHERE id = ?",
                (self.MODEL_SECRET, self.saved["id"]),
            )
            await db.execute(
                """
                UPDATE english_learning_audio
                SET speaker = ?,
                    voice = ?,
                    file_path = ?,
                    status = 'ready',
                    error = ?,
                    updated_at = 1.0
                WHERE utterance_id = ?
                """,
                (
                    ready_utterance["speaker"],
                    self.VOICE_SECRET,
                    str(ready_path),
                    self.ERROR_SECRET,
                    ready_utterance["id"],
                ),
            )
            await db.execute(
                """
                UPDATE english_learning_audio
                SET speaker = ?,
                    voice = ?,
                    file_path = ?,
                    status = 'failed',
                    error = ?,
                    updated_at = 1.0
                WHERE utterance_id = ?
                """,
                (
                    failed_utterance["speaker"],
                    self.VOICE_SECRET,
                    str(failed_path),
                    self.ERROR_SECRET,
                    failed_utterance["id"],
                ),
            )
            await db.commit()
        return {
            "ready": {
                "id": ready_utterance["audio"]["id"],
                "utterance_id": ready_utterance["id"],
            },
            "failed": {
                "id": failed_utterance["audio"]["id"],
                "utterance_id": failed_utterance["id"],
            },
        }

    def _assert_public_payload_has_no_internal_fields(self, payload, text):
        forbidden_keys = {
            "model_key",
            "tts_voice",
            "voice",
            "file_path",
            "error",
        }

        def visit(value):
            if isinstance(value, dict):
                self.assertTrue(
                    forbidden_keys.isdisjoint(value),
                    f"internal keys leaked: {forbidden_keys.intersection(value)}",
                )
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(payload)
        for secret in (
            self.MODEL_SECRET,
            self.VOICE_SECRET,
            self.ERROR_SECRET,
            self.PATH_SECRET,
        ):
            self.assertNotIn(secret, text)

    async def test_cards_public_payload_omits_internal_model_audio_and_error_fields(self):
        """Catches GET cards exposing model routes, voices, paths, or raw failures."""
        seeded = await self._seed_adversarial_internal_audio()

        response = await self.client.get(
            "/api/english-corner/cards",
            params={"status": "learning", "limit": 3, "offset": 0},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self._assert_public_payload_has_no_internal_fields(
            payload,
            response.text,
        )
        audio_by_utterance = {
            utterance["id"]: utterance["audio"]
            for card in payload["items"]
            for utterance in card["utterances"]
            if utterance["audio"] is not None
        }
        self.assertEqual(
            audio_by_utterance[seeded["ready"]["utterance_id"]],
            {
                "id": seeded["ready"]["id"],
                "utterance_id": seeded["ready"]["utterance_id"],
                "status": "ready",
                "url": (
                    "/api/english-corner/audio/"
                    f"{seeded['ready']['utterance_id']}"
                ),
            },
        )
        failed_audio = audio_by_utterance[
            seeded["failed"]["utterance_id"]
        ]
        self.assertEqual(
            {
                key: failed_audio[key]
                for key in ("id", "utterance_id", "status")
            },
            {
                "id": seeded["failed"]["id"],
                "utterance_id": seeded["failed"]["utterance_id"],
                "status": "failed",
            },
        )
        self.assertNotIn("url", failed_audio)
        self.assertIn("retry", failed_audio["message"].lower())

    async def test_cards_expose_missing_ai_audio_as_safe_targeted_retry(self):
        """Catches public null audio leaving an AI utterance with no recovery path."""
        response = await self.client.get(
            "/api/english-corner/cards",
            params={"status": "learning", "limit": 3, "offset": 0},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self._assert_public_payload_has_no_internal_fields(
            payload,
            response.text,
        )
        ai_utterances = [
            utterance
            for card in payload["items"]
            for utterance in card["utterances"]
            if utterance["speaker"] in {"aion", "connor"}
        ]
        user_utterances = [
            utterance
            for card in payload["items"]
            for utterance in card["utterances"]
            if utterance["speaker"] == "user"
        ]
        self.assertTrue(ai_utterances)
        for utterance in ai_utterances:
            self.assertEqual(utterance["audio"]["status"], "failed")
            self.assertEqual(
                utterance["audio"]["utterance_id"],
                utterance["id"],
            )
            self.assertEqual(
                utterance["audio"]["retry_url"],
                f"/api/english-corner/audio/{utterance['id']}/retry",
            )
        self.assertTrue(
            all(utterance["audio"] is None for utterance in user_utterances)
        )

    async def test_pack_public_payload_omits_adversarial_internal_fields(self):
        """Catches idempotent POST packs returning the raw persisted pack."""
        await self._seed_adversarial_internal_audio()

        response = await self.client.post(
            "/api/english-corner/packs",
            json={
                "actor": "aion",
                "context_limit": 0,
                "request_id": "api-seed",
                "tts_voice": "speech:custom:must-stay-private",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self._assert_public_payload_has_no_internal_fields(
            response.json(),
            response.text,
        )

    async def test_status_public_payload_omits_adversarial_audio_fields(self):
        """Catches status writes returning a raw nested card with audio secrets."""
        await self._seed_adversarial_internal_audio()
        card_id = self.saved["cards"][0]["id"]

        response = await self.client.patch(
            f"/api/english-corner/cards/{card_id}/status",
            json={"status": "learned"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self._assert_public_payload_has_no_internal_fields(
            response.json(),
            response.text,
        )

    async def test_audio_retry_returns_only_safe_same_origin_metadata(self):
        """Catches retry returning stored voice, absolute path, or raw error."""
        seeded = await self._seed_adversarial_internal_audio()

        response = await self.client.post(
            "/api/english-corner/audio/"
            f"{seeded['ready']['utterance_id']}/retry"
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self._assert_public_payload_has_no_internal_fields(
            payload,
            response.text,
        )
        self.assertEqual(
            payload,
            {
                "id": seeded["ready"]["id"],
                "utterance_id": seeded["ready"]["utterance_id"],
                "status": "ready",
                "url": (
                    "/api/english-corner/audio/"
                    f"{seeded['ready']['utterance_id']}"
                ),
            },
        )

    async def test_new_user_audio_retry_url_serves_audio_but_legacy_user_stays_hidden(self):
        """Catches ready user audio being advertised through an unreadable URL."""
        unified = await english_corner.save_learning_pack(
            copy.deepcopy(EnglishCornerPersistenceTests.PACK_PAYLOAD),
            request_id="api-unified-user-audio",
            generator="aion",
            tts_voice="speech:custom:english-trained",
            context_meta=dict(EnglishCornerPersistenceTests.CONTEXT_META),
            db_path=self.db_path,
        )
        user_utterance = next(
            utterance
            for card in unified["cards"]
            for utterance in card["utterances"]
            if utterance["speaker"] == "user"
        )

        async def synthesize_user_audio(text, voice, output_path, **_kwargs):
            self.assertEqual(text, user_utterance["english"])
            self.assertEqual(voice, "speech:custom:english-trained")
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"ID3-user-audio")

        with patch(
            "tts.synthesize_text_to_mp3",
            new=synthesize_user_audio,
        ):
            retried = await self.client.post(
                f"/api/english-corner/audio/{user_utterance['id']}/retry"
            )

        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["status"], "ready")
        audio_url = retried.json()["url"]
        served = await self.client.get(audio_url)
        self.assertEqual(served.status_code, 200, served.text)
        self.assertEqual(served.content, b"ID3-user-audio")

        legacy_user = next(
            utterance
            for card in self.saved["cards"]
            for utterance in card["utterances"]
            if utterance["speaker"] == "user"
        )
        legacy = await self.client.get(
            f"/api/english-corner/audio/{legacy_user['id']}"
        )
        self.assertEqual(legacy.status_code, 404, legacy.text)

    async def test_overview_and_card_pages_are_server_authoritative_and_public(self):
        """Catches stale counts, flat cards, or leaked persona/model/voice configuration."""
        with patch(
            "chatroom.get_chatroom_names",
            return_value=(
                "Private User",
                "Configured Main",
                "Configured Second",
            ),
        ):
            overview = await self.client.get("/api/english-corner/overview")

        self.assertEqual(overview.status_code, 200, overview.text)
        payload = overview.json()
        self.assertEqual(
            payload["counts"],
            {"learning": 3, "learned": 0},
        )
        self.assertEqual(
            payload["actors"],
            [
                {
                    "id": "aion",
                    "name": "Configured Main",
                    "avatar_url": "/public/gropicon1.png",
                },
                {
                    "id": "connor",
                    "name": "Configured Second",
                    "avatar_url": "/public/codexicon.png",
                },
            ],
        )
        self.assertEqual(
            payload["participants"],
            [
                {
                    "id": "user",
                    "name": "Private User",
                    "avatar_url": "/public/UserIcon.png",
                },
                {
                    "id": "aion",
                    "name": "Configured Main",
                    "avatar_url": "/public/gropicon1.png",
                },
                {
                    "id": "connor",
                    "name": "Configured Second",
                    "avatar_url": "/public/codexicon.png",
                },
            ],
        )
        self.assertEqual(
            [actor["id"] for actor in payload["actors"]],
            ["aion", "connor"],
        )
        for participant in payload["participants"]:
            avatar_url = participant["avatar_url"]
            self.assertTrue(avatar_url.startswith("/public/"))
            self.assertNotIn("://", avatar_url)
            self.assertNotIn("..", avatar_url)
            self.assertNotIn("\\", avatar_url)
        serialized = overview.text.lower()
        for private_fragment in (
            "persona",
            "model",
            "voice",
            "secret",
        ):
            self.assertNotIn(private_fragment, serialized)

        page = await self.client.get(
            "/api/english-corner/cards",
            params={"status": "learning", "limit": 2, "offset": 1},
        )
        self.assertEqual(page.status_code, 200, page.text)
        page_payload = page.json()
        self.assertEqual(
            {
                key: page_payload[key]
                for key in ("total", "limit", "offset")
            },
            {"total": 3, "limit": 2, "offset": 1},
        )
        self.assertEqual(len(page_payload["items"]), 2)
        self.assertEqual(len(page_payload["items"][0]["utterances"]), 2)
        self.assertEqual(len(page_payload["items"][0]["vocabulary"]), 1)
        self.assertIn("audio", page_payload["items"][0]["utterances"][0])

    async def test_query_and_body_validation_rejects_invalid_contract_values(self):
        """Catches unsupported actors/statuses and unbounded or blank request fields."""
        invalid_requests = (
            ("GET", "/api/english-corner/context-options?actor=user", None),
            ("GET", "/api/english-corner/cards?status=archived", None),
            ("GET", "/api/english-corner/cards?status=learning&limit=0", None),
            ("GET", "/api/english-corner/cards?status=learning&limit=101", None),
            ("GET", "/api/english-corner/cards?status=learning&offset=-1", None),
            (
                "POST",
                "/api/english-corner/packs",
                {
                    "actor": "user",
                    "context_limit": 0,
                    "request_id": "valid",
                    "tts_voice": "voice-english",
                },
            ),
            (
                "POST",
                "/api/english-corner/packs",
                {
                    "actor": "aion",
                    "context_limit": 0,
                    "request_id": "   ",
                    "tts_voice": "voice-english",
                },
            ),
            (
                "POST",
                "/api/english-corner/packs",
                {
                    "actor": "aion",
                    "context_limit": -1,
                    "request_id": "valid",
                    "tts_voice": "voice-english",
                },
            ),
            (
                "POST",
                "/api/english-corner/packs",
                {
                    "actor": "aion",
                    "context_limit": 0,
                    "request_id": "missing-voice",
                },
            ),
            (
                "POST",
                "/api/english-corner/packs",
                {
                    "actor": "aion",
                    "context_limit": 0,
                    "request_id": "blank-voice",
                    "tts_voice": "   ",
                },
            ),
            (
                "PATCH",
                f"/api/english-corner/cards/{self.saved['cards'][0]['id']}/status",
                {"status": "archived"},
            ),
        )
        for method, url, body in invalid_requests:
            with self.subTest(method=method, url=url):
                response = await self.client.request(method, url, json=body)
                self.assertEqual(response.status_code, 422, response.text)

        with patch(
            "context_builder.count_merged_timeline",
            new=AsyncMock(return_value=34),
        ):
            impossible_limit = await self.client.post(
                "/api/english-corner/packs",
                json={
                    "actor": "aion",
                    "context_limit": 25,
                    "request_id": "impossible-limit",
                    "tts_voice": "voice-english",
                },
            )
        self.assertEqual(impossible_limit.status_code, 400)
        self.assertIn("context limit", impossible_limit.text.lower())

    async def test_context_options_and_idempotent_pack_generation_use_domain_service(self):
        """Catches incorrect context options or HTTP retries invoking generation twice."""
        context_count = AsyncMock(return_value=34)
        ai_call = AsyncMock(
            return_value=EnglishCornerGenerationParserTests.VALID_PAYLOAD
        )
        with (
            patch("context_builder.count_merged_timeline", new=context_count),
            patch(
                "chatroom.get_chatroom_names",
                return_value=(
                    "Configured User",
                    "Configured Main",
                    "Configured Second",
                ),
            ),
            patch(
                "config.load_worldbook",
                return_value={
                    "user_persona": "",
                    "ai_persona": "",
                },
            ),
            patch("chatroom._read_connor_persona", return_value=""),
            patch("ai_providers.simple_ai_call", new=ai_call),
        ):
            options = await self.client.get(
                "/api/english-corner/context-options",
                params={"actor": "aion"},
            )
            snapshot_end = options.json()["learning_day_end"]
            first = await self.client.post(
                "/api/english-corner/packs",
                json={
                    "actor": "aion",
                    "context_limit": 0,
                    "request_id": " api-idempotent ",
                    "tts_voice": "speech:custom:english-trained",
                    "learning_day_end": snapshot_end,
                },
            )
            duplicate = await self.client.post(
                "/api/english-corner/packs",
                json={
                    "actor": "connor",
                    "context_limit": 0,
                    "request_id": "api-idempotent",
                    "tts_voice": "speech:custom:english-trained",
                    "learning_day_end": snapshot_end,
                },
            )

        self.assertEqual(options.status_code, 200, options.text)
        self.assertEqual(options.json()["context_total"], 34)
        self.assertEqual(options.json()["options"], [10, 20, 30, 34])
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(first.json(), duplicate.json())
        self.assertEqual(first.json()["request_id"], "api-idempotent")
        self.assertEqual(first.json()["learning_day_end"], snapshot_end)
        self.assertEqual(len(first.json()["cards"]), 3)
        self.assertNotIn("tts_voice", first.json())
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT tts_voice
                FROM english_learning_packs
                WHERE request_id = ?
                """,
                ("api-idempotent",),
            )
            self.assertEqual(
                (await cursor.fetchone())[0],
                "speech:custom:english-trained",
            )
        ai_call.assert_awaited_once()

    async def test_status_transitions_and_domain_errors_have_clear_http_statuses(self):
        """Catches unnormalized writes and missing cards reported as generic failures."""
        card_id = self.saved["cards"][0]["id"]
        learned = await self.client.patch(
            f"/api/english-corner/cards/{card_id}/status",
            json={"status": "learned"},
        )
        self.assertEqual(learned.status_code, 200, learned.text)
        self.assertEqual(learned.json()["status"], "learned")
        self.assertIsNotNone(learned.json()["learned_at"])

        missing = await self.client.patch(
            "/api/english-corner/cards/999999/status",
            json={"status": "learning"},
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertIn("999999", missing.text)

    async def test_generation_failure_is_non_200_without_provider_secret(self):
        """Catches provider exception details leaking through the HTTP boundary."""
        with (
            patch(
                "context_builder.count_merged_timeline",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "chatroom.get_chatroom_names",
                return_value=(
                    "Configured User",
                    "Configured Main",
                    "Configured Second",
                ),
            ),
            patch(
                "config.load_worldbook",
                return_value={"user_persona": "", "ai_persona": ""},
            ),
            patch("chatroom._read_connor_persona", return_value=""),
            patch(
                "ai_providers.simple_ai_call",
                new=AsyncMock(
                    side_effect=RuntimeError(
                        "provider failed with token secret-api-token"
                    )
                ),
            ),
        ):
            response = await self.client.post(
                "/api/english-corner/packs",
                json={
                    "actor": "aion",
                    "context_limit": 0,
                    "request_id": "provider-failure",
                    "tts_voice": "voice-english",
                },
            )

        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn("retry", response.text.lower())
        self.assertNotIn("secret-api-token", response.text)

    async def test_main_registers_router_once_and_serves_no_cache_page(self):
        """Catches duplicate router wiring or a cacheable/non-static page response."""
        import main

        registered = [
            route
            for route in main.app.routes
            if getattr(route, "path", None) == "/api/english-corner/overview"
            and "GET" in getattr(route, "methods", set())
        ]
        self.assertEqual(len(registered), 1)
        page = await main.english_corner_page()
        self.assertTrue(str(page.path).endswith("static\\english-corner.html"))
        self.assertEqual(
            page.headers["cache-control"],
            "no-cache, no-store, must-revalidate",
        )


class EnglishCornerCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_default_is_disabled_context_capability(self):
        """Catches the reminder shipping enabled for clean installs or in a wrong group."""
        import capabilities

        definition = capabilities.get_capability_def("english_corner_reminder")
        self.assertIsNotNone(definition)
        self.assertEqual(definition.category, "context")
        self.assertFalse(definition.default_enabled)

    async def test_enabled_nonzero_count_adds_one_natural_dynamic_name_reminder(self):
        """Catches noisy reminders, hardcoded identities, or missing live count."""
        import capabilities

        count = AsyncMock(return_value=7)
        with (
            patch.object(
                capabilities,
                "is_capability_enabled",
                side_effect=lambda key: key == "english_corner_reminder",
            ),
            patch("english_corner.get_unlearned_count", new=count),
        ):
            private_items = await capabilities.build_capability_prompt_items(
                "Dynamic Learner",
                who="aion",
            )
            group_items = await capabilities.build_capability_prompt_items(
                "Dynamic Learner",
                who="connor",
                include_private_whisper=True,
            )

        self.assertEqual(len(private_items), 1)
        self.assertEqual(group_items, private_items)
        reminder = private_items[0]
        for fragment in (
            "7",
            "Dynamic Learner",
            "明确提醒任务",
            "主动提醒",
            "不必等",
            "一直跳过",
            "不要每轮重复",
            "哄",
            "轻微激将",
            "陪学",
            "不要创建日程或闹铃",
            "不要触发空闲自主消息",
        ):
            self.assertIn(fragment, reminder)
        for personal_literal in ("Aion", "Ithil", "Connor"):
            self.assertNotIn(personal_literal, reminder)
        self.assertEqual(count.await_count, 2)

    async def test_enabled_zero_count_injects_nothing(self):
        """Catches an empty English corner still producing reminder noise."""
        import capabilities

        count = AsyncMock(return_value=0)
        with (
            patch.object(
                capabilities,
                "is_capability_enabled",
                side_effect=lambda key: key == "english_corner_reminder",
            ),
            patch("english_corner.get_unlearned_count", new=count),
        ):
            items = await capabilities.build_capability_prompt_items(
                "Dynamic Learner"
            )

        self.assertEqual(items, [])
        count.assert_awaited_once()

    async def test_disabled_capability_never_queries_count(self):
        """Catches disabled reminders retaining a database query or prompt item."""
        import capabilities

        count = AsyncMock(return_value=7)
        with (
            patch.object(
                capabilities,
                "is_capability_enabled",
                return_value=False,
            ),
            patch("english_corner.get_unlearned_count", new=count),
        ):
            items = await capabilities.build_capability_prompt_items(
                "Dynamic Learner"
            )

        self.assertEqual(items, [])
        count.assert_not_awaited()

    async def test_first_bootstrap_enables_existing_install_without_overwriting_choice(self):
        """Catches existing installs staying off or explicit false being overwritten."""
        bootstrap = getattr(
            database,
            "_bootstrap_english_corner_schema",
            None,
        )
        self.assertTrue(callable(bootstrap))
        with TemporaryDirectory() as temp_dir:
            existing_path = Path(temp_dir) / "existing.sqlite3"
            async with aiosqlite.connect(existing_path) as db:
                await db.executescript(
                    """
                    CREATE TABLE conversations (id TEXT PRIMARY KEY);
                    INSERT INTO conversations VALUES ('existing-conversation');
                    """
                )
                settings = {}
                save = Mock()
                changed = await bootstrap(
                    db,
                    settings=settings,
                    persist_settings=save,
                )

                self.assertTrue(changed)
                self.assertEqual(
                    settings["ai_prompt_capabilities"][
                        "english_corner_reminder"
                    ],
                    True,
                )
                save.assert_called_once_with(settings)

            chosen_path = Path(temp_dir) / "chosen.sqlite3"
            async with aiosqlite.connect(chosen_path) as db:
                await db.executescript(
                    """
                    CREATE TABLE conversations (id TEXT PRIMARY KEY);
                    INSERT INTO conversations VALUES ('existing-conversation');
                    """
                )
                settings = {
                    "ai_prompt_capabilities": {
                        "english_corner_reminder": False
                    }
                }
                save = Mock()
                changed = await bootstrap(
                    db,
                    settings=settings,
                    persist_settings=save,
                )

                self.assertFalse(changed)
                self.assertFalse(
                    settings["ai_prompt_capabilities"][
                        "english_corner_reminder"
                    ]
                )
                save.assert_not_called()

    async def test_clean_or_already_bootstrapped_database_does_not_enable_default(self):
        """Catches clean installs or later schema checks mutating capability settings."""
        bootstrap = getattr(
            database,
            "_bootstrap_english_corner_schema",
            None,
        )
        self.assertTrue(callable(bootstrap))
        with TemporaryDirectory() as temp_dir:
            clean_path = Path(temp_dir) / "clean.sqlite3"
            async with aiosqlite.connect(clean_path) as db:
                await db.execute(
                    "CREATE TABLE conversations (id TEXT PRIMARY KEY)"
                )
                settings = {}
                save = Mock()
                changed = await bootstrap(
                    db,
                    settings=settings,
                    persist_settings=save,
                )
                self.assertFalse(changed)
                self.assertEqual(settings, {})
                save.assert_not_called()

                await db.execute(
                    "INSERT INTO conversations VALUES ('created-later')"
                )
                await db.commit()
                changed_later = await bootstrap(
                    db,
                    settings=settings,
                    persist_settings=save,
                )
                self.assertFalse(changed_later)
                self.assertEqual(settings, {})
                save.assert_not_called()

    async def test_first_bootstrap_detects_existing_chatroom_message(self):
        """Catches group-only existing installs being mistaken for clean installs."""
        bootstrap = database._bootstrap_english_corner_schema
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "group-only.sqlite3"
            async with aiosqlite.connect(db_path) as db:
                await db.executescript(
                    """
                    CREATE TABLE chatroom_messages (id TEXT PRIMARY KEY);
                    INSERT INTO chatroom_messages VALUES ('existing-group-message');
                    """
                )
                settings = {}
                save = Mock()

                changed = await bootstrap(
                    db,
                    settings=settings,
                    persist_settings=save,
                )

        self.assertTrue(changed)
        self.assertTrue(
            settings["ai_prompt_capabilities"]["english_corner_reminder"]
        )
        save.assert_called_once_with(settings)
