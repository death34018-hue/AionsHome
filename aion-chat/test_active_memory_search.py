import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from active_memory_search import (
    HARD_BLOCK_LIMIT,
    MemorySearchRequest,
    MemorySearchResult,
    extract_memory_search_requests,
    format_memory_search_context,
    parse_memory_keywords,
    rank_memory_rows,
    resolve_memory_time_window,
    actor_memory_query,
)
from memory import _keyword_match_score


class ActiveMemorySearchTest(unittest.TestCase):
    def test_extracts_multiple_commands_and_limits_queries(self):
        text = (
            "[MEMORY_SEARCH:过敏药|latest|detail]\n"
            "[MEMORY_SEARCH：开斯婷 | date=前天]"
        )
        clean, requests = extract_memory_search_requests(text)
        self.assertEqual("", clean.strip())
        self.assertEqual(["过敏药", "开斯婷"], [item.query for item in requests])
        self.assertEqual("latest", requests[0].mode)
        self.assertTrue(requests[0].include_detail)
        self.assertEqual("前天", requests[1].date_text)

        _, capped = extract_memory_search_requests(
            "\n".join(f"[MEMORY_SEARCH:q{i}]" for i in range(8))
        )
        self.assertEqual(5, len(capped))

    def test_disabled_parser_leaves_command_untouched(self):
        text = "[MEMORY_SEARCH:过敏药|latest]"
        self.assertEqual((text, []), extract_memory_search_requests(text, enabled=False))

    def test_accepts_natural_preface_and_full_width_or_mixed_brackets(self):
        samples = (
            "我翻翻小账本。【MEMORY_SEARCH：辣椒炒肉|latest】",
            "等等，我去找找。［MEMORY_SEARCH:辣椒炒肉|latest］",
            "让我确认一下。【MEMORY_SEARCH：辣椒炒肉|latest]",
        )
        for text in samples:
            with self.subTest(text=text):
                clean, requests = extract_memory_search_requests(text)
                self.assertNotIn("MEMORY_SEARCH", clean)
                self.assertTrue(clean.strip())
                self.assertEqual(["辣椒炒肉"], [item.query for item in requests])

    def test_previous_day_uses_five_am_boundary(self):
        now = datetime(2026, 9, 2, 3, 0).astimezone()
        request = MemorySearchRequest(query="吃了什么", date_text="前天")
        start, end = resolve_memory_time_window(request, now=now)
        self.assertEqual(datetime(2026, 8, 30, 5, 0).astimezone(), datetime.fromtimestamp(start).astimezone())
        self.assertEqual(datetime(2026, 8, 31, 5, 0).astimezone(), datetime.fromtimestamp(end).astimezone())

    def test_parses_json_and_legacy_csv_keywords(self):
        self.assertEqual(["过敏药", "开斯婷"], parse_memory_keywords('["过敏药", "开斯婷"]'))
        self.assertEqual(["Ctrl+X", "小家", "数据库"], parse_memory_keywords("Ctrl+X, 小家，数据库"))

    def test_actor_query_is_backend_bound(self):
        aion_sql, aion_params = actor_memory_query("aion")
        connor_sql, connor_params = actor_memory_query("connor")
        self.assertIn(" FROM memories ", aion_sql)
        self.assertNotIn("chatroom_memories", aion_sql)
        self.assertIn(" FROM chatroom_memories ", connor_sql)
        self.assertIn("scope=?", connor_sql)
        self.assertEqual(("connor",), connor_params)

    def test_legacy_recall_keyword_score_accepts_csv(self):
        self.assertEqual(1.0, _keyword_match_score(["Ctrl+X"], "Ctrl+X, 小家，数据库"))

    def test_rare_exact_shortcut_outranks_common_keyword(self):
        rows = [
            {"id": "right", "content": "把 Ctrl+C 按成 Ctrl+X，数据库险些被剪走", "keywords": "Ctrl+X,小家", "importance": .7, "created_at": 10},
            {"id": "noise1", "content": "在小家里做饭", "keywords": "小家", "importance": .8, "created_at": 20},
            {"id": "noise2", "content": "收拾小家的客厅", "keywords": "小家", "importance": .8, "created_at": 30},
        ]
        results = rank_memory_rows(rows, [MemorySearchRequest("Ctrl+X"), MemorySearchRequest("小家")], actor="aion")
        self.assertEqual("right", results[0].memory_id)
        self.assertIn("关键词", "".join(results[0].hit_reasons))

    def test_latest_only_reorders_relevant_candidates(self):
        rows = [
            {"id": "old", "content": "吃了开斯婷", "keywords": "开斯婷", "importance": .5, "created_at": 10},
            {"id": "new", "content": "又吃了一次开斯婷", "keywords": "开斯婷", "importance": .5, "created_at": 30},
            {"id": "unrelated", "content": "刚刚浇了花", "keywords": "浇花", "importance": 1, "created_at": 40},
        ]
        results = rank_memory_rows(rows, [MemorySearchRequest("开斯婷", mode="latest")], actor="connor")
        self.assertEqual(["new", "old"], [item.memory_id for item in results])

    def test_context_budget_keeps_summaries_and_trims_details(self):
        results = [
            MemorySearchResult(
                memory_id=str(i), store="aion", content=f"候选记忆 {i} " + "摘要" * 200,
                occurred_at=100 + i, score=2 - i / 100, hit_reasons=["关键词精确命中"],
                direct=True, sources=["来源" * 1000] * 3,
            )
            for i in range(10)
        ]
        block = format_memory_search_context(results, "那天发生了什么？")
        self.assertLessEqual(len(block), HARD_BLOCK_LIMIT)
        self.assertIn("候选记忆 9", block)
        self.assertLess(block.count("来源原文"), 10)


if __name__ == "__main__":
    unittest.main()
