"""Reception history reads the existing lounge without changing its data."""

import asyncio
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def reception_path(tmp_path):
    path = tmp_path / 'lounge.sqlite3'
    schema = Path(__file__).resolve().parents[1] / 'AionsHome-Visitor-Lounge/src/visitor_lounge/schema.sql'
    with sqlite3.connect(path) as db:
        db.executescript(schema.read_text('utf-8'))
        db.execute("INSERT INTO visitors (id, created_at, display_name) VALUES ('guest', '2026-08-29T00:00:00+00:00', '来访朋友')")
        db.executemany(
            'INSERT INTO visits (id, visitor_id, started_at, last_activity_at, ended_at) VALUES (?, ?, ?, ?, ?)',
            [
                ('first', 'guest', '2026-08-29T01:00:00+00:00', '2026-08-29T01:10:00+00:00', '2026-08-29T01:10:00+00:00'),
                ('second', 'guest', '2026-08-29T02:00:00+00:00', '2026-08-29T02:01:00+00:00', None),
            ],
        )
        db.executemany(
            'INSERT INTO messages (id, visitor_id, sender, content, created_at, delivery_status) VALUES (?, ?, ?, ?, ?, ?)',
            [
                ('welcome', 'guest', 'host', '初次见面', '2026-08-29T00:59:59+00:00', 'accepted'),
                ('a', 'guest', 'visitor', '你好', '2026-08-29T01:01:00+00:00', 'accepted'),
                ('b', 'guest', 'host', '欢迎\n进来坐', '2026-08-29T01:01:00+00:00', 'accepted'),
                ('c', 'guest', 'visitor', '未送达', '2026-08-29T01:02:00+00:00', 'failed'),
                ('d', 'guest', 'visitor', '又来啦', '2026-08-29T02:01:00+00:00', 'accepted'),
            ],
        )
    return path


def test_existing_receptions_are_scoped_ordered_and_keep_full_dialogue(reception_path):
    asyncio.run(check_existing_receptions(reception_path))


async def check_existing_receptions(reception_path):
    from lounge_receptions import LoungeReceptionHistory

    history = LoungeReceptionHistory(reception_path)
    visits = await history.recent('connor', limit=50)
    assert [v['id'] for v in visits] == ['reception:second', 'reception:first']
    assert visits[1]['partner_name'] == '来访朋友'
    assert visits[1]['direction'] == 'inbound'
    assert visits[1]['turn_count'] == 2
    assert visits[0]['status'] == 'running'
    detail = await history.get('connor', 'reception:first')
    assert [(m['direction'], m['content']) for m in detail['messages']] == [
        ('outbound', '初次见面'), ('inbound', '你好'), ('outbound', '欢迎\n进来坐'),
    ]
    assert await history.recent('aion') == []
    assert await history.get('aion', 'reception:first') is None
    assert await history.get('connor', 'reception:missing') is None
    with sqlite3.connect(reception_path) as db:
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 5


def test_missing_lounge_does_not_create_a_database(tmp_path):
    asyncio.run(check_missing_lounge(tmp_path))


async def check_missing_lounge(tmp_path):
    from lounge_receptions import LoungeReceptionHistory

    path = tmp_path / 'not-installed.sqlite3'
    history = LoungeReceptionHistory(path)
    assert await history.recent('connor') == []
    assert await history.get('connor', 'reception:first') is None
    assert not path.exists()
