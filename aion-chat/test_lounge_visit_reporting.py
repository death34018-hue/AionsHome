import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lounge_visit_reporting import _default_save, publish_inbound_report, publish_outbound_report


class FakeRepository:
    async def get(self, actor_id, visit_id):
        assert actor_id == "aion"
        assert visit_id == "visit-1"
        return {
            "messages": [
                {"direction": "outbound", "content": "我们聊聊最近的生活。"},
                {"direction": "inbound", "content": "我最近开始养花。"},
            ]
        }


def test_default_report_save_disables_tts():
    save = AsyncMock(return_value={"id": "report-message"})
    with patch("autonomy._save_private_message", save):
        message = asyncio.run(
            _default_save(
                "connor",
                "刚才接待了一位朋友。",
                attachments=[{"type": "lounge_visit_report"}],
            )
        )

    assert message == {"id": "report-message"}
    save.assert_awaited_once_with(
        "connor",
        "刚才接待了一位朋友。",
        attachments=[{"type": "lounge_visit_report"}],
        auto_tts=False,
    )


def test_outbound_report_is_generated_by_actor_and_saved_as_card():
    generate = AsyncMock(return_value="我们聊了近况，他最近开始养花。下次可以继续聊植物。")
    save = AsyncMock(return_value={"id": "message-1"})
    result = SimpleNamespace(
        visit_id="visit-1", status="completed", turn_count=1, reason="max_turns"
    )

    message = asyncio.run(
        publish_outbound_report(
            "aion",
            "远方朋友",
            result,
            FakeRepository(),
            generate_summary=generate,
            save_message=save,
        )
    )

    assert message == {"id": "message-1"}
    assert "主要聊了什么" in generate.await_args.args[1]
    content = save.await_args.args[1]
    attachments = save.await_args.kwargs["attachments"]
    assert "开始养花" in content
    assert attachments == [{
        "type": "lounge_visit_report",
        "direction": "outbound",
        "partner_name": "远方朋友",
        "status": "completed",
        "turn_count": 1,
        "summary": content,
    }]


def test_report_falls_back_and_redacts_sensitive_model_output():
    generate = AsyncMock(
        return_value=(
            "Authorization: Bearer private-secret https://private.example "
            "talk_to_host 后聊完了。"
        )
    )
    save = AsyncMock(return_value={"id": "message-2"})
    result = SimpleNamespace(
        visit_id="visit-1", status="interrupted", turn_count=1, reason="connection_failed"
    )

    asyncio.run(
        publish_outbound_report(
            "aion",
            "朋友 https://friend.example",
            result,
            FakeRepository(),
            generate_summary=generate,
            save_message=save,
        )
    )

    content = save.await_args.args[1]
    card = save.await_args.kwargs["attachments"][0]
    for unsafe in ("Authorization", "Bearer", "private-secret", "https://", "talk_to_host"):
        assert unsafe not in content
        assert unsafe not in repr(card)


def test_report_uses_fixed_message_when_generation_fails():
    generate = AsyncMock(side_effect=RuntimeError("model unavailable"))
    save = AsyncMock(return_value={"id": "message-3"})
    result = SimpleNamespace(
        visit_id="visit-1", status="completed", turn_count=1, reason="max_turns"
    )

    asyncio.run(
        publish_outbound_report(
            "aion",
            "远方朋友",
            result,
            FakeRepository(),
            generate_summary=generate,
            save_message=save,
        )
    )

    assert "没有留下足够的可总结内容" in save.await_args.args[1]


def test_inbound_report_is_saved_to_host_chat_as_one_card():
    generate = AsyncMock(return_value="刚才有位朋友来做客，我们聊了养花和近况。")
    save = AsyncMock(return_value={"id": "inbound-message"})

    message = asyncio.run(
        publish_inbound_report(
            "connor",
            "来访朋友",
            [{"direction": "inbound", "content": "最近开始养花"}],
            status="completed",
            turn_count=1,
            generate_summary=generate,
            save_message=save,
        )
    )

    assert message == {"id": "inbound-message"}
    card = save.await_args.kwargs["attachments"][0]
    assert card["direction"] == "inbound"
    assert card["partner_name"] == "来访朋友"
    assert card["summary"] == save.await_args.args[1]
