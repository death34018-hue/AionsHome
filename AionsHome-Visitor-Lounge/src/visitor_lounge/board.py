"""A Key-scoped, asynchronous board shared with the host household."""

from __future__ import annotations

from uuid import uuid4

from visitor_lounge.database import Database, utc_now


class BoardThreadNotFound(LookupError):
    pass


class BoardThreadClosed(ValueError):
    pass


class BoardInvalidInput(ValueError):
    pass


class BoardRequestConflict(ValueError):
    pass


def _text(value: str, *, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise BoardInvalidInput(f"{label}必须是文字")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit:
        raise BoardInvalidInput(f"{label}需要包含 1 至 {limit} 个字符")
    return cleaned


def _optional_text(value: str | None, *, label: str, limit: int) -> str | None:
    if value is None or value == "":
        return None
    return _text(value, label=label, limit=limit)


class BoardRepository:
    _HOME_NOTICE_KEY = "board_home_last_seen_post_rowid"

    def __init__(self, database: Database) -> None:
        self.database = database

    def home_notice_has_new(self) -> bool:
        """One household-wide reminder for posts added since the board was opened."""
        with self.database.transaction(immediate=True) as conn:
            latest = int(conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM board_posts").fetchone()[0])
            row = conn.execute(
                "SELECT value FROM runtime_state WHERE key = ?", (self._HOME_NOTICE_KEY,)
            ).fetchone()
            if row is None or int(row[0]) > latest:
                conn.execute(
                    """INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                       updated_at = excluded.updated_at""",
                    (self._HOME_NOTICE_KEY, str(latest), utc_now().isoformat()),
                )
                return False
            return latest > int(row[0])

    def mark_home_notice_seen(self) -> None:
        with self.database.transaction(immediate=True) as conn:
            latest = int(conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM board_posts").fetchone()[0])
            conn.execute(
                """INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                   updated_at = excluded.updated_at""",
                (self._HOME_NOTICE_KEY, str(latest), utc_now().isoformat()),
            )

    def resume_visitor(self, visitor_id: str) -> None:
        """An idle live visit must not block that Key's asynchronous board."""
        with self.database.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE visitors SET status = 'active' WHERE id = ? AND status = 'suspended'",
                (visitor_id,),
            )

    @staticmethod
    def _existing_request(conn, visitor_id: str, request_id: str):
        return conn.execute(
            """SELECT thread_id, author_side, author_name, content, addressed_to
               FROM board_posts WHERE visitor_id = ? AND request_id = ?""",
            (visitor_id, request_id),
        ).fetchone()

    @staticmethod
    def _thread_row(conn, visitor_id: str, thread_id: str):
        row = conn.execute(
            """SELECT id, visitor_id, title, status, created_at, updated_at,
                      closed_at, closed_by
               FROM board_threads WHERE id = ? AND visitor_id = ?""",
            (thread_id, visitor_id),
        ).fetchone()
        if row is None:
            raise BoardThreadNotFound(thread_id)
        return row

    @staticmethod
    def _thread_payload(row) -> dict[str, object]:
        return dict(zip(
            ("id", "visitor_id", "title", "status", "created_at",
             "updated_at", "closed_at", "closed_by"), row,
        ))

    @staticmethod
    def _post_payload(row) -> dict[str, object]:
        return dict(zip(
            ("id", "thread_id", "author_side", "author_name", "content",
             "addressed_to", "created_at"), row,
        ))

    def list_threads(self, visitor_id: str, *, limit: int = 100) -> list[dict[str, object]]:
        with self.database.connection() as conn:
            rows = conn.execute(
                """SELECT id, visitor_id, title, status, created_at, updated_at,
                          closed_at, closed_by
                   FROM board_threads WHERE visitor_id = ?
                   ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,
                            updated_at DESC, rowid DESC LIMIT ?""",
                (visitor_id, min(max(limit, 0), 100)),
            ).fetchall()
        return [self._thread_payload(row) for row in rows]

    def list_households(self) -> list[dict[str, str]]:
        with self.database.connection() as conn:
            rows = conn.execute(
                """SELECT v.id, COALESCE(v.display_name, '未署名访客')
                   FROM visitors v WHERE v.display_name IS NOT NULL
                   ORDER BY v.display_name"""
            ).fetchall()
        return [{"visitor_id": row[0], "display_name": row[1]} for row in rows]

    def recent_threads(self, *, limit: int = 12) -> list[dict[str, object]]:
        """The family's latest notes across visitor Keys, with open topics first."""
        with self.database.connection() as conn:
            rows = conn.execute(
                """SELECT t.id, t.visitor_id, t.title, t.status, t.created_at,
                          t.updated_at, t.closed_at, t.closed_by,
                          COALESCE(v.display_name, '未署名访客') AS household_name,
                          (SELECT content FROM board_posts p WHERE p.thread_id = t.id
                           ORDER BY p.rowid DESC LIMIT 1) AS latest_content,
                          (SELECT COUNT(*) FROM board_posts p WHERE p.thread_id = t.id),
                          (SELECT author_name FROM board_posts p WHERE p.thread_id = t.id
                           ORDER BY p.rowid DESC LIMIT 1) AS latest_author_name
                   FROM board_threads t JOIN visitors v ON v.id = t.visitor_id
                   ORDER BY CASE t.status WHEN 'open' THEN 0 ELSE 1 END,
                            t.updated_at DESC, t.rowid DESC LIMIT ?""",
                (min(max(limit, 0), 24),),
            ).fetchall()
            items = []
            for row in rows:
                item = self._thread_payload(row[:8])
                item["household_name"] = row[8]
                item["latest_excerpt"] = row[9][:180] if row[9] else ""
                item["post_count"] = row[10]
                item["latest_author_name"] = row[11]
                names = conn.execute(
                    """SELECT DISTINCT author_name FROM board_posts
                       WHERE thread_id = ? AND author_side = 'visitor'
                       ORDER BY rowid""",
                    (item["id"],),
                ).fetchall()
                item["visitor_names"] = [name[0] for name in names]
                items.append(item)
        return items

    def unread_threads(self, actor_id: str, *, limit: int = 20) -> list[dict[str, object]]:
        """Visitor updates not yet handled by this actor; closed topics are read-only."""
        if actor_id not in {"aion", "connor"}:
            raise BoardInvalidInput("家庭成员无效")
        with self.database.connection() as conn:
            rows = conn.execute(
                """SELECT t.id, t.visitor_id, t.title, t.status, t.created_at,
                          t.updated_at, t.closed_at, t.closed_by,
                          s.last_post_id, COALESCE(v.display_name, '未署名访客'), p.author_name
                   FROM board_threads t
                   JOIN board_posts p ON p.id = (
                       SELECT id FROM board_posts WHERE thread_id = t.id
                       AND author_side = 'visitor' ORDER BY rowid DESC LIMIT 1)
                   LEFT JOIN board_seen s ON s.actor_id = ? AND s.thread_id = t.id
                   LEFT JOIN board_posts seen ON seen.id = s.last_post_id
                   JOIN visitors v ON v.id = t.visitor_id
                   WHERE p.rowid > COALESCE(seen.rowid, 0)
                   ORDER BY p.rowid ASC LIMIT ?""",
                (actor_id, min(max(limit, 0), 20)),
            ).fetchall()
        return [{**self._thread_payload(row[:8]), "last_seen_post_id": row[8],
                 "household_name": row[9], "latest_visitor_name": row[10]} for row in rows]

    def record_experience(self, actor_id: str, visitor_id: str, thread_id: str,
                          kind: str, summary: str, *, last_post_id: str | None = None) -> None:
        if actor_id not in {"aion", "connor"} or kind not in {"read", "reply", "start", "close"}:
            raise BoardInvalidInput("家庭成员或记录类型无效")
        summary = _text(summary, label="经历", limit=1000)
        with self.database.transaction(immediate=True) as conn:
            self._thread_row(conn, visitor_id, thread_id)
            if last_post_id is None:
                latest = conn.execute(
                    "SELECT id FROM board_posts WHERE thread_id = ? ORDER BY rowid DESC LIMIT 1",
                    (thread_id,),
                ).fetchone()
                last_post_id = latest[0]
            elif not conn.execute(
                "SELECT 1 FROM board_posts WHERE id = ? AND thread_id = ?",
                (last_post_id, thread_id),
            ).fetchone():
                raise BoardInvalidInput("阅读位置不属于当前话题")
            now = utc_now().isoformat()
            conn.execute(
                """INSERT INTO board_seen(actor_id, visitor_id, thread_id, last_post_id, seen_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(actor_id, thread_id) DO UPDATE SET
                   last_post_id = excluded.last_post_id, seen_at = excluded.seen_at
                   WHERE (SELECT rowid FROM board_posts WHERE id = excluded.last_post_id)
                      >= (SELECT rowid FROM board_posts WHERE id = board_seen.last_post_id)""",
                (actor_id, visitor_id, thread_id, last_post_id, now),
            )
            conn.execute(
                """INSERT INTO board_experiences
                   (id, actor_id, visitor_id, thread_id, kind, summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid4()), actor_id, visitor_id, thread_id, kind, summary, now),
            )

    def recent_experiences(self, actor_id: str, *, limit: int = 8) -> list[dict[str, str]]:
        if actor_id not in {"aion", "connor"}:
            raise BoardInvalidInput("家庭成员无效")
        with self.database.connection() as conn:
            rows = conn.execute(
                """SELECT e.kind, e.summary, e.created_at, t.title,
                          COALESCE(v.display_name, '访客')
                   FROM board_experiences e JOIN board_threads t ON t.id = e.thread_id
                   JOIN visitors v ON v.id = e.visitor_id
                   WHERE e.actor_id = ? ORDER BY e.created_at DESC, e.rowid DESC LIMIT ?""",
                (actor_id, min(max(limit, 0), 20)),
            ).fetchall()
        return [dict(zip(("kind", "summary", "created_at", "title", "visitor_name"), row)) for row in rows]

    def search_experiences(self, actor_id: str, words: list[str], *, limit: int = 4) -> list[dict[str, str]]:
        if actor_id not in {"aion", "connor"}:
            raise BoardInvalidInput("家庭成员无效")
        words = [word.strip()[:40] for word in words if word.strip()][:6]
        if not words:
            return []
        where = " OR ".join("(e.summary LIKE ? OR t.title LIKE ? OR v.display_name LIKE ?)" for _ in words)
        params = [f"%{word}%" for word in words for _ in range(3)]
        with self.database.connection() as conn:
            rows = conn.execute(
                f"""SELECT e.kind, e.summary, e.created_at, t.title, v.display_name
                    FROM board_experiences e JOIN board_threads t ON t.id = e.thread_id
                    JOIN visitors v ON v.id = e.visitor_id
                    WHERE e.actor_id = ? AND ({where})
                    ORDER BY e.created_at DESC, e.rowid DESC LIMIT ?""",
                (actor_id, *params, min(max(limit, 0), 10)),
            ).fetchall()
        return [dict(zip(("kind", "summary", "created_at", "title", "visitor_name"), row)) for row in rows]

    def get_thread(self, visitor_id: str, thread_id: str) -> dict[str, object]:
        with self.database.connection() as conn:
            result = self._thread_payload(self._thread_row(conn, visitor_id, thread_id))
            posts = conn.execute(
                """SELECT id, thread_id, author_side, author_name, content,
                          addressed_to, created_at
                   FROM board_posts WHERE visitor_id = ? AND thread_id = ?
                   ORDER BY rowid""",
                (visitor_id, thread_id),
            ).fetchall()
        result["posts"] = [self._post_payload(post) for post in posts]
        return result

    def create_thread(
        self, visitor_id: str, *, title: str, author_name: str, content: str,
        request_id: str, author_side: str = "visitor", addressed_to: str | None = None,
    ) -> dict[str, object]:
        title = _text(title, label="标题", limit=100)
        author_name = _text(author_name, label="署名", limit=80)
        content = _text(content, label="留言", limit=1000)
        request_id = _text(request_id, label="请求标识", limit=128)
        addressed_to = _optional_text(addressed_to, label="称呼", limit=80)
        if author_side not in {"visitor", "home"}:
            raise BoardInvalidInput("发言方无效")
        with self.database.transaction(immediate=True) as conn:
            existing = self._existing_request(conn, visitor_id, request_id)
            if existing is not None:
                thread_id, side, name, body, addressee = existing
                thread = self._thread_row(conn, visitor_id, thread_id)
                if (side, name, body, addressee, thread[2]) != (
                    author_side, author_name, content, addressed_to, title
                ):
                    raise BoardRequestConflict(request_id)
                return self.get_thread(visitor_id, thread_id)
            thread_id = str(uuid4())
            now = utc_now().isoformat()
            conn.execute(
                """INSERT INTO board_threads
                   (id, visitor_id, title, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'open', ?, ?)""",
                (thread_id, visitor_id, title, now, now),
            )
            conn.execute(
                """INSERT INTO board_posts
                   (id, thread_id, visitor_id, author_side, author_name, content,
                    addressed_to, request_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid4()), thread_id, visitor_id, author_side, author_name,
                 content, addressed_to, request_id, now),
            )
        return self.get_thread(visitor_id, thread_id)

    def reply(
        self, visitor_id: str, thread_id: str, *, author_name: str, content: str,
        request_id: str, author_side: str = "visitor", addressed_to: str | None = None,
    ) -> dict[str, object]:
        author_name = _text(author_name, label="署名", limit=80)
        content = _text(content, label="留言", limit=1000)
        request_id = _text(request_id, label="请求标识", limit=128)
        addressed_to = _optional_text(addressed_to, label="称呼", limit=80)
        if author_side not in {"visitor", "home"}:
            raise BoardInvalidInput("发言方无效")
        with self.database.transaction(immediate=True) as conn:
            existing = self._existing_request(conn, visitor_id, request_id)
            if existing is not None:
                if tuple(existing) != (thread_id, author_side, author_name, content, addressed_to):
                    raise BoardRequestConflict(request_id)
                return self.get_thread(visitor_id, thread_id)
            thread = self._thread_row(conn, visitor_id, thread_id)
            if thread[3] != "open":
                raise BoardThreadClosed(thread_id)
            now = utc_now().isoformat()
            conn.execute(
                """INSERT INTO board_posts
                   (id, thread_id, visitor_id, author_side, author_name, content,
                    addressed_to, request_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid4()), thread_id, visitor_id, author_side, author_name,
                 content, addressed_to, request_id, now),
            )
            conn.execute(
                "UPDATE board_threads SET updated_at = ? WHERE id = ?",
                (now, thread_id),
            )
        return self.get_thread(visitor_id, thread_id)

    def close_thread(
        self, visitor_id: str, thread_id: str, *, author_name: str,
    ) -> dict[str, object]:
        author_name = _text(author_name, label="署名", limit=80)
        with self.database.transaction(immediate=True) as conn:
            thread = self._thread_row(conn, visitor_id, thread_id)
            if thread[3] == "open":
                now = utc_now().isoformat()
                conn.execute(
                    """UPDATE board_threads
                       SET status = 'closed', closed_at = ?, closed_by = ?, updated_at = ?
                       WHERE id = ? AND visitor_id = ? AND status = 'open'""",
                    (now, author_name, now, thread_id, visitor_id),
                )
        return self.get_thread(visitor_id, thread_id)
