"""Server-side app-supervision cache and model-visible ability text.

This module never contacts the phone. Chat paths only take a synchronous copy
of the latest asynchronously reported snapshot.
"""

from __future__ import annotations

import copy
import re
import threading
import time
import uuid

from capabilities import is_capability_enabled


APP_COMMAND_PATTERN = re.compile(
    r"\[(?:APP_(?:LOCK|TEMP_UNLOCK|UNLOCK)\s*:\s*[^\]]*"
    r"|DEVICE_(?:LOCK|TEMP_UNLOCK|UNLOCK)(?:\s*:\s*[^\]]*)?)\]",
    re.IGNORECASE,
)
APP_DIRECTIVE_PATTERN = re.compile(
    r"\[APP_(LOCK|TEMP_UNLOCK|UNLOCK)\s*:\s*([^\]]*)\]",
    re.IGNORECASE,
)
DEVICE_DIRECTIVE_PATTERN = re.compile(
    r"\[DEVICE_(LOCK|TEMP_UNLOCK|UNLOCK)(?:\s*:\s*([^\]]*))?\]",
    re.IGNORECASE,
)


class AppSupervisionStateCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot: dict = {"groups": []}
        self._received_at = 0.0
        self._seen_event_ids: set[str] = set()
        self._event_order: list[str] = []

    def replace_snapshot(self, snapshot: dict, *, received_at: float | None = None) -> None:
        safe_snapshot = copy.deepcopy(snapshot if isinstance(snapshot, dict) else {})
        groups = safe_snapshot.get("groups")
        safe_snapshot["groups"] = groups if isinstance(groups, list) else []
        with self._lock:
            self._snapshot = safe_snapshot
            self._received_at = float(time.time() if received_at is None else received_at)

    def clear(self) -> None:
        with self._lock:
            self._snapshot = {"groups": []}
            self._received_at = 0.0
            self._seen_event_ids.clear()
            self._event_order.clear()

    def read(self) -> tuple[dict, float]:
        with self._lock:
            return copy.deepcopy(self._snapshot), self._received_at

    def accept_event_once(self, event_id: str) -> bool:
        normalized = str(event_id or "").strip()
        if not normalized:
            return False
        with self._lock:
            if normalized in self._seen_event_ids:
                return False
            self._seen_event_ids.add(normalized)
            self._event_order.append(normalized)
            if len(self._event_order) > 512:
                expired = self._event_order.pop(0)
                self._seen_event_ids.discard(expired)
            return True


state_cache = AppSupervisionStateCache()


