"""Local credential and bounded context formatting for Visitor Lounge."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from config import DATA_DIR


TOKEN_PATH = DATA_DIR / "lounge-context-bridge.key"


def get_bridge_token(path: Path = TOKEN_PATH) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = path.read_text("utf-8").strip()
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_text("utf-8").strip()
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token)
    return token


async def build_host_context(actor_id: str, query_text: str, recent_messages: list[dict]) -> list[dict]:
    from lounge_actor_context import build_lounge_actor_context

    return await build_lounge_actor_context(
        actor_id, query_text, recent_messages, limit=20
    )
