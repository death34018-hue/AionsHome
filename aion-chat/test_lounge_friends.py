import json

import pytest

from lounge_friends import LoungeFriendStore, mask_visitor_key


def create_friend(store, **overrides):
    fields = {
        "actor_id": "aion",
        "display_name": "远方朋友",
        "lounge_url": "https://friend.example/mcp",
        "visitor_key": "secret-visitor-key-value",
        "relationship_note": "第一次认识",
        "enabled": True,
        "allow_autonomous": True,
        "cooldown_hours": 12,
        "max_turns": 4,
    }
    fields.update(overrides)
    return store.create(**fields)


def test_friend_store_keeps_secrets_out_of_public_payload(tmp_path):
    store = LoungeFriendStore(tmp_path / "lounge_friends.json", clock=lambda: 1_786_200_000.0)
    friend = store.create(
        actor_id="aion",
        display_name="远方朋友",
        lounge_url="https://friend.example/mcp",
        visitor_key="secret-visitor-key-value",
        relationship_note="第一次认识",
        enabled=True,
        allow_autonomous=True,
        cooldown_hours=12,
        max_turns=4,
    )
    public = store.public_dict(friend)
    assert public["visitor_key_masked"].endswith("alue")
    assert "secret-visitor-key-value" not in repr(public)
    assert store.eligible_for_autonomy("connor") == []


def test_friend_store_normalizes_https_url_and_persists_schema_v1(tmp_path):
    path = tmp_path / "lounge_friends.json"
    store = LoungeFriendStore(path, clock=lambda: 100.0)

    friend = create_friend(store, lounge_url="https://friend.example/anything?ignored=yes")

    assert friend.lounge_url == "https://friend.example/mcp"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "friends": [
            {
                "id": friend.id,
                "actor_id": "aion",
                "display_name": "远方朋友",
                "lounge_url": "https://friend.example/mcp",
                "visitor_key": "secret-visitor-key-value",
                "relationship_note": "第一次认识",
                "enabled": True,
                "allow_autonomous": True,
                "cooldown_hours": 12,
                "max_turns": 4,
                "last_visit_at": None,
                "created_at": 100.0,
                "updated_at": 100.0,
            }
        ],
    }
    assert LoungeFriendStore(path).get_owned("aion", friend.id) == friend


@pytest.mark.parametrize(
    ("changes", "expected_message"),
    [
        ({"lounge_url": "http://friend.example/mcp"}, "Invalid lounge URL"),
        ({"lounge_url": "https:///mcp"}, "Invalid lounge URL"),
        ({"cooldown_hours": 0}, "Invalid cooldown"),
        ({"cooldown_hours": 169}, "Invalid cooldown"),
        ({"max_turns": 0}, "Invalid max turns"),
        ({"max_turns": 9}, "Invalid max turns"),
    ],
)
def test_friend_store_rejects_invalid_visit_settings_without_leaking_key(tmp_path, changes, expected_message):
    store = LoungeFriendStore(tmp_path / "lounge_friends.json")
    fields = {"visitor_key": "private-key-must-not-leak", **changes}

    with pytest.raises(ValueError, match=expected_message) as error:
        create_friend(store, **fields)

    assert "private-key-must-not-leak" not in str(error.value)


def test_friend_store_keeps_names_and_records_scoped_to_their_actor(tmp_path):
    store = LoungeFriendStore(tmp_path / "lounge_friends.json", clock=lambda: 100.0)
    first = create_friend(store, actor_id="aion", display_name="Shared Friend")
    second = create_friend(
        store,
        actor_id="connor",
        display_name="shared friend",
        visitor_key="different-visitor-key",
    )

    with pytest.raises(ValueError, match="Duplicate display name"):
        create_friend(store, actor_id="aion", display_name="SHARED FRIEND")
    with pytest.raises(KeyError, match="Friend not found"):
        store.get_owned("aion", second.id)

    assert store.list_for_actor("aion") == [first]
    assert store.list_for_actor("connor") == [second]
    assert store.delete("aion", second.id) is False
    assert store.get_owned("connor", second.id) == second


def test_friend_store_rejects_reusing_a_visitor_key_for_another_actor_at_same_endpoint(tmp_path):
    store = LoungeFriendStore(tmp_path / "lounge_friends.json")
    create_friend(store, lounge_url="https://friend.example/first-path")

    with pytest.raises(ValueError, match="Visitor key already in use") as error:
        create_friend(
            store,
            actor_id="connor",
            display_name="Another Friend",
            lounge_url="https://friend.example/second-path",
        )

    assert "secret-visitor-key-value" not in str(error.value)


