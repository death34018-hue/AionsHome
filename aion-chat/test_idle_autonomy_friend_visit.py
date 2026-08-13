import asyncio
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import autonomy
from lounge_friends import LoungeFriend, LoungeFriendStore
from lounge_visit import LoungeVisitResult


def test_activity_timeline_builds_safe_autonomous_visit_cards():
    html_path = ROOT / "static" / "activity-logs.html"
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync(process.argv[1], 'utf8');
const blocks = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).filter(source => source.trim());
const element = () => ({
  value: '24', checked: false, innerHTML: '', textContent: '', scrollTop: 0,
  scrollHeight: 0, style: {}, classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  addEventListener() {}, getBoundingClientRect() { return { left: 0, right: 0, top: 0, bottom: 0 }; },
});
global.window = global;
global.document = { addEventListener() {}, getElementById() { return element(); } };
global.$ = () => element();
global.api = async () => ({});
global.connectCommonWS = () => {};
global.escHtml = value => String(value ?? '');
global.showToast = () => {};
global.requestAnimationFrame = callback => callback();
global.setTimeout = () => 0;
vm.runInThisContext(blocks.at(-1));
const items = autonomousVisitTimelineItems(
  {
    actors: [{ id: 'aion', display_name: '配置角色' }],
    friends: [{ id: 'friend-1', actor_id: 'aion', display_name: '远方朋友' }],
  },
  { aion: [
    { id: 'visit-1', actor_id: 'aion', friend_id: 'friend-1', trigger_source: 'autonomy', status: 'completed', turn_count: 3, error: '', started_at: 200, finished_at: 210 },
    { id: 'visit-2', actor_id: 'aion', friend_id: 'friend-1', trigger_source: 'manual', status: 'completed', turn_count: 1, error: '', started_at: 220, finished_at: 230 },
    { id: 'visit-3', actor_id: 'aion', friend_id: 'friend-1', trigger_source: 'autonomy', status: 'interrupted', turn_count: 1, error: 'Error: https://private.example talk_to_host Authorization: secret', started_at: 240, finished_at: 250 },
    { id: 'visit-running', actor_id: 'aion', friend_id: 'friend-1', trigger_source: 'autonomy', status: 'running', turn_count: 0, error: '', started_at: 260, finished_at: null },
  ] },
  100,
);
assert.strictEqual(items.length, 2);
assert.deepStrictEqual(items.find(item => item.source_id === 'visit-1'), {
  timestamp: 210,
  time: new Date(210 * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }),
  kind: 'friend_visit',
  actor: '配置角色',
  actor_id: 'aion',
  title: '配置角色拜访“远方朋友”后回家了',
  detail: '好友：远方朋友；回合数：3；状态：完成；原因：拜访顺利结束',
  source_id: 'visit-1',
  attachments: [],
});
const interrupted = items.find(item => item.source_id === 'visit-3');
assert.ok(!interrupted.detail.includes('https://'));
assert.ok(!interrupted.detail.includes('talk_to_host'));
assert.ok(!interrupted.detail.includes('Authorization'));
"""
    result = subprocess.run(
        ["node", "-e", script, str(html_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr


def friend(**changes):
    fields = {
        "id": "friend-owned",
        "actor_id": "aion",
        "display_name": "远方朋友",
        "lounge_url": "https://friend.example/mcp",
        "visitor_key": "private-visitor-key",
        "relationship_note": "很久没聊的朋友",
        "enabled": True,
        "allow_autonomous": True,
        "cooldown_hours": 12,
        "max_turns": 4,
        "last_visit_at": None,
        "created_at": 100.0,
        "updated_at": 100.0,
    }
    fields.update(changes)
    return LoungeFriend(**fields)


def test_friend_is_ineligible_until_twelve_hour_cooldown_passes(tmp_path):
    now = 50_000.0
    store = LoungeFriendStore(tmp_path / "friends.json", clock=lambda: now)
    owned = store.create(
        actor_id="aion",
        display_name="远方朋友",
        lounge_url="https://friend.example/mcp",
        visitor_key="private-visitor-key",
        relationship_note="很久没聊的朋友",
        enabled=True,
        allow_autonomous=True,
        cooldown_hours=12,
        max_turns=4,
    )
    store.mark_visited("aion", owned.id, now - 12 * 60 * 60 + 1)

    assert store.eligible_for_autonomy("aion") == []


def test_choose_friend_rejects_id_outside_current_actor_and_exposes_only_safe_fields(monkeypatch):
    prompts = []
    owned = friend()

    async def fake_ask(_actor, instruction, **_kwargs):
        prompts.append(instruction)
        return {
            "friend_id": "friend-owned-by-another-actor",
            "topic": "聊聊近况",
            "reason": "想去问候",
        }

    monkeypatch.setattr(autonomy, "_ask_actor_json", fake_ask)

    selected = asyncio.run(autonomy._choose_lounge_friend("aion", [owned]))

    assert selected == ("", "")
    assert prompts
    assert owned.id in prompts[0]
    assert owned.display_name in prompts[0]
    assert owned.relationship_note in prompts[0]
    assert owned.visitor_key not in prompts[0]
    assert owned.lounge_url not in prompts[0]
    assert "talk_to_host" not in prompts[0]


def test_choose_friend_redacts_legacy_key_from_name_and_note(monkeypatch):
    prompts = []
    key = "legacy-private-key"
    owned = friend(
        display_name=f"ordinary name {key}",
        visitor_key=key,
        relationship_note=f"ordinary note {key}",
    )

    async def fake_ask(_actor, instruction, **_kwargs):
        prompts.append(instruction)
        return {"friend_id": owned.id, "topic": "ordinary topic", "reason": "visit"}

    monkeypatch.setattr(autonomy, "_ask_actor_json", fake_ask)

    selected = asyncio.run(autonomy._choose_lounge_friend("aion", [owned]))

    assert selected == (owned.id, "ordinary topic")
    assert key not in prompts[0]
    assert owned.lounge_url not in prompts[0]
    assert owned.id in prompts[0]
    assert "ordinary name" in prompts[0]
    assert "ordinary note" in prompts[0]


def test_run_friend_visit_revalidates_owned_friend_before_coordinator(monkeypatch):
    selected_friend = friend()

    class FakeStore:
        def eligible_for_autonomy(self, actor):
            assert actor == "aion"
            return [selected_friend]

        def get_owned(self, actor, friend_id):
            assert actor == "aion"
            assert friend_id == "friend-owned-by-another-actor"
            raise KeyError("Friend not found")

    monkeypatch.setattr(autonomy, "_lounge_friend_store", lambda: FakeStore())
    monkeypatch.setattr(
        autonomy,
        "_choose_lounge_friend",
        AsyncMock(return_value=("friend-owned-by-another-actor", "聊聊近况")),
    )

    result = asyncio.run(autonomy._run_friend_visit("aion"))

    assert result == {"cancelled": True, "reason": "friend_unavailable"}


def test_run_friend_visit_uses_autonomy_trigger_and_saves_safe_homecoming(monkeypatch):
    selected_friend = friend(display_name="远方朋友https://friend-name.example/talk_to_host")
    calls = []
    events = []
    saved_message = {"id": "msg-home"}

    class FakeStore:
        def eligible_for_autonomy(self, actor):
            assert actor == "aion"
            return [selected_friend]

        def get_owned(self, actor, friend_id):
            assert (actor, friend_id) == ("aion", selected_friend.id)
            return selected_friend

    class FakeCoordinator:
        def __init__(self, store, repository, mcp, actor_name_resolver):
            assert isinstance(store, FakeStore)
            assert repository == "repository"
            assert actor_name_resolver("aion") == "配置角色https://actor-name.example/talk_to_host"

        async def run_visit(self, actor, friend_id, trigger, topic, compose_next):
            calls.append((actor, friend_id, trigger, topic, compose_next))
            return LoungeVisitResult(
                "visit-1",
                "interrupted",
                2,
                "",
                "https://private.example Authorization: secret talk_to_host",
            )

    @asynccontextmanager
    async def repository_provider():
        yield "repository"

    async def fake_append(*args, **kwargs):
        events.append((args, kwargs))
        return {"id": "idle-visit"}

    from routes import lounge_friends as lounge_routes

    monkeypatch.setattr(autonomy, "_lounge_friend_store", lambda: FakeStore())
    monkeypatch.setattr(
        autonomy,
        "_choose_lounge_friend",
        AsyncMock(return_value=(selected_friend.id, "聊聊近况")),
    )
    monkeypatch.setattr(
        autonomy,
        "_actor_label",
        lambda actor: "配置角色https://actor-name.example/talk_to_host",
    )
    monkeypatch.setattr(autonomy, "append_idle_event", fake_append)
    publish_report = AsyncMock(return_value=saved_message)
    monkeypatch.setattr(autonomy, "publish_outbound_report", publish_report)
    monkeypatch.setattr(lounge_routes, "lounge_repository_provider", repository_provider)
    monkeypatch.setattr(lounge_routes, "LoungeVisitCoordinator", FakeCoordinator)
    monkeypatch.setattr(lounge_routes, "compose_lounge_message", "compose-next")

    result = asyncio.run(autonomy._run_friend_visit("aion"))

    assert calls == [
        ("aion", selected_friend.id, "autonomy", "聊聊近况", "compose-next")
    ]
    assert events[0][0][1] == "friend_visit_interrupted"
    assert events[0][1]["target_type"] == "lounge_visit"
    assert events[0][1]["target_id"] == "visit-1"
    assert events[0][1]["metadata"] == {
        "friend_id": selected_friend.id,
        "friend_name": "远方朋友链接",
        "turn_count": 2,
        "status": "interrupted",
        "reason": "拜访过程中出现了状况",
    }
    assert publish_report.await_count == 1
    assert publish_report.await_args.args[0] == "aion"
    assert publish_report.await_args.args[1] == selected_friend.display_name
    assert publish_report.await_args.args[3] == "repository"
    for unsafe in ("https://", "Authorization", "secret", "talk_to_host"):
        assert unsafe not in events[0][0][3]
    assert result["message"] == saved_message
    assert result["visit_id"] == "visit-1"


def test_run_actor_once_dispatches_friend_visit(monkeypatch):
    monkeypatch.setattr(
        autonomy,
        "_select_action",
        AsyncMock(return_value={"action": "friend_visit", "reason": "想去问候", "message": ""}),
    )
    monkeypatch.setattr(autonomy, "append_idle_event", AsyncMock(return_value={"id": "select"}))
    run_friend_visit = AsyncMock(return_value={"cancelled": False})
    monkeypatch.setattr(autonomy, "_run_friend_visit", run_friend_visit)

    result = asyncio.run(autonomy._run_actor_once("aion", manual=True))

    run_friend_visit.assert_awaited_once_with("aion")
    assert result["action"] == "friend_visit"
