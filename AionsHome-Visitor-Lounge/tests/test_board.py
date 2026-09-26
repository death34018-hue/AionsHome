import pytest

from visitor_lounge.board import BoardRepository, BoardThreadNotFound, BoardThreadClosed
from visitor_lounge.repository import VisitorRepository


@pytest.fixture
def board(database):
    database.initialize()
    visitors = VisitorRepository(database)
    return BoardRepository(database), visitors.create_unclaimed_visitor(), visitors.create_unclaimed_visitor()


def test_family_key_sees_its_own_wall_and_distinct_signatures(board):
    repository, jellyfish_key, other_key = board
    thread = repository.create_thread(
        jellyfish_key, title="今天的小事", author_name="水母家 A",
        content="我家的灯亮了", request_id="new-1",
    )
    repository.reply(
        jellyfish_key, thread["id"], author_name="水母家 B",
        content="还是我修好的", request_id="reply-1",
    )

    own = repository.get_thread(jellyfish_key, thread["id"])
    assert [post["author_name"] for post in own["posts"]] == ["水母家 A", "水母家 B"]
    assert repository.list_threads(other_key) == []
    with pytest.raises(BoardThreadNotFound):
        repository.get_thread(other_key, thread["id"])


def test_closed_thread_cannot_receive_more_replies(board):
    repository, jellyfish_key, _ = board
    thread = repository.create_thread(
        jellyfish_key, title="旧话题", author_name="水母家 A",
        content="今天先聊到这", request_id="new-1",
    )
    repository.close_thread(jellyfish_key, thread["id"], author_name="Aion")

    with pytest.raises(BoardThreadClosed):
        repository.reply(
            jellyfish_key, thread["id"], author_name="水母家 A",
            content="再补一句", request_id="reply-after-close",
        )
    assert len(repository.get_thread(jellyfish_key, thread["id"])["posts"]) == 1


def test_retrying_request_id_does_not_duplicate_post(board):
    repository, jellyfish_key, _ = board
    first = repository.create_thread(
        jellyfish_key, title="趣闻", author_name="水母家 A",
        content="水母灯亮啦", request_id="new-1",
    )
    again = repository.create_thread(
        jellyfish_key, title="趣闻", author_name="水母家 A",
        content="水母灯亮啦", request_id="new-1",
    )

    assert again["id"] == first["id"]
    assert len(repository.list_threads(jellyfish_key)) == 1


def test_family_wall_shows_recent_notes_from_all_keys_with_visitor_signatures(board):
    repository, first_key, second_key = board
    visitors = VisitorRepository(repository.database)
    visitors.claim_name(first_key, "水母家", "test")
    visitors.claim_name(second_key, "企鹅家", "test")
    first = repository.create_thread(first_key, title="灯亮了", author_name="水母家 A",
                                     content="今天亮起一盏灯", request_id="wall-1")
    repository.reply(first_key, first["id"], author_name="水母家 B",
                     content="我来补一句", request_id="wall-2")
    second = repository.create_thread(second_key, title="带来照片", author_name="企鹅家 A",
                                      content="送给大家一张照片", request_id="wall-3")

    latest = repository.recent_threads(limit=2)
    assert [item["id"] for item in latest] == [second["id"], first["id"]]
    assert latest[1]["household_name"] == "水母家"
    assert latest[1]["visitor_names"] == ["水母家 A", "水母家 B"]
    assert latest[1]["latest_excerpt"] == "我来补一句"
    assert latest[1]["latest_author_name"] == "水母家 B"
    assert latest[0]["latest_author_name"] == "企鹅家 A"
    assert latest[1]["post_count"] == 2
    assert len(repository.recent_threads(limit=1)) == 1


def test_family_wall_uses_last_post_author_even_after_closing(board):
    repository, visitor_id, _ = board
    VisitorRepository(repository.database).claim_name(visitor_id, "朋友家", "test")
    thread = repository.create_thread(visitor_id, title="接着聊", author_name="朋友",
                                      content="来打个招呼", request_id="latest-1")
    repository.reply(visitor_id, thread["id"], author_name="家里的伙伴",
                     author_side="home", content="收到啦", request_id="latest-2")
    repository.close_thread(visitor_id, thread["id"], author_name="主人")

    assert repository.recent_threads()[0]["latest_author_name"] == "家里的伙伴"


def test_family_wall_puts_open_notes_first_before_applying_limit(board):
    repository, first_key, second_key = board
    visitors = VisitorRepository(repository.database)
    visitors.claim_name(first_key, "朋友家一", "test")
    visitors.claim_name(second_key, "朋友家二", "test")
    notes = []
    for index, visitor_id in enumerate([first_key, second_key, first_key, second_key]):
        thread = repository.create_thread(visitor_id, title=f"话题 {index}", author_name="朋友",
                                          content="一张便签", request_id=f"order-{index}")
        if index in {0, 3}:
            repository.close_thread(visitor_id, thread["id"], author_name="主人")
        notes.append(thread["id"])
    with repository.database.transaction() as conn:
        for index, thread_id in enumerate(notes):
            conn.execute("UPDATE board_threads SET updated_at = ? WHERE id = ?",
                         (f"2026-09-{index + 1:02d}T12:00:00+00:00", thread_id))

    assert [item["id"] for item in repository.recent_threads()] == [notes[2], notes[1], notes[3], notes[0]]
    assert [item["id"] for item in repository.recent_threads(limit=2)] == [notes[2], notes[1]]
