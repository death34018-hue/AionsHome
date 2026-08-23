import asyncio
from pathlib import Path

import pytest

from capabilities import lounge_visit_ability_text
from lounge_friends import LoungeFriend, LoungeFriendStore
from lounge_visit_commands import (
    LoungeVisitCommandStreamFilter,
    handle_lounge_visit_commands,
    is_chat_visit_friend_allowed,
    is_immediate_lounge_visit_request,
)


def _store_with_friends(tmp_path: Path) -> tuple[LoungeFriendStore, str, str]:
    store = LoungeFriendStore(tmp_path / "lounge_friends.json", clock=lambda: 10.0)
    aion_friend = store.create(
        actor_id="aion",
        display_name="Aion friend",
        lounge_url="https://aion.example/mcp",
        visitor_key="private-key",
        relationship_note="old friend",
        enabled=True,
        allow_autonomous=True,
        cooldown_hours=12,
        max_turns=4,
    )
    connor_friend = store.create(
        actor_id="connor",
        display_name="Connor friend",
        lounge_url="https://connor.example/mcp",
        visitor_key="other-private-key",
        relationship_note="other friend",
        enabled=True,
        allow_autonomous=True,
        cooldown_hours=12,
        max_turns=4,
    )
    store.create(
        actor_id="aion",
        display_name="Disabled friend",
        lounge_url="https://disabled.example/mcp",
        visitor_key="disabled-private-key",
        relationship_note="disabled",
        enabled=False,
        allow_autonomous=True,
        cooldown_hours=12,
        max_turns=4,
    )
    return store, aion_friend.id, connor_friend.id


def test_lounge_ability_lists_owned_enabled_friend_ids_without_keys(tmp_path):
    store, aion_id, connor_id = _store_with_friends(tmp_path)

    text = lounge_visit_ability_text("aion", store)

    assert aion_id in text
    assert connor_id not in text
    assert "Aion friend" in text
    assert "old friend" in text
    assert "private-key" not in text
    assert "disabled-private-key" not in text


def test_lounge_ability_excludes_enabled_friend_without_autonomous_permission(tmp_path):
    store, aion_id, _connor_id = _store_with_friends(tmp_path)
    blocked = store.create(
        actor_id="aion",
        display_name="Prompt blocked friend",
        lounge_url="https://blocked.example/mcp",
        visitor_key="blocked-private-key",
        relationship_note="must stay private",
        enabled=True,
        allow_autonomous=False,
        cooldown_hours=12,
        max_turns=4,
    )

    text = lounge_visit_ability_text("aion", store)

    assert aion_id in text
    assert blocked.id not in text
    assert "Prompt blocked friend" not in text
    assert "must stay private" not in text


def test_lounge_ability_returns_nothing_when_no_owned_friend_allows_autonomy(tmp_path):
    store = LoungeFriendStore(tmp_path / "lounge_friends.json", clock=lambda: 10.0)
    store.create(
        actor_id="aion",
        display_name="Manual only friend",
        lounge_url="https://manual.example/mcp",
        visitor_key="manual-private-key",
        relationship_note="manual visits only",
        enabled=True,
        allow_autonomous=False,
        cooldown_hours=12,
        max_turns=4,
    )

    assert lounge_visit_ability_text("aion", store) == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我给你加了一个好友", False),
        ("一会儿要去拜访谁", False),
        ("之后再去串门吧", False),
        ("现在去拜访他", True),
        ("马上去串门", True),
        ("出发去找他聊聊", True),
    ],
)
def test_immediate_lounge_visit_request_is_conservative(text, expected):
    assert is_immediate_lounge_visit_request(text) is expected


def test_lounge_command_strips_tag_without_starting_for_non_immediate_user_text(tmp_path):
    _store, friend_id, _connor_id = _store_with_friends(tmp_path)
    started = []

    async def start_visit(actor_id: str, selected_friend_id: str, topic: str) -> str:
        started.append((actor_id, selected_friend_id, topic))
        return "visit-id"

    visible, visit_ids = asyncio.run(
        handle_lounge_visit_commands(
            f"知道啦。[LOUNGE_VISIT:{friend_id}|聊聊近况]",
            actor_id="aion",
            user_text="我给你加了一个好友",
            start_visit=start_visit,
        )
    )

    assert visible == "知道啦。"
    assert visit_ids == []
    assert started == []