@pytest.mark.parametrize(
    ("first_url", "second_url"),
    [
        ("https://FRIEND.EXAMPLE/first", "https://friend.example/second"),
        ("https://friend.example:443/first", "https://friend.example/second"),
    ],
)
def test_friend_store_rejects_cross_actor_key_reuse_at_equivalent_canonical_endpoint(
    tmp_path, first_url, second_url
):
    store = LoungeFriendStore(tmp_path / "lounge_friends.json")
    create_friend(store, lounge_url=first_url)

    with pytest.raises(ValueError, match="Visitor key already in use"):
        create_friend(
            store,
            actor_id="connor",
            display_name="Another Friend",
            lounge_url=second_url,
        )


def test_friend_store_preserves_non_default_https_port_in_canonical_endpoint(tmp_path):
    store = LoungeFriendStore(tmp_path / "lounge_friends.json")

    friend = create_friend(store, lounge_url="https://FRIEND.EXAMPLE:8443/anything")

    assert friend.lounge_url == "https://friend.example:8443/mcp"


def test_friend_store_rejects_updating_a_visitor_key_to_another_actor_binding(tmp_path):
    store = LoungeFriendStore(tmp_path / "lounge_friends.json")
    create_friend(store)
    other = create_friend(
        store,
        actor_id="connor",
        display_name="Another Friend",
        visitor_key="different-visitor-key",
    )

    with pytest.raises(ValueError, match="Visitor key already in use") as error:
        store.update("connor", other.id, visitor_key="secret-visitor-key-value")

    assert "secret-visitor-key-value" not in str(error.value)


@pytest.mark.parametrize("field", ["display_name", "relationship_note"])
def test_friend_store_rejects_key_embedded_in_user_editable_text(tmp_path, field):
    store = LoungeFriendStore(tmp_path / "lounge_friends.json")
    key = "private-key-must-not-leak"

    with pytest.raises(ValueError, match="^Invalid lounge friend data$") as error:
        create_friend(store, visitor_key=key, **{field: f"ordinary {key} text"})

    assert key not in str(error.value)


def test_friend_store_rejects_update_that_embeds_retained_key(tmp_path):
    store = LoungeFriendStore(tmp_path / "lounge_friends.json")
    friend = create_friend(store)

    with pytest.raises(ValueError, match="^Invalid lounge friend data$"):
        store.update(
            "aion",
            friend.id,
            relationship_note=f"ordinary {friend.visitor_key} text",
        )


def test_public_friend_text_redacts_key_from_legacy_stored_fields(tmp_path):
    path = tmp_path / "lounge_friends.json"
    store = LoungeFriendStore(path)
    friend = create_friend(store)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["friends"][0]["display_name"] = f"ordinary name {friend.visitor_key}"
    payload["friends"][0]["relationship_note"] = (
        f"ordinary note {friend.visitor_key}"
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    public = store.public_dict(store.get_owned("aion", friend.id))

    assert friend.visitor_key not in repr(public)
    assert "ordinary name" in public["display_name"]
    assert "ordinary note" in public["relationship_note"]


def test_friend_store_rejects_non_object_json_with_generic_storage_error(tmp_path):
    path = tmp_path / "lounge_friends.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="^Invalid lounge friend storage$") as error:
        LoungeFriendStore(path).list_for_actor("aion")

    assert str(error.value) == "Invalid lounge friend storage"


def test_friend_store_updates_cooldown_eligibility_and_visited_timestamp(tmp_path):
    now = [1_000.0]
    store = LoungeFriendStore(tmp_path / "lounge_friends.json", clock=lambda: now[0])
    friend = create_friend(store, cooldown_hours=1)

    assert store.eligible_for_autonomy("aion") == [friend]
    visited = store.mark_visited("aion", friend.id, when=1_000.0)
    assert visited.last_visit_at == 1_000.0
    assert store.eligible_for_autonomy("aion") == []

    now[0] = 4_600.0
    assert store.eligible_for_autonomy("aion") == [visited]
    updated = store.update("aion", friend.id, allow_autonomous=False)
    assert updated.allow_autonomous is False
    assert updated.updated_at == 4_600.0
    assert store.eligible_for_autonomy("aion") == []


def test_mask_visitor_key_keeps_only_the_last_four_characters():
    assert mask_visitor_key("secret-visitor-key-value") == "********************alue"
    assert mask_visitor_key("abc") == "***"
