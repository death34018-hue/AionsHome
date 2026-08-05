from fastapi import APIRouter
from pydantic import BaseModel, Field

from app_supervision import role_catalog
from app_supervision_ai import state_cache as supervision_state_cache
from app_supervision_ai import (
    list_pending_app_supervision_commands,
    expire_pending_app_supervision_commands,
    acknowledge_app_supervision_command,
    get_app_supervision_command,
    format_app_supervision_result_message,
)
from capabilities import is_capability_enabled
from database import get_db


router = APIRouter()


class StateGroup(BaseModel):
    groupId: str
    displayName: str = ""
    roleId: str = ""
    roundUsageMs: int = Field(default=0, ge=0)
    foregroundOpen: bool = False
    effectiveState: str = "NORMAL"
    lock: dict | None = None
    temporaryUnlock: dict | None = None


class StateDeviceLock(BaseModel):
    effectiveState: str = "NORMAL"
    lock: dict | None = None
    temporaryUnlock: dict | None = None


class StateReport(BaseModel):
    eventId: str = ""
    eventType: str = "snapshot"
    triggerGroupId: str = ""
    checkpointMinutes: int = Field(default=0, ge=0, le=120)
    groups: list[StateGroup] = Field(default_factory=list)
    deviceLock: StateDeviceLock | None = None


class CommandAck(BaseModel):
    commandId: str
    success: bool
    reason: str = ""


@router.get("/api/app-supervision/roles")
async def get_roles():
    return {"roles": role_catalog()}


@router.get("/api/app-supervision/runtime-config")
async def get_runtime_config():
    return {
        "featureEnabled": is_capability_enabled("app_supervision"),
        "roles": role_catalog(),
    }


@router.post("/api/app-supervision/state")
async def report_state(body: StateReport):
    enabled = is_capability_enabled("app_supervision")
    if not enabled:
        supervision_state_cache.clear()
        return {"featureEnabled": False, "checkpointAccepted": False}
    snapshot = {
        "eventType": body.eventType,
        "groups": [group.model_dump() for group in body.groups],
    }
    if body.deviceLock is not None:
        snapshot["deviceLock"] = body.deviceLock.model_dump()
    supervision_state_cache.replace_snapshot(snapshot)
    checkpoint_accepted = False
    if body.eventType == "checkpoint":
        checkpoint_accepted = supervision_state_cache.accept_event_once(body.eventId)
        if checkpoint_accepted:
            trigger_group = next(
                (group.model_dump() for group in body.groups if group.groupId == body.triggerGroupId),
                None,
            )
            if trigger_group and body.checkpointMinutes > 0:
                import asyncio
                from schedule import schedule_mgr
                asyncio.create_task(schedule_mgr.fire_app_supervision_checkpoint(
                    trigger_group,
                    event_id=body.eventId,
                    checkpoint_minutes=body.checkpointMinutes,
                ))
    return {
        "featureEnabled": True,
        "checkpointAccepted": checkpoint_accepted,
    }


@router.get("/api/app-supervision/commands/pending")
async def get_pending_commands():
    if not is_capability_enabled("app_supervision"):
        return {"commands": []}
    async with get_db() as db:
        expired = await expire_pending_app_supervision_commands(db)
        commands = await list_pending_app_supervision_commands(db)
        await db.commit()
    for command in expired:
        await _publish_command_result(command, success=False, reason="命令已过期")
    return {"commands": commands}


@router.post("/api/app-supervision/commands/ack")
async def acknowledge_command(body: CommandAck):
    async with get_db() as db:
        command = await get_app_supervision_command(db, body.commandId)
        accepted = await acknowledge_app_supervision_command(
            db,
            body.commandId,
            success=body.success,
            reason=body.reason,
        )
        await db.commit()
    if accepted and command:
        await _publish_command_result(
            command, success=body.success, reason=body.reason
        )
    return {"accepted": accepted}


async def _publish_command_result(command: dict, *, success: bool, reason: str):
    role_names = {item["id"]: item["label"] for item in role_catalog()}
    snapshot, _ = supervision_state_cache.read()
    group_names = {
        str(item.get("groupId") or ""): str(item.get("displayName") or "")
        for item in snapshot.get("groups", [])
        if isinstance(item, dict)
    }
    message = format_app_supervision_result_message(
        command,
        success=success,
        reason=reason,
        role_names=role_names,
        group_names=group_names,
    )
    from schedule import _chatroom_sys_msg, _sys_msg
    if "chatroom" in command["sourceKind"]:
        await _chatroom_sys_msg(
            command["sourceRef"], message,
            after_msg_id=command["sourceMessageId"],
        )
    else:
        await _sys_msg(
            command["sourceRef"], message,
            after_msg_id=command["sourceMessageId"],
        )
