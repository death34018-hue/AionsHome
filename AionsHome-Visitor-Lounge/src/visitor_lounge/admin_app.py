"""Loopback-only administration boundary for Visitor Lounge."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from ipaddress import ip_address
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from visitor_lounge.admin_time import format_admin_payload_timestamps
from visitor_lounge.container import Container
from visitor_lounge.repository import (
    RuntimeStateRepository,
    VisitorNotFound,
    VisitorRepository,
)
from visitor_lounge.reception_settings import (
    InvalidReceptionSettings,
    ReceptionSettings,
    ReceptionSettingsRepository,
)
from visitor_lounge.security import InvalidVisitorName, KeyService, normalize_visitor_name
from visitor_lounge.settings import Settings


NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
RESOURCE_STATE_MAX_AGE = timedelta(seconds=30)
ACTIVITY_LABELS = {
    "safety_lock": "安全锁定",
    "visitor_unlocked": "已解锁",
    "visitor_paused": "已暂停",
    "visitor_suspended": "已挂起",
    "key_created": "Key 已创建",
    "key_revealed": "Key 已显示",
    "key_revoked": "Key 已撤销",
    "key_rotated": "Key 已轮换",
    "quota_reset": "额度已重置",
    "visitor_deleted": "访客已删除",
    "visitor_exported": "访客数据已导出",
    "visitor_invited": "邀请已创建",
    "key_copy_disclosed": "Key 已为复制而披露",
    "credential_input_rejected": "疑似凭据已拦截",
    "visitor_identity_updated": "访客身份已更新",
    "visitors_deleted": "访客已清理",
}


class ConfirmationBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    confirmation: str = Field(max_length=32)


class BulkDeleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visitor_ids: list[str] = Field(min_length=1, max_length=100)
    confirmation: str = Field(max_length=32)


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    note: str = Field(min_length=1, max_length=2000)


class InvitationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visitor_kind: Literal["human", "external_ai"]


class VisitorIdentityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    visitor_kind: Literal["human", "external_ai"]


class ReceptionSettingsBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    persona_text: str = Field(min_length=1, max_length=12000)
    first_welcome: str = Field(min_length=1, max_length=1000)
    returning_welcome: str = Field(min_length=1, max_length=1000)
    quota_exhausted: str = Field(min_length=1, max_length=1000)
    unsafe_request: str = Field(min_length=1, max_length=1000)
    credential_detected: str = Field(min_length=1, max_length=1000)
    input_too_long: str = Field(min_length=1, max_length=1000)
    lounge_closed: str = Field(min_length=1, max_length=1000)
    system_unavailable: str = Field(min_length=1, max_length=1000)
    hourly_quota_limit: int = Field(ge=1, le=500)
    lounge_enabled: bool
    idle_minutes: int


class VisitorBusyForDeletion(RuntimeError):
    """Raised when a selected visitor still has active model work."""


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _json_payload(value: object) -> dict[str, Any]:
    try:
        loaded = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_peer(host: str) -> bool:
    """Accept Starlette's explicit test peer in addition to real loopback IPs."""
    return host == "testclient" or _is_loopback_host(host)


