"""Owner-only visit scrapbook. Paper copies stay frozen at the time of the visit."""
from __future__ import annotations

import json
from uuid import uuid4

from visitor_lounge.board import BoardRepository
from visitor_lounge.database import Database, utc_now


def paper_copy(paper: dict) -> dict:
    # Keep the conversation, never arbitrary remote metadata or connection credentials.
    fields = ("id", "title", "status", "created_at", "updated_at", "closed_at", "closed_by")
    post_fields = ("id", "author_side", "author_name", "content", "addressed_to", "created_at")
    result = {key: paper[key] for key in fields if key in paper}
    result["posts"] = [{key: post[key] for key in post_fields if key in post}
                       for post in paper.get("posts", []) if isinstance(post, dict)]
    return result


class VisitJournal:
    def __init__(self, database: Database):
        self.database = database

    def begin(self, actor_id: str, source: str, trigger: str, friend_name: str = "") -> dict:
        visit = {"id": str(uuid4()), "actor_id": actor_id, "source": source,
                 "trigger": trigger, "friend_name": friend_name, "status": "running",
                 "started_at": utc_now().isoformat(), "finished_at": None,
                 "legacy": False, "topics": []}
        self.save(visit)
        return visit

    def save(self, visit: dict) -> None:
        with self.database.transaction(immediate=True) as conn:
            conn.execute("""INSERT INTO board_visits(id,actor_id,source,started_at,payload_json)
                            VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json""",
                         (visit["id"], visit["actor_id"], visit["source"], visit["started_at"],
                          json.dumps(visit, ensure_ascii=False)))

    def finish(self, visit: dict, status: str) -> None:
        if status == "waiting_user":
            for topic in visit["topics"]:
                if topic["status"] == "reading":
                    topic["status"] = "waiting_user"
        if status in {"failed", "interrupted"}:
            for topic in visit["topics"]:
                if topic["status"] == "sending":
                    topic["status"] = "unconfirmed"
                elif topic["status"] == "reading":
                    topic["status"] = "incomplete"
        visit.update(status=status, finished_at=utc_now().isoformat())
        self.save(visit)

    def recover(self) -> None:
        with self.database.connection() as conn:
            rows = conn.execute("SELECT payload_json FROM board_visits WHERE json_extract(payload_json,'$.status')='running'").fetchall()
        for row in rows:
            self.finish(json.loads(row[0]), "interrupted")

    def _old_entries(self) -> list[dict]:
        """Group old experiences by paper/minute, without inventing past visit boundaries."""
        with self.database.connection() as conn:
            rows = conn.execute("""
                SELECT e.id,e.actor_id,'home' AS source,e.visitor_id AS wall_id,e.thread_id,
                       e.kind,e.summary,e.created_at,t.title,COALESCE(v.display_name,'朋友') AS friend_name
                FROM board_experiences e JOIN board_threads t ON t.id=e.thread_id
                JOIN visitors v ON v.id=e.visitor_id
                WHERE e.created_at < COALESCE((SELECT MIN(started_at) FROM board_visits),'9999')
                UNION ALL
                SELECT id,actor_id,'outbound',friend_id,'',kind,summary,created_at,'',friend_name
                FROM board_outbound_experiences
                WHERE created_at < COALESCE((SELECT MIN(started_at) FROM board_visits),'9999')
                ORDER BY created_at
            """).fetchall()
        grouped = {}
        for row in rows:
            ident, actor, source, wall, thread, kind, summary, created, title, name = row
            key = (actor, source, wall, thread, created[:16])
            if key not in grouped:
                grouped[key] = {"id": "legacy-" + ident, "actor_id": actor, "source": source,
                                "started_at": created, "finished_at": created,
                                "status": "read", "legacy": True, "trigger": "legacy",
                                "friend_name": name, "topics": [], "old_notes": [],
                                "wall_id": wall, "thread_id": thread}
            entry = grouped[key]
            entry["old_notes"].append({"kind": kind, "summary": summary})
            if kind != "read":
                entry["status"] = {"reply": "replied", "close": "closed", "start": "started"}.get(kind, "read")
            entry["finished_at"] = created
            entry["topics"] = [{"title": title or "朋友家的留言", "status": entry["status"]}]
        return list(grouped.values())

    @staticmethod
    def _cover(visit: dict) -> dict:
        return {**{key: visit.get(key) for key in ("id", "actor_id", "source", "trigger", "status",
                                                    "started_at", "finished_at", "friend_name", "legacy")},
                "topics": [{key: topic.get(key) for key in ("title", "friend_name", "status", "reason")}
                           for topic in visit["topics"]]}

    def list(self, *, actor_id: str | None = None, source: str | None = None,
             limit: int = 20, offset: int = 0) -> dict:
        conditions, params = [], []
        for key, value in (("actor_id", actor_id), ("source", source)):
            if value:
                conditions.append(key + "=?")
                params.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.database.connection() as conn:
            rows = conn.execute("SELECT payload_json FROM board_visits" + where, params).fetchall()
        visits = [json.loads(row[0]) for row in rows]
        visits += [entry for entry in self._old_entries()
                   if (not actor_id or entry["actor_id"] == actor_id)
                   and (not source or entry["source"] == source)]
        visits.sort(key=lambda item: (item["started_at"], item["id"]), reverse=True)
        return {"items": [self._cover(item) for item in visits[offset:offset + limit]], "total": len(visits)}

    def get(self, visit_id: str) -> dict | None:
        if not visit_id.startswith("legacy-"):
            with self.database.connection() as conn:
                row = conn.execute("SELECT payload_json FROM board_visits WHERE id=?", (visit_id,)).fetchone()
            return json.loads(row[0]) if row else None
        visit = next((item for item in self._old_entries() if item["id"] == visit_id), None)
        if visit is None:
            return None
        if visit["source"] == "home":
            paper = BoardRepository(self.database).get_thread(visit["wall_id"], visit["thread_id"])
            visit["topics"][0].update(snapshot=paper_copy(paper), snapshot_kind="current",
                                      visitor_id=visit["wall_id"], thread_id=visit["thread_id"])
        else:
            with self.database.connection() as conn:
                rows = conn.execute("SELECT thread_json FROM board_outbound_threads WHERE friend_id=? AND thread_json IS NOT NULL",
                                    (visit["wall_id"],)).fetchall()
            visit["topics"] = [{"title": paper.get("title", "留存便签"), "snapshot": paper_copy(paper),
                                "snapshot_kind": "cached", "status": "archive"}
                               for row in rows for paper in [json.loads(row[0])]]
        return visit
