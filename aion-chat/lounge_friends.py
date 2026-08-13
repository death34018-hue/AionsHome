"""Private, per-actor storage for lounge friends."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit


_SCHEMA_VERSION = 1
_EDITABLE_FIELDS = {
    "display_name",
    "lounge_url",
    "visitor_key",
    "relationship_note",
    "enabled",
    "allow_autonomous",
    "cooldown_hours",
    "max_turns",
}


@dataclass(frozen=True)
class LoungeFriend:
    id: str
    actor_id: str
    display_name: str
    lounge_url: str
    visitor_key: str
    relationship_note: str
    enabled: bool
    allow_autonomous: bool
    cooldown_hours: int
    max_turns: int
    last_visit_at: float | None
    created_at: float
    updated_at: float


def mask_visitor_key(visitor_key: str) -> str:
    """Return a fixed-width-safe display value without exposing the key."""
    if len(visitor_key) <= 4:
        return "*" * len(visitor_key)
    return "*" * (len(visitor_key) - 4) + visitor_key[-4:]


def redact_visitor_key(value: str, visitor_key: str) -> str:
    """Redact an exact known key while preserving surrounding text."""
    if not visitor_key:
        return value
    return value.replace(visitor_key, "[redacted]")


class LoungeFriendStore:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock

    def list_for_actor(self, actor_id: str) -> list[LoungeFriend]:
        return [friend for friend in self._friends() if friend.actor_id == actor_id]

    def get_owned(self, actor_id: str, friend_id: str) -> LoungeFriend:
        for friend in self._friends():
            if friend.actor_id == actor_id and friend.id == friend_id:
                return friend
        raise KeyError("Friend not found")

    def create(self, **fields: object) -> LoungeFriend:
        now = float(self.clock())
        friend = self._make_friend(
            {
                **fields,
                "id": uuid.uuid4().hex,
                "last_visit_at": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        self._ensure_key_not_in_editable_text(friend)
        friends = self._friends()
        self._ensure_unique_name(friends, friend)
        self._ensure_visitor_key_binding(friends, friend)
        self._write(friends + [friend])
        return friend

    def update(self, actor_id: str, friend_id: str, **fields: object) -> LoungeFriend:
        if not set(fields).issubset(_EDITABLE_FIELDS):
            raise ValueError("Invalid friend update")
        friends = self._friends()
        current = self._find_owned(friends, actor_id, friend_id)
        replacement = self._make_friend(
            {
                **asdict(current),
                **fields,
                "updated_at": float(self.clock()),
            }
        )
        self._ensure_key_not_in_editable_text(replacement)
        self._ensure_unique_name(friends, replacement, ignore_id=current.id)
        self._ensure_visitor_key_binding(friends, replacement, ignore_id=current.id)
        self._write([replacement if friend.id == current.id else friend for friend in friends])
        return replacement

    def delete(self, actor_id: str, friend_id: str) -> bool:
        friends = self._friends()
        retained = [
            friend
            for friend in friends
            if not (friend.actor_id == actor_id and friend.id == friend_id)
        ]
        if len(retained) == len(friends):
            return False
        self._write(retained)
        return True

    def eligible_for_autonomy(self, actor_id: str) -> list[LoungeFriend]:
        now = float(self.clock())
        return [
            friend
            for friend in self.list_for_actor(actor_id)
            if friend.enabled
            and friend.allow_autonomous
            and (
                friend.last_visit_at is None
                or now >= friend.last_visit_at + friend.cooldown_hours * 60 * 60
            )
        ]

    def mark_visited(self, actor_id: str, friend_id: str, when: float) -> LoungeFriend:
        if isinstance(when, bool) or not isinstance(when, (int, float)):
            raise ValueError("Invalid visit timestamp")
        friends = self._friends()
        current = self._find_owned(friends, actor_id, friend_id)
        replacement = self._make_friend(
            {
                **asdict(current),
                "last_visit_at": float(when),
                "updated_at": float(self.clock()),
            }
        )
        self._write([replacement if friend.id == current.id else friend for friend in friends])
        return replacement

    def public_dict(self, friend: LoungeFriend) -> dict[str, object]:
        return {
            "id": friend.id,
            "actor_id": friend.actor_id,
            "display_name": redact_visitor_key(
                friend.display_name, friend.visitor_key
            ),
            "lounge_url": friend.lounge_url,
            "relationship_note": redact_visitor_key(
                friend.relationship_note, friend.visitor_key
            ),
            "enabled": friend.enabled,
            "allow_autonomous": friend.allow_autonomous,
            "cooldown_hours": friend.cooldown_hours,
            "max_turns": friend.max_turns,
            "last_visit_at": friend.last_visit_at,
            "created_at": friend.created_at,
            "updated_at": friend.updated_at,
            "visitor_key_masked": mask_visitor_key(friend.visitor_key),
        }

    def _friends(self) -> list[LoungeFriend]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            if payload.get("schema_version") != _SCHEMA_VERSION:
                raise ValueError
            records = payload.get("friends")
            if not isinstance(records, list):
                raise ValueError
            return [self._make_friend(record) for record in records]
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid lounge friend storage") from error

    def _write(self, friends: list[LoungeFriend]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "friends": [asdict(friend) for friend in friends],
        }
        try:
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temp_path.replace(self.path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _find_owned(
        self, friends: list[LoungeFriend], actor_id: str, friend_id: str
    ) -> LoungeFriend:
        for friend in friends:
            if friend.actor_id == actor_id and friend.id == friend_id:
                return friend
        raise KeyError("Friend not found")

    def _ensure_unique_name(
        self, friends: list[LoungeFriend], candidate: LoungeFriend, ignore_id: str | None = None
    ) -> None:
        name = candidate.display_name.casefold()
        if any(
            friend.actor_id == candidate.actor_id
            and friend.id != ignore_id
            and friend.display_name.casefold() == name
            for friend in friends
        ):
            raise ValueError("Duplicate display name")

    def _ensure_visitor_key_binding(
        self, friends: list[LoungeFriend], candidate: LoungeFriend, ignore_id: str | None = None
    ) -> None:
        if any(
            friend.actor_id != candidate.actor_id
            and friend.id != ignore_id
            and friend.lounge_url == candidate.lounge_url
            and friend.visitor_key == candidate.visitor_key
            for friend in friends
        ):
            raise ValueError("Visitor key already in use")

    @staticmethod
    def _ensure_key_not_in_editable_text(candidate: LoungeFriend) -> None:
        if any(
            candidate.visitor_key in value
            for value in (candidate.display_name, candidate.relationship_note)
        ):
            raise ValueError("Invalid lounge friend data")

    def _make_friend(self, fields: object) -> LoungeFriend:
        if not isinstance(fields, dict):
            raise ValueError("Invalid lounge friend record")
        required = {
            "id",
            "actor_id",
            "display_name",
            "lounge_url",
            "visitor_key",
            "relationship_note",
            "enabled",
            "allow_autonomous",
            "cooldown_hours",
            "max_turns",
            "last_visit_at",
            "created_at",
            "updated_at",
        }
        if set(fields) != required:
            raise ValueError("Invalid lounge friend record")
        return LoungeFriend(
            id=self._required_text(fields["id"], "Invalid lounge friend record"),
            actor_id=self._required_text(fields["actor_id"], "Invalid lounge friend record"),
            display_name=self._required_text(fields["display_name"], "Invalid lounge friend record"),
            lounge_url=self._normalize_lounge_url(fields["lounge_url"]),
            visitor_key=self._required_text(fields["visitor_key"], "Invalid lounge friend record"),
            relationship_note=self._required_text(fields["relationship_note"], "Invalid lounge friend record"),
            enabled=self._required_bool(fields["enabled"]),
            allow_autonomous=self._required_bool(fields["allow_autonomous"]),
            cooldown_hours=self._bounded_int(fields["cooldown_hours"], 1, 168, "Invalid cooldown"),
            max_turns=self._bounded_int(fields["max_turns"], 1, 8, "Invalid max turns"),
            last_visit_at=self._optional_timestamp(fields["last_visit_at"]),
            created_at=self._timestamp(fields["created_at"]),
            updated_at=self._timestamp(fields["updated_at"]),
        )

    @staticmethod
    def _required_text(value: object, message: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(message)
        return value.strip()

    @staticmethod
    def _required_bool(value: object) -> bool:
        if not isinstance(value, bool):
            raise ValueError("Invalid lounge friend record")
        return value

    @staticmethod
    def _bounded_int(value: object, lower: int, upper: int, message: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError(message)
        return value

    @staticmethod
    def _timestamp(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Invalid lounge friend record")
        return float(value)

    def _optional_timestamp(self, value: object) -> float | None:
        if value is None:
            return None
        return self._timestamp(value)

    @staticmethod
    def _normalize_lounge_url(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("Invalid lounge URL")
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("Invalid lounge URL")
        hostname = parsed.hostname.lower()
        netloc = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port not in (None, 443):
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit(("https", netloc, "/mcp", "", ""))
