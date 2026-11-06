from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_live_chat_presence(monkeypatch):
    """Lounge tests never inspect the user's real chat database."""
    async def latest_message():
        return 0.0
    monkeypatch.setattr("visitor_lounge.board_patrol.latest_user_message_ts", latest_message, raising=False)


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def database(tmp_path: Path):
    from visitor_lounge.database import Database

    return Database(tmp_path / "visitor-lounge.sqlite3")
