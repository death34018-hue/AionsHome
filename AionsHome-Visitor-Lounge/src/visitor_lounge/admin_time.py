"""Presentation-only timezone conversion for the loopback admin UI."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


ADMIN_TIMESTAMP_KEYS = frozenset(
    {
        "created_at",
        "completed_at",
        "started_at",
        "finished_at",
        "updated_at",
        "expires_at",
        "revoked_at",
        "ends_at",
        "reset_at",
        "quota_reset_at",
        "last_activity_at",
        "resource_checked_at",
        "disclosure_consented_at",
        "safety_locked_until",
    }
)


def format_admin_timestamp(
    value: object,
    timezone_name: str = "Asia/Shanghai",
) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S")


def format_admin_payload_timestamps(
    value: object,
    timezone_name: str = "Asia/Shanghai",
) -> object:
    if isinstance(value, list):
        return [format_admin_payload_timestamps(item, timezone_name) for item in value]
    if isinstance(value, dict):
        rendered: dict[object, object] = {}
        for key, item in value.items():
            if key in ADMIN_TIMESTAMP_KEYS:
                rendered[key] = format_admin_timestamp(item, timezone_name)
            else:
                rendered[key] = format_admin_payload_timestamps(item, timezone_name)
        return rendered
    return value
