import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_builder import render_merged_timeline


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