async def ensure_app_supervision_tables(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_supervision_commands (
            command_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            group_id TEXT NOT NULL,
            minutes INTEGER,
            message TEXT NOT NULL DEFAULT '',
            source_message_id TEXT NOT NULL UNIQUE,
            role_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_ref TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            ack_success INTEGER,
            ack_reason TEXT NOT NULL DEFAULT '',
            acked_at REAL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_supervision_commands_pending "
        "ON app_supervision_commands(status, expires_at)"
    )


def _command_row(row) -> dict:
    return {
        "commandId": row[0],
        "action": row[1],
        "groupId": row[2],
        "minutes": row[3],
        "message": row[4],
        "sourceMessageId": row[5],
        "roleId": row[6],
        "sourceKind": row[7],
        "sourceRef": row[8],
        "createdAt": row[9],
        "expiresAt": row[10],
    }


async def get_app_supervision_command(db, command_id: str) -> dict | None:
    await ensure_app_supervision_tables(db)
    cursor = await db.execute(
        """SELECT command_id, action, group_id, minutes, message,
                  source_message_id, role_id, source_kind, source_ref,
                  created_at, expires_at
           FROM app_supervision_commands WHERE command_id=?""",
        (command_id,),
    )
    row = await cursor.fetchone()
    return _command_row(row) if row else None


async def enqueue_app_supervision_command(
    db,
    command: dict,
    *,
    source_message_id: str,
    role_id: str,
    source_kind: str,
    source_ref: str = "",
    now: float | None = None,
) -> dict:
    await ensure_app_supervision_tables(db)
    created_at = float(time.time() if now is None else now)
    command_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT OR IGNORE INTO app_supervision_commands
        (command_id, action, group_id, minutes, message, source_message_id,
         role_id, source_kind, source_ref, created_at, expires_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            command_id,
            command["action"],
            command["groupId"],
            command.get("minutes"),
            command.get("message", ""),
            source_message_id,
            role_id,
            source_kind,
            source_ref,
            created_at,
            created_at + 300.0,
        ),
    )
    cursor = await db.execute(
        """SELECT command_id, action, group_id, minutes, message,
                  source_message_id, role_id, source_kind, source_ref,
                  created_at, expires_at
           FROM app_supervision_commands WHERE source_message_id=?""",
        (source_message_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("failed to persist app supervision command")
    return _command_row(row)


async def list_pending_app_supervision_commands(db, *, now: float | None = None) -> list[dict]:
    await ensure_app_supervision_tables(db)
    current_time = float(time.time() if now is None else now)
    await expire_pending_app_supervision_commands(db, now=current_time)
    cursor = await db.execute(
        """SELECT command_id, action, group_id, minutes, message,
                  source_message_id, role_id, source_kind, source_ref,
                  created_at, expires_at
           FROM app_supervision_commands
           WHERE status='pending' AND expires_at >= ?
           ORDER BY created_at""",
        (current_time,),
    )
    return [_command_row(row) for row in await cursor.fetchall()]


async def expire_pending_app_supervision_commands(
    db, *, now: float | None = None
) -> list[dict]:
    await ensure_app_supervision_tables(db)
    current_time = float(time.time() if now is None else now)
    cursor = await db.execute(
        """SELECT command_id, action, group_id, minutes, message,
                  source_message_id, role_id, source_kind, source_ref,
                  created_at, expires_at
           FROM app_supervision_commands
           WHERE status='pending' AND expires_at < ?
           ORDER BY created_at""",
        (current_time,),
    )
    expired = [_command_row(row) for row in await cursor.fetchall()]
    if expired:
        await db.execute(
            "UPDATE app_supervision_commands SET status='expired' "
            "WHERE status='pending' AND expires_at < ?",
            (current_time,),
        )
    return expired


async def acknowledge_app_supervision_command(
    db,
    command_id: str,
    *,
    success: bool,
    reason: str,
    now: float | None = None,
) -> bool:
    await ensure_app_supervision_tables(db)
    current_time = float(time.time() if now is None else now)
    cursor = await db.execute(
        """UPDATE app_supervision_commands
           SET status='acked', ack_success=?, ack_reason=?, acked_at=?
           WHERE command_id=? AND status='pending' AND expires_at >= ?""",
        (1 if success else 0, str(reason or ""), current_time, command_id, current_time),
    )
    if cursor.rowcount:
        return True
    await db.execute(
        "UPDATE app_supervision_commands SET status='expired' "
        "WHERE command_id=? AND status='pending' AND expires_at < ?",
        (command_id, current_time),
    )
    return False


async def queue_app_supervision_reply_command(
    text: str,
    *,
    source_message_id: str,
    role_id: str,
    source_kind: str,
    source_ref: str = "",
) -> tuple[str, dict | None]:
    snapshot, _ = state_cache.read()
    group_ids = {
        str(group.get("groupId") or "").strip()
        for group in snapshot.get("groups", [])
        if isinstance(group, dict) and str(group.get("groupId") or "").strip()
    }
    cleaned, command = parse_app_supervision_command(
        text,
        group_ids,
        enabled=is_capability_enabled("app_supervision"),
    )
    if command is None:
        return cleaned, None
    from database import get_db
    async with get_db() as db:
        queued = await enqueue_app_supervision_command(
            db,
            command,
            source_message_id=source_message_id,
            role_id=role_id,
            source_kind=source_kind,
            source_ref=source_ref,
        )
        await db.commit()
    return cleaned, queued


async def broadcast_app_supervision_command(command: dict | None) -> None:
    if not command:
        return
    from ws import manager
    await manager.broadcast({"type": "app_supervision_command", "data": command})


def format_app_supervision_result_message(
    command: dict,
    *,
    success: bool,
    reason: str,
    role_names: dict[str, str],
    group_names: dict[str, str],
) -> str:
    role_id = str(command.get("roleId") or "")
    group_id = str(command.get("groupId") or "")
    role_name = str(role_names.get(role_id) or "AI")
    group_name = str(group_names.get(group_id) or group_id or "应用")
    action = command.get("action")
    verbs = {
        "lock": "锁定",
        "temp_unlock": "暂时解锁",
        "unlock": "解锁",
        "device_lock": "锁定",
        "device_temp_unlock": "暂时解锁",
        "device_unlock": "解锁",
    }
    verb = verbs.get(action, "处理")
    if str(action).startswith("device_"):
        group_name = "手机"
    if success:
        duration = (
            f" {int(command['minutes'])} 分钟"
            if action in {
                "lock",
                "temp_unlock",
                "device_lock",
                "device_temp_unlock",
            }
            else ""
        )
        return f"【{role_name}】{verb}了{group_name}{duration}"
    clean_reason = str(reason or "手机拒绝执行").strip()
    return f"【{role_name}】未能{verb}{group_name}：{clean_reason}"


def parse_app_supervision_command(
    text: str,
    valid_group_ids: set[str] | list[str] | tuple[str, ...],
    *,
    enabled: bool,
) -> tuple[str, dict | None]:
    """Strip every supervision tag and return at most the first valid command."""
    source = str(text or "")
    valid_ids = {str(value).strip() for value in valid_group_ids if str(value).strip()}
    selected: dict | None = None
    if enabled:
        for match in APP_COMMAND_PATTERN.finditer(source):
            raw_directive = match.group(0)
            app_match = APP_DIRECTIVE_PATTERN.fullmatch(raw_directive)
            if app_match:
                action = app_match.group(1).upper()
                fields = [part.strip() for part in app_match.group(2).split("|")]
                if action == "UNLOCK":
                    if len(fields) == 1 and fields[0] in valid_ids:
                        selected = {"action": "unlock", "groupId": fields[0]}
                        break
                    continue
                if len(fields) != 3 or fields[0] not in valid_ids:
                    continue
                try:
                    minutes = int(fields[1])
                except (TypeError, ValueError):
                    continue
                if str(minutes) != fields[1] or not 1 <= minutes <= 120:
                    continue
                selected = {
                    "action": "lock" if action == "LOCK" else "temp_unlock",
                    "groupId": fields[0],
                    "minutes": minutes,
                    "message": fields[2],
                }
                break

            device_match = DEVICE_DIRECTIVE_PATTERN.fullmatch(raw_directive)
            if not device_match:
                continue
            action = device_match.group(1).upper()
            payload = device_match.group(2)
            if action == "UNLOCK":
                if payload is None:
                    selected = {"action": "device_unlock", "groupId": ""}
                    break
                continue
            if payload is None:
                continue
            fields = [part.strip() for part in payload.split("|")]
            if len(fields) != 2:
                continue
            try:
                minutes = int(fields[0])
            except (TypeError, ValueError):
                continue
            if str(minutes) != fields[0] or not 1 <= minutes <= 120:
                continue
            selected = {
                "action": (
                    "device_lock"
                    if action == "LOCK"
                    else "device_temp_unlock"
                ),
                "groupId": "",
                "minutes": minutes,
                "message": fields[1],
            }
            break
    cleaned = APP_COMMAND_PATTERN.sub("", source).strip()
    return cleaned, selected


def _minutes(milliseconds: float) -> str:
    return f"{max(0.0, milliseconds) / 60_000:.1f} 分钟"


def build_app_supervision_ability_text(
    cache: AppSupervisionStateCache | None = None,
    *,
    now: float | None = None,
) -> str:
    if not is_capability_enabled("app_supervision"):
        return ""

    selected_cache = cache or state_cache
    snapshot, received_at = selected_cache.read()
    current_time = float(time.time() if now is None else now)
    age_seconds = max(0, int(current_time - received_at)) if received_at else 0
    lines = [
        "[APP_LOCK:groupId|分钟|锁屏提示] — 锁定应用组。",
        "[APP_TEMP_UNLOCK:groupId|分钟|解锁说明] — 暂时解锁应用组。",
        "[APP_UNLOCK:groupId] — 解除应用组锁定。",
        "[DEVICE_LOCK:分钟|锁屏提示] — 锁定整台手机的普通应用。",
        "[DEVICE_TEMP_UNLOCK:分钟|解锁说明] — 暂时解除整机锁定。",
        "[DEVICE_UNLOCK] — 解除整机锁定。",
        "每条回复最多使用一条监管指令；分钟必须是 1–120 的整数。",
        "你可以操作任意应用组。只使用下方稳定 groupId，不要使用包名或猜测缩写。",
        "当用户绕开单个应用锁，连续切换多个娱乐应用时，可以直接使用整机锁定。"
        "整机锁定仍允许本应用、来电、通话和紧急呼叫。",
    ]

    groups = [item for item in snapshot.get("groups", []) if isinstance(item, dict)]
    device = snapshot.get("deviceLock")
    device = device if isinstance(device, dict) else None
    if not groups and not device:
        lines.append("当前还没有手机上报的应用监管状态。")
        return "\n".join(lines)

    lines.append(f"当前缓存状态（快照于 {age_seconds} 秒前收到）：")
    if device:
        state = str(device.get("effectiveState") or "NORMAL")
        directive = (
            device.get("temporaryUnlock")
            if state == "TEMPORARILY_UNLOCKED"
            else device.get("lock")
        )
        directive = directive if isinstance(directive, dict) else {}
        role_id = str(directive.get("roleId") or "未指定")
        message = str(directive.get("message") or "").strip()
        deadline_wall_ms = float(directive.get("deadlineWallMs") or 0)
        remaining_ms = max(0.0, deadline_wall_ms - current_time * 1_000)
        details = f"，负责角色 {role_id}"
        if deadline_wall_ms:
            details += f"，剩余 {_minutes(remaining_ms)}"
        if message:
            details += f"，提示 {message}"
        lines.append(f"- 整机状态 {state}{details}")
    elapsed_since_snapshot_ms = max(0.0, current_time - received_at) * 1_000 if received_at else 0.0
    for group in groups:
        group_id = str(group.get("groupId") or "").strip()
        display_name = str(group.get("displayName") or group_id or "未命名应用组").strip()
        if not group_id:
            continue
        usage_ms = float(group.get("roundUsageMs") or 0)
        foreground_open = bool(group.get("foregroundOpen"))
        if foreground_open:
            usage_ms += elapsed_since_snapshot_ms
        usage = _minutes(usage_ms) + ("（估算）" if foreground_open else "")
        state = str(group.get("effectiveState") or "NORMAL")
        foreground = "，当前前台打开" if foreground_open else ""
        lines.append(f"- {group_id} → {display_name}：本轮 {usage}，状态 {state}{foreground}")
    return "\n".join(lines)


def inject_app_supervision_context(messages: list[dict]) -> list[dict]:
    if any("APP_LOCK" in str(item.get("content") or "") for item in messages):
        return messages
    text = build_app_supervision_ability_text()
    if not text:
        return messages
    insert_at = max(0, len(messages) - 1)
    messages.insert(insert_at, {
        "role": "user",
        "content": f"[应用监管能力与当前状态]\n{text}",
    })
    return messages
