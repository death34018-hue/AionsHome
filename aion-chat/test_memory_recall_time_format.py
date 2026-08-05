import sys
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import memory
from memory import _format_raw_evidence_block, _memory_line_with_evidence


def local_ts(value: str) -> float:
    return datetime.strptime(value, "%Y-%m-%d %H:%M").timestamp()


class MemoryRecallTimeFormatTests(unittest.TestCase):
    def test_recalled_memory_includes_source_time_without_repeating_content_date(self):
        rendered = _memory_line_with_evidence(
            {
                "content": "2026-07-08，完成了值得保留的一件事。",
                "source_start_ts": local_ts("2026-07-08 05:00"),
                "source_end_ts": local_ts("2026-07-09 04:59"),
            }
        )

        self.assertEqual(
            rendered,
            "- 记忆（发生：2026-07-08 05:00 至 2026-07-09 04:59）：完成了值得保留的一件事。",
        )
        self.assertEqual(rendered.count("2026-07-08"), 1)

    def test_recalled_memory_uses_record_time_when_source_time_is_missing(self):
        rendered = _memory_line_with_evidence(
            {
                "content": "一条只有记录时间的旧记忆。",
                "created_at": local_ts("2026-06-03 12:30"),
            }
        )

        self.assertEqual(
            rendered,
            "- 记忆（记录：2026-06-03 12:30）：一条只有记录时间的旧记忆。",
        )

    def test_recalled_memory_without_any_time_keeps_plain_content(self):
        rendered = _memory_line_with_evidence({"content": "时间未知的旧记忆。"})

        self.assertEqual(rendered, "- 记忆：时间未知的旧记忆。")

    def test_shared_prompt_formatter_applies_time_labels_to_every_recalled_memory(self):
        self.assertTrue(
            hasattr(memory, "format_recalled_memories_for_prompt"),
            "召回入口需要一个统一的时间格式化函数",
        )

        rendered = memory.format_recalled_memories_for_prompt(
            [
                {
                    "content": "第一条记忆。",
                    "source_start_ts": local_ts("2026-05-01 09:00"),
                },
                {
                    "content": "第二条记忆。",
                    "created_at": local_ts("2026-05-02 10:30"),
                },
            ],
            limit=200,
        )

        self.assertEqual(
            rendered,
            "- 记忆（发生：2026-05-01 09:00）：第一条记忆。\n"
            "- 记忆（记录：2026-05-02 10:30）：第二条记忆。",
        )

    def test_source_evidence_block_does_not_repeat_content_date(self):
        rendered = _format_raw_evidence_block(
            {
                "content": "2026-04-12，去医院复查。",
                "source_start_ts": local_ts("2026-04-12 08:00"),
                "source_end_ts": local_ts("2026-04-12 09:00"),
            },
            [
                {
                    "created_at": local_ts("2026-04-12 08:15"),
                    "name": "用户",
                    "content": "今天去医院复查。",
                }
            ],
        )

        self.assertEqual(rendered.count("2026-04-12"), 1)
        self.assertTrue(rendered.startswith("- 记忆（发生：2026-04-12 08:00-09:00）：去医院复查。"))


if __name__ == "__main__":
    unittest.main()
