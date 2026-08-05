"""Fail-open reads for messages already summarized by verified Homecoming data."""

from __future__ import annotations


async def filter_uncovered(
    db, owner_id: str, messages: list[dict]
) -> tuple[list[dict], set[str]]:
    if owner_id not in {"main", "second"} or not messages:
        return list(messages), set()
    message_ids = [str(item.get("id") or "") for item in messages]
    if any(not item for item in message_ids):
        return list(messages), set()
    try:
        covered: set[str] = set()
        for offset in range(0, len(message_ids), 400):
            batch = message_ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            cursor = await db.execute(
                "SELECT message_id FROM homecoming_summary_coverage "
                f"WHERE owner_id=? AND message_id IN ({placeholders})",
                (owner_id, *batch),
            )
            covered.update(str(row[0]) for row in await cursor.fetchall())
        return (
            [item for item in messages if str(item.get("id") or "") not in covered],
            covered,
        )
    except Exception:
        return list(messages), set()
