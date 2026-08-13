from pathlib import Path

import pytest


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def database(tmp_path: Path):
    from visitor_lounge.database import Database

    return Database(tmp_path / "visitor-lounge.sqlite3")