def test_chat_visit_friend_permission_requires_enabled_and_autonomous(tmp_path):
    store, allowed_id, _connor_id = _store_with_friends(tmp_path)
    manual_only = store.create(
        actor_id="aion",
        display_name="Manual only",
        lounge_url="https://manual-only.example/mcp",
        visitor_key="manual-only-key",
        relationship_note="manual",
        enabled=True,
        allow_autonomous=False,
        cooldown_hours=12,
        max_turns=4,
    )

    assert is_chat_visit_friend_allowed(store.get_owned("aion", allowed_id)) is True
    assert is_chat_visit_friend_allowed(manual_only) is False


def test_lounge_ability_redacts_legacy_key_from_friend_name_and_note():
    key = "legacy-private-key"
    legacy_friend = LoungeFriend(
        id="legacy-friend-id",
        actor_id="aion",
        display_name=f"ordinary name {key}",
        lounge_url="https://legacy.example/mcp",
        visitor_key=key,
        relationship_note=f"ordinary note {key}",
        enabled=True,
        allow_autonomous=True,
        cooldown_hours=12,
        max_turns=4,
        last_visit_at=None,
        created_at=1.0,
        updated_at=1.0,
    )

    class LegacyStore:
        def list_for_actor(self, actor_id):
            assert actor_id == "aion"
            return [legacy_friend]

    text = lounge_visit_ability_text("aion", LegacyStore())

    assert key not in text
    assert legacy_friend.lounge_url not in text
    assert legacy_friend.id in text
    assert "ordinary name" in text
    assert "ordinary note" in text


def test_lounge_command_only_starts_owned_friend_with_a_topic(tmp_path):
    store, aion_id, connor_id = _store_with_friends(tmp_path)
    started: list[tuple[str, str, str]] = []

    async def start_visit(actor_id: str, friend_id: str, topic: str) -> str:
        friend = store.get_owned(actor_id, friend_id)
        if not friend.enabled or not topic:
            return ""
        started.append((actor_id, friend_id, topic))
        return "visit-id"

    visible, visit_ids = asyncio.run(
        handle_lounge_visit_commands(
            (
                f"去拜访。[LOUNGE_VISIT:{aion_id}|聊聊近况]"
                f"[LOUNGE_VISIT:{connor_id}|越权]"
                f"[LOUNGE_VISIT:{aion_id}|   ]"
                "结束。"
            ),
            actor_id="aion",
            user_text="现在去拜访他",
            start_visit=start_visit,
        )
    )

    assert started == [("aion", aion_id, "聊聊近况")]
    assert visit_ids == ["visit-id"]
    assert visible == "去拜访。结束。"


def test_lounge_command_filter_never_exposes_split_protocol_marker(tmp_path):
    _store, friend_id, _connor_id = _store_with_friends(tmp_path)
    stream_filter = LoungeVisitCommandStreamFilter()

    visible = "".join(
        [
            stream_filter.feed("好呀，[LOUNGE_VIS"),
            stream_filter.feed(f"IT:{friend_id}|聊聊"),
            stream_filter.feed("近况]等我回来。"),
            stream_filter.flush(),
        ]
    )

    assert visible == "好呀，等我回来。"


def test_lounge_command_removes_unterminated_trailing_fragment_without_starting_it(tmp_path):
    _store, friend_id, _connor_id = _store_with_friends(tmp_path)
    started: list[tuple[str, str, str]] = []

    async def start_visit(actor_id: str, selected_friend_id: str, topic: str) -> str:
        started.append((actor_id, selected_friend_id, topic))
        return "visit-id"

    visible, visit_ids = asyncio.run(
        handle_lounge_visit_commands(
            (
                f"保留 [普通标签] [LOUNGE_VISIT:{friend_id}|正常拜访]"
                f" 结尾 [LOUNGE_VISIT:{friend_id}|未闭合"
            ),
            actor_id="aion",
            user_text="现在去拜访他",
            start_visit=start_visit,
        )
    )

    assert visible == "保留 [普通标签]  结尾"
    assert started == [("aion", friend_id, "正常拜访")]
    assert visit_ids == ["visit-id"]
