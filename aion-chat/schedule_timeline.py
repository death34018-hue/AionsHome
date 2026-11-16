"""Unified calendar view and an additional default-routine context."""

from datetime import datetime, timedelta

import routine_schedule as routines
from calendar_store import active_records, sharing_enabled, table_exists

NEXT_WINDOW = timedelta(hours=2)


def _origin_names():
    from schedule import get_schedule_origin_name
    return {role: get_schedule_origin_name(role) for role in ("user", "aion", "connor")}


async def _timeline(db, day, records, names):
    weekly, overrides = [], {}
    if await table_exists(db, "routine_weekly"):
        weekly = await routines._weekly(db, day.weekday())
        overrides = await routines._overrides(db, day)
    def routine_row(row):
        return {**row, "kind": "routine", "origin": "user", "origin_name": names["user"],
                "date": day.isoformat(), "event_key": f"routine:{day.isoformat()}:{row['id']}",
                "event_time_known": True, "reminders": [], "past_reminders": [], "reminder_state": "none", "schedule_records": []}
    items = [routine_row(row) for row in routines._merge(weekly, overrides)]
    by_key = {row["event_key"]: row for row in items}
    for record in records:
        if record["status"] == "cancelled" and record["type"] == "reminder":
            continue
        event_at = record.get("event_at") or record["trigger_at"]
        try:
            event_time = datetime.fromisoformat(event_at.replace("T", " "))
        except ValueError:
            continue
        key = record.get("event_key") or "schedule:" + record["id"]
        if record["status"] == "cancelled" and not record.get("event_at") and not key.startswith("routine:"):
            continue
        if key.startswith("routine:"):
            parts = key.split(":", 2)
            record_date = parts[1]
        else:
            record_date = event_time.date().isoformat()
        if record_date != day.isoformat():
            continue
        record = {**record, "origin_name": names.get(record.get("origin"), names["aion"])}
        row = by_key.get(key)
        if key.startswith("routine:") and row is None and record["status"] != "active":
            continue
        if row is None:
            row = {"id": "schedule:" + record["id"], "schedule_id": record["id"],
                   "kind": "schedule", "date": day.isoformat(), "start": event_time.strftime("%H:%M"), "end": "",
                   "content": record["content"], "origin": record.get("origin", "aion"),
                   "origin_name": record["origin_name"], "event_key": key,
                   "event_time_known": bool(record.get("event_at")), "overridden": False,
                   "reminders": [], "past_reminders": [], "reminder_state": "none", "schedule_records": []}
            by_key[key] = row
            items.append(row)
        elif row["kind"] == "schedule" and record.get("event_at"):
            row["start"] = event_time.strftime("%H:%M")
            row["event_time_known"] = True
        row["schedule_records"].append(record)
        if record["type"] in ("alarm", "monitor"):
            row["reminders" if record["status"] == "active" else "past_reminders"].append(record)
    for row in items:
        row["reminder_state"] = "pending" if row["reminders"] else row["past_reminders"][-1]["status"] if row["past_reminders"] else "none"
    return {"date": day.isoformat(), "weekday": day.weekday(), "items": sorted(items, key=lambda row: (row["start"], row["id"])),
            "weekly_items": [routine_row(row) for row in sorted(weekly, key=lambda row: (row["start"], row["id"]))],
            "has_overrides": bool(overrides)}


async def get_timeline(day, *, db_factory=None):
    async with (db_factory or routines.get_db)() as db:
        result = await _timeline(db, day, await active_records(db, include_ended=True), _origin_names())
        result["share_enabled"] = await sharing_enabled(db)
        return result


def _line(row, *, include_date=True):
    title = row["content"].replace("\n", " ")[:180]
    timing = row["start"] + ("—" + row["end"] if row.get("end") else "")
    if include_date:
        timing = row["date"] + " " + timing
    creator = "默认安排" if row["kind"] == "routine" and not row.get("overridden") else row["origin_name"] + "设置"
    return f"{timing} {title}｜{creator}"


async def build_routine_prompt(now=None, *, db_factory=None):
    now = now or datetime.now()
    async with (db_factory or routines.get_db)() as db:
        if not await sharing_enabled(db):
            return ""
        names = _origin_names()
        yesterday = await _timeline(db, now.date() - timedelta(days=1), [], names)
        today = await _timeline(db, now.date(), [], names)
        tomorrow = await _timeline(db, now.date() + timedelta(days=1), [], names)
        candidates = []
        all_items = yesterday["items"] + today["items"] + tomorrow["items"]
        for row in all_items:
            start = datetime.fromisoformat(row["date"] + " " + row["start"])
            if row.get("end"):
                end = datetime.fromisoformat(row["date"] + " " + row["end"])
                if end <= start:
                    end += timedelta(days=1)
            else:
                later = [item["start"] for item in all_items if item["date"] == row["date"] and item["kind"] == "routine" and item["start"] > row["start"]]
                end = datetime.fromisoformat(row["date"] + " " + min(later)) if later else datetime.combine(start.date() + timedelta(days=1), datetime.min.time())
            candidates.append((start, end, row))
        current = sorted([item for item in candidates if item[0] <= now < item[1]],
                         key=lambda item: (item[0], bool(item[2].get("overridden")), item[2]["kind"] == "schedule"), reverse=True)
        following = sorted([item for item in candidates if now < item[0] <= now + NEXT_WINDOW], key=lambda item: item[0])
        lines = ["以下是计划，实际聊天信息优先。"]
        current_today = not current or current[0][2]["date"] == now.date().isoformat()
        lines.append(("今日：" if current_today else "当前：") + (_line(current[0][2], include_date=not current_today) if current else "暂无安排"))
        if following:
            lines.append("下一项：" + _line(following[0][2], include_date=following[0][2]["date"] != now.date().isoformat()))
        return "\n".join(lines)