class AdminService:
    """Small synchronous service for trusted, local management actions."""

    def __init__(self, container: Container) -> None:
        self.container = container
        self.database = container.database
        self.visitors = VisitorRepository(container.database)
        self.runtime_state = RuntimeStateRepository(container.database)
        self.keys = KeyService(self.visitors, container.settings)
        self.reception = ReceptionSettingsRepository(
            container.database, container.settings.root
        )
        self._fernet = Fernet(container.settings.master_key)

    def _now(self) -> datetime:
        return self.container.clock()

    def _require_visitor(self, visitor_id: str) -> None:
        self.visitors.visitor(visitor_id)

    def save_reception(self, body: ReceptionSettingsBody) -> ReceptionSettings:
        saved = self.reception.save(ReceptionSettings(**body.model_dump()))
        self._apply_quota_window(saved.hourly_quota_limit)
        return saved

    def restore_reception(self) -> ReceptionSettings:
        saved = self.reception.restore_defaults()
        self._apply_quota_window(saved.hourly_quota_limit)
        return saved

    def _apply_quota_window(self, limit: int) -> None:
        now = self._now()
        cutoff = now - timedelta(hours=12)
        with self.database.transaction(immediate=True) as conn:
            rows = conn.execute(
                """
                SELECT id, started_at FROM quota_windows
                WHERE rowid IN (
                    SELECT MAX(rowid) FROM quota_windows GROUP BY visitor_id
                )
                  AND started_at > ?
                """,
                (cutoff.isoformat(),),
            ).fetchall()
            for window_id, started_at_value in rows:
                started_at = _parse_timestamp(started_at_value) or now
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                conn.execute(
                    """
                    UPDATE quota_windows
                    SET limit_count = ?, ends_at = ?
                    WHERE id = ?
                    """,
                    (
                        limit,
                        (started_at + timedelta(hours=12)).isoformat(),
                        window_id,
                    ),
                )

    def _audit(
        self,
        kind: str,
        visitor_id: str | None,
        client_host: str | None,
        *,
        details: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        payload = json.dumps(
            {
                "actor": "loopback_admin",
                "client_host": client_host or "unknown",
                "details": details or {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        values = (
            str(uuid4()),
            visitor_id,
            kind,
            payload,
            _timestamp(self._now()),
        )
        statement = (
            "INSERT INTO audit_events "
            "(id, visitor_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)"
        )
        if connection is not None:
            connection.execute(statement, values)
            return
        with self.database.transaction(immediate=True) as conn:
            conn.execute(statement, values)

    def dashboard(self) -> dict[str, Any]:
        now = self._now()
        reception = self.reception.get()
        try:
            local_zone = ZoneInfo(self.container.settings.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown admin timezone") from exc
        local_now = now.astimezone(local_zone)
        local_start = datetime.combine(local_now.date(), time.min, local_zone)
        day_start = local_start.astimezone(timezone.utc).isoformat()
        day_end = (local_start + timedelta(days=1)).astimezone(timezone.utc).isoformat()
        with self.database.connection() as conn:
            conn.row_factory = sqlite3.Row
            metrics = {
                "today_visitors": int(
                    conn.execute(
                        """
                        SELECT COUNT(DISTINCT visitor_id) FROM visits
                        WHERE julianday(last_activity_at) >= julianday(?)
                          AND julianday(last_activity_at) < julianday(?)
                        """,
                        (day_start, day_end),
                    ).fetchone()[0]
                ),
                "model_calls": int(
                    conn.execute(
                        """
                        SELECT
                          (SELECT COUNT(*) FROM model_calls
                           WHERE julianday(created_at) >= julianday(?)
                             AND julianday(created_at) < julianday(?))
                          +
                          (SELECT COUNT(*) FROM summary_generation_attempts
                           WHERE julianday(started_at) >= julianday(?)
                             AND julianday(started_at) < julianday(?))
                        """,
                        (day_start, day_end, day_start, day_end),
                    ).fetchone()[0]
                ),
                "unread_summaries": int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM notification_events
                        WHERE kind = 'summary_ready' AND delivered_at IS NULL
                        """
                    ).fetchone()[0]
                ),
                "queue_depth": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM generation_jobs WHERE status = 'queued'"
                    ).fetchone()[0]
                ),
            }
            job_counts = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status IN ('queued', 'running') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'completed'
                              AND julianday(created_at) >= julianday(?)
                              AND julianday(created_at) < julianday(?) THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status IN ('failed', 'cancelled', 'interrupted')
                              AND julianday(created_at) >= julianday(?)
                              AND julianday(created_at) < julianday(?) THEN 1 ELSE 0 END)
                FROM generation_jobs
                """,
                (day_start, day_end, day_start, day_end),
            ).fetchone()
            summary_attempt_counts = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status IN ('failed', 'timed_out', 'interrupted', 'cancelled')
                              THEN 1 ELSE 0 END)
                FROM summary_generation_attempts
                WHERE julianday(started_at) >= julianday(?)
                  AND julianday(started_at) < julianday(?)
                """,
                (day_start, day_end),
            ).fetchone()
            token_totals = conn.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
                FROM (
                    SELECT input_tokens, output_tokens
                    FROM model_calls
                    WHERE usage_reported = 1
                      AND julianday(created_at) >= julianday(?)
                      AND julianday(created_at) < julianday(?)
                    UNION ALL
                    SELECT input_tokens, output_tokens
                    FROM summary_generation_attempts
                    WHERE usage_reported = 1
                      AND julianday(started_at) >= julianday(?)
                      AND julianday(started_at) < julianday(?)
                )
                """,
                (day_start, day_end, day_start, day_end),
            ).fetchone()
            metrics.update(
                {
                    "lounge_enabled": reception.lounge_enabled,
                    "active_generations": int(job_counts[0] or 0),
                    "today_completed": int(job_counts[1] or 0)
                    + int(summary_attempt_counts[0] or 0),
                    "today_failed": int(job_counts[2] or 0)
                    + int(summary_attempt_counts[1] or 0),
                    "reported_input_tokens": int(token_totals[0]),
                    "reported_output_tokens": int(token_totals[1]),
                }
            )
            visitors = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT visitors.id, visitors.display_name, visitors.status,
                           visitors.created_at, visitors.visitor_kind,
                           (SELECT masked FROM visitor_keys
                             WHERE visitor_keys.visitor_id = visitors.id
                               AND revoked_at IS NULL
                             ORDER BY rowid DESC LIMIT 1) AS active_key_masked,
                            (SELECT COUNT(*) FROM messages
                             WHERE messages.visitor_id = visitors.id) AS message_count,
                            (SELECT sender FROM messages
                             WHERE messages.visitor_id = visitors.id
                             ORDER BY messages.rowid DESC LIMIT 1) AS latest_message_sender,
                            (SELECT content FROM messages
                             WHERE messages.visitor_id = visitors.id
                             ORDER BY messages.rowid DESC LIMIT 1) AS latest_message_content,
                            MAX(visits.last_activity_at) AS last_activity_at
                    FROM visitors
                    LEFT JOIN visits ON visits.visitor_id = visitors.id
                    GROUP BY visitors.id
                    ORDER BY COALESCE(MAX(visits.last_activity_at), visitors.created_at) DESC
                    """
                ).fetchall()
            ]
            for visitor in visitors:
                visitor["identity_claimed"] = visitor["display_name"] is not None
                visitor["message_count"] = int(visitor["message_count"] or 0)
                quota = conn.execute(
                    """
                    SELECT limit_count, used_count, reserved_count, ends_at
                    FROM quota_windows WHERE visitor_id = ?
                    ORDER BY started_at DESC, rowid DESC LIMIT 1
                    """,
                    (visitor["id"],),
                ).fetchone()
                quota_expired = bool(
                    quota is not None
                    and (_parse_timestamp(quota[3]) or now) <= now
                )
                visitor["quota_remaining"] = (
                    None
                    if quota is None
                    else (
                        reception.hourly_quota_limit
                        if quota_expired
                        else max(0, int(quota[0]) - int(quota[1]) - int(quota[2]))
                    )
                )
                visitor["quota_limit"] = (
                    None
                    if quota is None
                    else reception.hourly_quota_limit
                    if quota_expired
                    else int(quota[0])
                )
                visitor["quota_reset_at"] = (
                    None if quota is None or quota_expired else str(quota[3])
                )
                visitor["quota_message"] = (
                    "下一条消息开始新 12 小时窗口" if quota_expired else None
                )
            summaries = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT summaries.visitor_id, visitors.display_name,
                           summaries.text, summaries.created_at
                    FROM summaries
                    JOIN visitors ON visitors.id = summaries.visitor_id
                    ORDER BY summaries.created_at DESC, summaries.rowid DESC
                    LIMIT 8
                    """
                ).fetchall()
            ]
            model_activity = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT kind, visitor_id, display_name, status, failure_reason,
                           usage_reported, input_tokens, output_tokens, started_at,
                           sort_row
                    FROM (
                        SELECT 'chat' AS kind, model_calls.visitor_id,
                               visitors.display_name, generation_jobs.status,
                               NULL AS failure_reason, model_calls.usage_reported,
                               model_calls.input_tokens, model_calls.output_tokens,
                               model_calls.created_at AS started_at,
                               model_calls.rowid AS sort_row
                        FROM model_calls
                        JOIN generation_jobs ON generation_jobs.id = model_calls.job_id
                        LEFT JOIN visitors ON visitors.id = model_calls.visitor_id
                        UNION ALL
                        SELECT 'memory' AS kind, summary_generation_attempts.visitor_id,
                               visitors.display_name, summary_generation_attempts.status,
                               summary_generation_attempts.failure_reason,
                               summary_generation_attempts.usage_reported,
                               summary_generation_attempts.input_tokens,
                               summary_generation_attempts.output_tokens,
                               summary_generation_attempts.started_at,
                               summary_generation_attempts.rowid AS sort_row
                        FROM summary_generation_attempts
                        LEFT JOIN visitors
                          ON visitors.id = summary_generation_attempts.visitor_id
                    )
                    ORDER BY started_at DESC, sort_row DESC
                    LIMIT 30
                    """
                ).fetchall()
            ]
            failed_statuses = {"failed", "timed_out", "interrupted", "cancelled"}
            running_statuses = {"queued", "running"}
            for activity in model_activity:
                status = str(activity["status"])
                activity["kind_label"] = (
                    "聊天回复" if activity["kind"] == "chat" else "滚动记忆"
                )
                activity["visitor_name"] = (
                    str(activity["display_name"])
                    if activity["display_name"] is not None
                    else str(activity["visitor_id"])[:8]
                    if activity["visitor_id"]
                    else "已删除访客"
                )
                activity["state_class"] = (
                    "is-failed"
                    if status in failed_statuses
                    else "is-running"
                    if status in running_statuses
                    else "is-completed"
                    if status == "completed"
                    else ""
                )
                activity.pop("sort_row", None)
            recent_activity = []
            placeholders = ",".join("?" for _ in ACTIVITY_LABELS)
            for row in conn.execute(
                f"""
                SELECT audit_events.kind, audit_events.payload,
                       audit_events.created_at, visitors.display_name
                FROM audit_events
                LEFT JOIN visitors ON visitors.id = audit_events.visitor_id
                WHERE audit_events.kind IN ({placeholders})
                ORDER BY audit_events.created_at DESC, audit_events.rowid DESC
                LIMIT 12
                """,
                tuple(ACTIVITY_LABELS),
            ).fetchall():
                payload = _json_payload(row[1])
                details = payload.get("details", {})
                fallback_id = details.get("visitor_id") if isinstance(details, dict) else None
                recent_activity.append(
                    {
                        "label": ACTIVITY_LABELS[str(row[0])],
                        "visitor_name": (
                            str(row[3])
                            if row[3] is not None
                            else (str(fallback_id)[:8] if fallback_id else "已删除访客")
                        ),
                        "created_at": str(row[2]),
                    }
                )
        resource = self.runtime_state.resource_gate()
        if resource is None or self._now() - resource.checked_at > RESOURCE_STATE_MAX_AGE:
            metrics["resource_status"] = "stale"
            metrics["resource_checked_at"] = None if resource is None else resource.checked_at.isoformat()
        else:
            metrics["resource_status"] = "available" if resource.can_start else "paused"
            metrics["resource_checked_at"] = resource.checked_at.isoformat()
        payload = {
            "metrics": metrics,
            "visitors": visitors,
            "summaries": summaries,
            "model_activity": model_activity,
            "recent_activity": recent_activity,
            "timezone_name": self.container.settings.timezone_name,
        }
        rendered = format_admin_payload_timestamps(
            payload, self.container.settings.timezone_name
        )
        assert isinstance(rendered, dict)
        return rendered

    def visitor_detail(
        self, visitor_id: str, *, message_page: int = 1, message_page_size: int = 100
    ) -> dict[str, Any]:
        configured_quota_limit = self.reception.get().hourly_quota_limit
        with self.database.connection() as conn:
            conn.row_factory = sqlite3.Row
            visitor_row = conn.execute(
                """
                SELECT id, display_name, status, created_at, disclosure_version,
                       disclosure_consented_at, visitor_kind, safety_locked_until
                FROM visitors WHERE id = ?
                """,
                (visitor_id,),
            ).fetchone()
            if visitor_row is None:
                raise VisitorNotFound(visitor_id)
            visitor = dict(visitor_row)
            message_total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE visitor_id = ?",
                    (visitor_id,),
                ).fetchone()[0]
            )
            message_page_size = min(100, max(1, int(message_page_size)))
            message_pages = max(
                1, (message_total + message_page_size - 1) // message_page_size
            )
            message_page = min(message_pages, max(1, int(message_page)))
            messages = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, sender, content, created_at, source, delivery_status
                    FROM messages WHERE visitor_id = ?
                    ORDER BY rowid DESC LIMIT ? OFFSET ?
                    """,
                    (
                        visitor_id,
                        message_page_size,
                        (message_page - 1) * message_page_size,
                    ),
                ).fetchall()
            ]
            messages.reverse()
            summaries = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT summaries.id, summaries.text, summaries.created_at,
                           summaries.first_message_id, summaries.last_message_id
                    FROM summaries
                    WHERE summaries.visitor_id = ?
                    ORDER BY summaries.created_at, summaries.rowid
                    """,
                    (visitor_id,),
                ).fetchall()
            ]
            calls = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT model_calls.id, model_calls.job_id,
                           model_calls.usage_reported,
                           model_calls.input_tokens, model_calls.output_tokens,
                           model_calls.created_at, model_calls.completed_at,
                           generation_jobs.status, generation_jobs.action,
                           generation_jobs.request_id
                    FROM model_calls
                    JOIN generation_jobs ON generation_jobs.id = model_calls.job_id
                    WHERE model_calls.visitor_id = ?
                    ORDER BY model_calls.created_at, model_calls.rowid
                    """,
                    (visitor_id,),
                ).fetchall()
            ]
            summary_attempts = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, summary_job_id, status, usage_reported,
                           input_tokens, output_tokens, failure_reason,
                           started_at, finished_at
                    FROM summary_generation_attempts
                    WHERE visitor_id = ?
                    ORDER BY started_at, rowid
                    """,
                    (visitor_id,),
                ).fetchall()
            ]
            quota_windows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, limit_count, used_count, reserved_count,
                           started_at, ends_at
                    FROM quota_windows WHERE visitor_id = ?
                    ORDER BY started_at DESC, rowid DESC
                    """,
                    (visitor_id,),
                ).fetchall()
            ]
            summary_ids = {str(summary["id"]) for summary in summaries}
            summary_notification_ids = []
            for row in conn.execute(
                """
                SELECT id, payload FROM notification_events
                WHERE visitor_id = ? AND kind = 'summary_ready'
                  AND delivered_at IS NULL
                ORDER BY created_at, rowid
                """,
                (visitor_id,),
            ).fetchall():
                if str(_json_payload(row[1]).get("summary_id")) in summary_ids:
                    summary_notification_ids.append(str(row[0]))
            keys = [
                {
                    **dict(row),
                    "status": "revoked" if row[3] is not None else "active",
                }
                for row in conn.execute(
                    """
                    SELECT id, masked, created_at, revoked_at
                    FROM visitor_keys WHERE visitor_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    """,
                    (visitor_id,),
                ).fetchall()
            ]
            sessions = [
                {
                    **dict(row),
                    "status": (
                        "revoked"
                        if row[4] is not None
                        else (
                            "expired"
                            if (_parse_timestamp(row[3]) or self._now()) <= self._now()
                            else "active"
                        )
                    ),
                }
                for row in conn.execute(
                    """
                    SELECT id, device_id, created_at, expires_at, revoked_at
                    FROM auth_sessions WHERE visitor_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    """,
                    (visitor_id,),
                ).fetchall()
            ]
            audit_events = []
            for row in conn.execute(
                """
                SELECT id, kind, payload, created_at FROM audit_events
                WHERE visitor_id = ? ORDER BY created_at DESC, rowid DESC
                """,
                (visitor_id,),
            ).fetchall():
                event = dict(row)
                event["payload"] = _json_payload(event["payload"])
                audit_events.append(event)

        input_price = self.container.settings.input_token_price_per_million
        output_price = self.container.settings.output_token_price_per_million
        prices_configured = input_price is not None and output_price is not None
        for call in calls:
            started = _parse_timestamp(call["created_at"])
            completed = _parse_timestamp(call["completed_at"])
            call["latency_seconds"] = (
                None
                if started is None or completed is None
                else max(0.0, (completed - started).total_seconds())
            )
            call["estimated_cost"] = (
                (
                    int(call["input_tokens"]) * input_price
                    + int(call["output_tokens"]) * output_price
                )
                / 1_000_000
                if prices_configured and bool(call["usage_reported"])
                else None
            )
        for attempt in summary_attempts:
            started = _parse_timestamp(attempt["started_at"])
            finished = _parse_timestamp(attempt["finished_at"])
            attempt["latency_seconds"] = (
                None
                if started is None or finished is None
                else max(0.0, (finished - started).total_seconds())
            )
            attempt["estimated_cost"] = (
                (
                    int(attempt["input_tokens"]) * input_price
                    + int(attempt["output_tokens"]) * output_price
                )
                / 1_000_000
                if prices_configured and bool(attempt["usage_reported"])
                else None
            )
        reported_attempts = [
            attempt for attempt in summary_attempts if bool(attempt["usage_reported"])
        ]
        reported_calls = [call for call in calls if bool(call["usage_reported"])]
        total_input_tokens = sum(
            int(call["input_tokens"]) for call in reported_calls
        ) + sum(int(attempt["input_tokens"]) for attempt in reported_attempts)
        total_output_tokens = sum(
            int(call["output_tokens"]) for call in reported_calls
        ) + sum(int(attempt["output_tokens"]) for attempt in reported_attempts)
        total_cost = (
            (total_input_tokens * input_price + total_output_tokens * output_price)
            / 1_000_000
            if prices_configured
            else None
        )
        latest_quota = quota_windows[0] if quota_windows else None
        latest_expired = bool(
            latest_quota is not None
            and (_parse_timestamp(latest_quota["ends_at"]) or self._now()) <= self._now()
        )
        quota_current = None
        if latest_quota is not None:
            limit_count = (
                configured_quota_limit
                if latest_expired
                else int(latest_quota["limit_count"])
            )
            used_count = 0 if latest_expired else int(latest_quota["used_count"])
            reserved_count = (
                0 if latest_expired else int(latest_quota["reserved_count"])
            )
            quota_current = {
                "limit_count": limit_count,
                "used_count": used_count,
                "reserved_count": reserved_count,
                "remaining": max(0, limit_count - used_count - reserved_count),
                "reset_at": None if latest_expired else str(latest_quota["ends_at"]),
                "message": (
                    "下一条消息开始新 12 小时窗口" if latest_expired else None
                ),
            }
        payload = {
            "visitor": visitor,
            "messages": messages,
            "message_page": message_page,
            "message_pages": message_pages,
            "message_total": message_total,
            "summaries": summaries,
            "calls": calls,
            "summary_attempts": summary_attempts,
            "quota_windows": quota_windows,
            "quota_current": quota_current,
            "keys": keys,
            "sessions": sessions,
            "audit_events": audit_events,
            "summary_notification_ids": summary_notification_ids,
            "prices_configured": prices_configured,
            "usage_totals": {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "estimated_cost": total_cost,
            },
            "timezone_name": self.container.settings.timezone_name,
        }
        rendered = format_admin_payload_timestamps(
            payload, self.container.settings.timezone_name
        )
        assert isinstance(rendered, dict)
        return rendered

    def mark_summary_notifications_delivered(
        self, visitor_id: str, notification_ids: list[str]
    ) -> int:
        if not notification_ids:
            return 0
        placeholders = ",".join("?" for _ in notification_ids)
        with self.database.transaction(immediate=True) as conn:
            updated = conn.execute(
                f"""
                UPDATE notification_events SET delivered_at = ?
                WHERE visitor_id = ? AND kind = 'summary_ready'
                  AND delivered_at IS NULL
                  AND id IN ({placeholders})
                """,
                (_timestamp(self._now()), visitor_id, *notification_ids),
            )
        return updated.rowcount

    def create_key(self, visitor_id: str, client_host: str | None) -> str:
        self._require_visitor(visitor_id)
        with self.database.transaction(immediate=True) as conn:
            plain = self.keys.create(visitor_id, connection=conn)
            self._audit(
                "key_created",
                visitor_id,
                client_host,
                details={"masked": plain.masked},
                connection=conn,
            )
        return plain.value

    def create_invitation(
        self,
        visitor_kind: Literal["human", "external_ai"],
        client_host: str | None,
    ) -> tuple[str, str]:
        with self.database.transaction(immediate=True) as conn:
            visitor_id = self.visitors.create_unclaimed_visitor(
                visitor_kind, connection=conn
            )
            plain = self.keys.create(visitor_id, connection=conn)
            self._audit(
                "visitor_invited",
                visitor_id,
                client_host,
                details={"masked": plain.masked, "visitor_kind": visitor_kind},
                connection=conn,
            )
        return visitor_id, plain.value

    def update_identity(
        self,
        visitor_id: str,
        name: str,
        visitor_kind: Literal["human", "external_ai"],
        client_host: str | None,
    ) -> None:
        normalized = normalize_visitor_name(name, set())
        previous = self.visitors.visitor(visitor_id)
        with self.database.transaction(immediate=True) as conn:
            self.visitors.update_identity(
                visitor_id,
                normalized,
                visitor_kind,
                connection=conn,
            )
            self._audit(
                "visitor_identity_updated",
                visitor_id,
                client_host,
                details={
                    "old_name": previous.display_name,
                    "new_name": normalized,
                    "old_kind": previous.visitor_kind,
                    "new_kind": visitor_kind,
                },
                connection=conn,
            )

    def disclose_key_for_copy(
        self, visitor_id: str, client_host: str | None
    ) -> str:
        self._require_visitor(visitor_id)
        with self.database.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT encrypted_value, masked FROM visitor_keys
                WHERE visitor_id = ? AND revoked_at IS NULL
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (visitor_id,),
            ).fetchone()
            if row is None:
                raise KeyError("active key not found")
            try:
                value = self._fernet.decrypt(bytes(row[0])).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError) as exc:
                raise RuntimeError("stored key cannot be decrypted") from exc
            self._audit(
                "key_copy_disclosed",
                visitor_id,
                client_host,
                details={"masked": str(row[1]), "purpose": "copy"},
                connection=conn,
            )
        return value

    def reveal_key(self, visitor_id: str, client_host: str | None) -> str:
        self._require_visitor(visitor_id)
        with self.database.connection() as conn:
            row = conn.execute(
                """
                SELECT encrypted_value, masked FROM visitor_keys
                WHERE visitor_id = ? AND revoked_at IS NULL
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (visitor_id,),
            ).fetchone()
        if row is None:
            raise KeyError("active key not found")
        try:
            value = self._fernet.decrypt(bytes(row[0])).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise RuntimeError("stored key cannot be decrypted") from exc
        self._audit("key_revealed", visitor_id, client_host, details={"masked": str(row[1])})
        return value

    def rotate_key(self, visitor_id: str, client_host: str | None) -> str:
        self._require_visitor(visitor_id)
        with self.database.transaction(immediate=True) as conn:
            plain = self.keys.rotate(visitor_id, connection=conn)
            self._audit(
                "key_rotated",
                visitor_id,
                client_host,
                details={"masked": plain.masked},
                connection=conn,
            )
        return plain.value

    def revoke_keys(self, visitor_id: str, client_host: str | None) -> None:
        self._require_visitor(visitor_id)
        with self.database.transaction(immediate=True) as conn:
            count = conn.execute(
                """
                UPDATE visitor_keys SET revoked_at = ?
                WHERE visitor_id = ? AND revoked_at IS NULL
                """,
                (_timestamp(self._now()), visitor_id),
            ).rowcount
            self._audit(
                "key_revoked",
                visitor_id,
                client_host,
                details={"count": count},
                connection=conn,
            )

    def set_status(self, visitor_id: str, status: str, client_host: str | None) -> None:
        kind = {
            "paused": "visitor_paused",
            "active": "visitor_unlocked",
            "suspended": "visitor_suspended",
        }[status]
        with self.database.transaction(immediate=True) as conn:
            self.visitors.set_status(visitor_id, status, connection=conn)
            self._audit(
                kind,
                visitor_id,
                client_host,
                details={"status": status},
                connection=conn,
            )

    def reset_quota(self, visitor_id: str, client_host: str | None) -> None:
        self._require_visitor(visitor_id)
        with self.database.transaction(immediate=True) as conn:
            window = conn.execute(
                """
                SELECT id FROM quota_windows WHERE visitor_id = ?
                ORDER BY started_at DESC, rowid DESC LIMIT 1
                """,
                (visitor_id,),
            ).fetchone()
            window_id = None if window is None else str(window[0])
            if window_id is not None:
                conn.execute(
                    "UPDATE quota_windows SET used_count = 0 WHERE id = ?",
                    (window_id,),
                )
            self._audit(
                "quota_reset",
                visitor_id,
                client_host,
                details={"window_id": window_id},
                connection=conn,
            )

    def add_note(self, visitor_id: str, note: str, client_host: str | None) -> None:
        self._require_visitor(visitor_id)
        cleaned = note.strip()
        if not cleaned:
            raise ValueError("note cannot be empty")
        self._audit("note_added", visitor_id, client_host, details={"note": cleaned})

    def export(self, visitor_id: str, client_host: str | None) -> dict[str, Any]:
        detail = self.visitor_detail(visitor_id)
        detail.pop("summary_notification_ids", None)
        self._audit("visitor_exported", visitor_id, client_host)
        return detail

    def delete_visitors(
        self, visitor_ids: list[str], client_host: str | None
    ) -> int:
        visitor_ids = list(dict.fromkeys(str(value) for value in visitor_ids))
        if not visitor_ids or len(visitor_ids) > 100 or any(not value for value in visitor_ids):
            raise ValueError("需要选择 1 至 100 位访客")
        placeholders = ",".join("?" for _ in visitor_ids)
        with self.database.transaction(immediate=True) as conn:
            existing = {
                str(row[0])
                for row in conn.execute(
                    f"SELECT id FROM visitors WHERE id IN ({placeholders})",
                    tuple(visitor_ids),
                ).fetchall()
            }
            if existing != set(visitor_ids):
                raise VisitorNotFound(next(iter(set(visitor_ids) - existing), "unknown"))
            active_generation = conn.execute(
                f"""
                SELECT 1 FROM generation_jobs
                WHERE visitor_id IN ({placeholders}) AND status IN ('queued', 'running')
                LIMIT 1
                """,
                tuple(visitor_ids),
            ).fetchone()
            active_summary = conn.execute(
                f"""
                SELECT 1 FROM summary_jobs
                WHERE visitor_id IN ({placeholders}) AND status IN ('queued', 'running')
                LIMIT 1
                """,
                tuple(visitor_ids),
            ).fetchone()
            if active_generation is not None or active_summary is not None:
                raise VisitorBusyForDeletion
            conn.execute(
                f"DELETE FROM audit_events WHERE visitor_id IN ({placeholders})",
                tuple(visitor_ids),
            )
            deleted = conn.execute(
                f"DELETE FROM visitors WHERE id IN ({placeholders})",
                tuple(visitor_ids),
            ).rowcount
            self._audit(
                "visitors_deleted",
                None,
                client_host,
                details={"count": deleted},
                connection=conn,
            )
        return deleted

    def delete(self, visitor_id: str, client_host: str | None) -> None:
        self.delete_visitors([visitor_id], client_host)


def create_admin_app(container: Container) -> FastAPI:
    """Build a separate app that can only be configured on a loopback address."""
    if not _is_loopback_host(container.settings.admin_host):
        raise ValueError("admin app must bind to a loopback host")
    container.database.initialize()
    project_root = Path(__file__).resolve().parents[2]
    templates = Jinja2Templates(directory=project_root / "templates")
    service = AdminService(container)
    app = FastAPI(
        title="Visitor Lounge Admin",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.admin_service = service
    app.mount(
        "/admin/static",
        StaticFiles(directory=project_root / "static"),
        name="admin-static",
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok", "service": "admin"}

    @app.middleware("http")
    async def enforce_local_admin_boundary(request: Request, call_next):
        peer = request.client.host if request.client is not None else ""
        if not _is_loopback_peer(peer):
            return JSONResponse({"detail": "仅允许本机访问"}, status_code=403)

        requested_host = (request.url.hostname or "").casefold()
        allowed_hosts = {
            "localhost",
            "127.0.0.1",
            "::1",
            container.settings.admin_host.casefold(),
        }
        if peer == "testclient":
            allowed_hosts.add("testserver")
        if requested_host not in allowed_hosts:
            return JSONResponse({"detail": "管理主机名无效"}, status_code=403)

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                request_host = request.headers.get("host", "").casefold()
                if (
                    parsed.scheme.casefold() != request.url.scheme.casefold()
                    or parsed.netloc.casefold() != request_host
                ):
                    return JSONResponse({"detail": "拒绝跨站管理操作"}, status_code=403)

        response = await call_next(request)
        if request.url.path.startswith("/admin"):
            response.headers.update(NO_STORE_HEADERS)
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError):
        return JSONResponse({"detail": "请求无效"}, status_code=422)

    @app.exception_handler(VisitorNotFound)
    async def visitor_not_found(_: Request, __: VisitorNotFound):
        return JSONResponse({"detail": "访客不存在"}, status_code=404)

    def client_host(request: Request) -> str | None:
        return request.client.host if request.client is not None else None

    @app.get("/admin")
    async def dashboard(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="admin_dashboard.html",
            context={
                **service.dashboard(),
                "host_name": container.settings.host_display_name,
            },
        )

    @app.get("/admin/settings")
    async def reception_settings(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="admin_settings.html",
            context={
                "settings": service.reception.get(),
                "host_name": container.settings.host_display_name,
            },
        )

    @app.put("/admin/api/settings")
    async def save_reception_settings(body: ReceptionSettingsBody):
        try:
            saved = service.save_reception(body)
        except InvalidReceptionSettings as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return saved.__dict__

    @app.post("/admin/api/settings/restore-defaults")
    async def restore_reception_settings():
        return service.restore_reception().__dict__

    @app.get("/admin/visitors/{visitor_id}")
    async def visitor_detail(visitor_id: str, request: Request, message_page: int = 1):
        detail = service.visitor_detail(visitor_id, message_page=message_page)
        notification_ids = detail.pop("summary_notification_ids")
        response = templates.TemplateResponse(
            request=request,
            name="admin_visitor.html",
            context={
                **detail,
                "host_name": container.settings.host_display_name,
            },
        )
        service.mark_summary_notifications_delivered(visitor_id, notification_ids)
        return response

    def key_response(value: str, *, purpose: str | None = None) -> JSONResponse:
        payload: dict[str, Any] = {"key": value, "hide_after_seconds": 30}
        if purpose is not None:
            payload["purpose"] = purpose
        return JSONResponse(
            payload, headers=NO_STORE_HEADERS
        )

    @app.post("/admin/api/invitations", status_code=201)
    async def create_invitation(body: InvitationBody, request: Request):
        visitor_id, key = service.create_invitation(
            body.visitor_kind, client_host(request)
        )
        return JSONResponse(
            {
                "visitor_id": visitor_id,
                "key": key,
                "visitor_kind": body.visitor_kind,
                "hide_after_seconds": 30,
            },
            status_code=201,
            headers=NO_STORE_HEADERS,
        )

    @app.put("/admin/api/visitors/{visitor_id}/identity")
    async def update_identity(
        visitor_id: str, body: VisitorIdentityBody, request: Request
    ):
        try:
            service.update_identity(
                visitor_id,
                body.name,
                body.visitor_kind,
                client_host(request),
            )
        except InvalidVisitorName:
            raise HTTPException(
                status_code=422,
                detail="名字需要包含 1 至 200 个有效字符",
            ) from None
        return {"ok": True}

    @app.post("/admin/api/visitors/{visitor_id}/key")
    async def create_key(visitor_id: str, request: Request):
        return key_response(service.create_key(visitor_id, client_host(request)))

    @app.post("/admin/api/visitors/{visitor_id}/key/reveal")
    async def reveal_key(visitor_id: str, request: Request):
        try:
            value = service.reveal_key(visitor_id, client_host(request))
        except KeyError:
            raise HTTPException(status_code=404, detail="没有可用 Key") from None
        except RuntimeError:
            raise HTTPException(status_code=500, detail="Key 无法读取") from None
        return key_response(value)

    @app.post("/admin/api/visitors/{visitor_id}/key/rotate")
    async def rotate_key(visitor_id: str, request: Request):
        return key_response(service.rotate_key(visitor_id, client_host(request)))

    @app.post("/admin/api/visitors/{visitor_id}/key/revoke", status_code=204)
    async def revoke_key(visitor_id: str, request: Request):
        service.revoke_keys(visitor_id, client_host(request))
        return Response(status_code=204)

    @app.post("/admin/api/visitors/{visitor_id}/key/copy-disclosure")
    async def copy_disclosure(visitor_id: str, request: Request):
        try:
            value = service.disclose_key_for_copy(visitor_id, client_host(request))
        except KeyError:
            raise HTTPException(status_code=404, detail="没有可用 Key") from None
        except RuntimeError:
            raise HTTPException(status_code=500, detail="Key 无法读取") from None
        return key_response(value, purpose="copy")

    @app.post("/admin/api/visitors/{visitor_id}/pause", status_code=204)
    async def pause(visitor_id: str, request: Request):
        service.set_status(visitor_id, "paused", client_host(request))
        return Response(status_code=204)

    @app.post("/admin/api/visitors/{visitor_id}/unlock", status_code=204)
    async def unlock(visitor_id: str, request: Request):
        service.set_status(visitor_id, "active", client_host(request))
        return Response(status_code=204)

    @app.post("/admin/api/visitors/{visitor_id}/suspend", status_code=204)
    async def suspend(visitor_id: str, request: Request):
        service.set_status(visitor_id, "suspended", client_host(request))
        return Response(status_code=204)

    @app.post("/admin/api/visitors/{visitor_id}/quota/reset", status_code=204)
    async def reset_quota(visitor_id: str, request: Request):
        service.reset_quota(visitor_id, client_host(request))
        return Response(status_code=204)

    @app.post("/admin/api/visitors/{visitor_id}/notes", status_code=201)
    async def add_note(visitor_id: str, body: NoteBody, request: Request):
        try:
            service.add_note(visitor_id, body.note, client_host(request))
        except ValueError:
            raise HTTPException(status_code=422, detail="备注不能为空") from None
        return {"ok": True}

    @app.post("/admin/api/visitors/{visitor_id}/export")
    async def export_visitor(
        visitor_id: str, body: ConfirmationBody, request: Request
    ):
        if body.confirmation != "EXPORT":
            raise HTTPException(status_code=409, detail="需要输入 EXPORT 确认")
        return JSONResponse(
            service.export(visitor_id, client_host(request)),
            headers=NO_STORE_HEADERS,
        )

    @app.post("/admin/api/visitor-cleanup")
    async def delete_visitors(body: BulkDeleteBody, request: Request):
        if body.confirmation != "DELETE":
            raise HTTPException(status_code=409, detail="需要输入 DELETE 确认")
        try:
            deleted = service.delete_visitors(body.visitor_ids, client_host(request))
        except VisitorBusyForDeletion:
            raise HTTPException(
                status_code=409,
                detail="所选访客仍有排队或生成中的任务，请等待结束后再删除",
            ) from None
        except (VisitorNotFound, ValueError):
            raise HTTPException(
                status_code=404, detail="所选访客已不存在，请刷新页面"
            ) from None
        return {"ok": True, "deleted": deleted}

    @app.delete("/admin/api/visitors/{visitor_id}", status_code=204)
    async def delete_visitor(
        visitor_id: str, body: ConfirmationBody, request: Request
    ):
        if body.confirmation != "DELETE":
            raise HTTPException(status_code=409, detail="需要输入 DELETE 确认")
        try:
            service.delete(visitor_id, client_host(request))
        except VisitorBusyForDeletion:
            raise HTTPException(
                status_code=409,
                detail="该访客仍有排队或生成中的任务，请等待结束后再删除",
            ) from None
        return Response(status_code=204)

    return app


def build_admin_app() -> FastAPI:
    """Uvicorn factory that deliberately assembles no generation workers."""
    root = Path(__file__).resolve().parents[2]
    return create_admin_app(Container.build_admin(Settings.load(root)))
