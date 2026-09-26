"""Owner-only access code for board routes on the otherwise open home app."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import secrets


COOKIE = "family_board_owner"
CODE_PATH = Path(__file__).resolve().parents[2] / "data/board_owner_code.txt"


def owner_code() -> str:
    CODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CODE_PATH.exists():
        try:
            with CODE_PATH.open("x", encoding="ascii") as handle:
                handle.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
    return CODE_PATH.read_text("ascii").strip()


def owner_cookie() -> str:
    return hmac.new(owner_code().encode("ascii"), b"family-board-owner-v1",
                    hashlib.sha256).hexdigest()


def valid_owner_cookie(value: str | None) -> bool:
    return bool(value) and hmac.compare_digest(value, owner_cookie())


def valid_owner_code(value: str) -> bool:
    return hmac.compare_digest(value.strip(), owner_code())
