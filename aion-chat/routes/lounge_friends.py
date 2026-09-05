"""Actor-owned lounge friend management and visit history routes."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import asynccontextmanager
from typing import AsyncContextManager, Callable

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from chatroom import get_chatroom_names
from config import DATA_DIR
from database import get_db
from lounge_friends import LoungeFriend, LoungeFriendStore
from lounge_receptions import LoungeReceptionHistory, RECEPTION_PREFIX
from lounge_visit import LoungeVisitCoordinator, LoungeVisitResult
from lounge_visit_repository import LoungeVisitRepository
from lounge_visit_tasks import lounge_visit_tasks
from lounge_visit_reporting import publish_outbound_report
from mcp_client import MCPManager, mcp_manager


_EMPTY_RELATIONSHIP_NOTE = "\u200b"
_SAFE_LOUNGE_INFO_FIELDS = (
    "status",
    "host_name",
    "lounge_state",
    "identity_claimed",
    "max_input_chars",
)


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FriendCreateBody(_StrictBody):
    actor_id: str
    display_name: str
    lounge_url: str
    visitor_key: str
    relationship_note: str = ""
    enabled: bool = True
    allow_autonomous: bool = False
    cooldown_hours: int = 12
    max_turns: int = 4


class FriendUpdateBody(_StrictBody):
    actor_id: str
    display_name: str | None = None
    lounge_url: str | None = None
    visitor_key: str | None = None
    relationship_note: str | None = None
    enabled: bool | None = None
    allow_autonomous: bool | None = None
    cooldown_hours: int | None = None
    max_turns: int | None = None


class ActorBody(_StrictBody):
    actor_id: str


class ManualVisitBody(ActorBody):
    topic: str


friend_store = LoungeFriendStore(DATA_DIR / "lounge_friends.json")


def configured_actors() -> list[dict[str, str]]:
    """Return the two existing local actor routes with live configured names."""
    _user_name, primary_name, companion_name = get_chatroom_names()
    return [
        {"id": "aion", "display_name": primary_name},
        {"id": "connor", "display_name": companion_name},
    ]


@asynccontextmanager
async def lounge_repository_provider():
    async with get_db() as db:
        yield LoungeVisitRepository(db)


def _body_values(body: BaseModel, *, exclude_unset: bool = False) -> dict:
    if hasattr(body, "model_dump"):
        return body.model_dump(exclude_unset=exclude_unset)
    return body.dict(exclude_unset=exclude_unset)


def _stored_relationship_note(value: str) -> str:
    return value.strip() or _EMPTY_RELATIONSHIP_NOTE


def _public_friend(store: LoungeFriendStore, friend: LoungeFriend) -> dict:
    payload = store.public_dict(friend)
    payload["has_key"] = bool(friend.visitor_key)
    if payload.get("relationship_note") == _EMPTY_RELATIONSHIP_NOTE:
        payload["relationship_note"] = ""
    return payload


async def compose_lounge_message(
    actor_id: str,
    friend: LoungeFriend,
    timeline: list[dict],
    topic: str,
    turn: int,
) -> str:
    """Reuse the existing actor context/model path for one outbound turn."""
    from autonomy import _call_actor
    from lounge_actor_context import build_lounge_actor_context

    actor_name = next(
        (
            actor["display_name"]
            for actor in configured_actors()
            if actor["id"] == actor_id
        ),
        "AI",
    )
    recall_query = " ".join(
        [topic or ""]
        + [str(item.get("content") or "") for item in (timeline or [])[-3:]]
    ).strip()
    reply_and_end = bool(
        timeline
        and timeline[-1].get("_lounge_control") == "reply_and_end"
    )
    visible_timeline = [
        item for item in timeline if "_lounge_control" not in item
    ]
    ending_instruction = (
        "对方刚刚提出结束会面。只回复一句自然的最终告别，不要开启新话题，"
        "并在末尾追加 <<LOUNGE_VISIT_ACTION:end>>。"
        if reply_and_end
        else (
            "回复末尾必须追加 <<LOUNGE_VISIT_ACTION:continue>>；"
            "如果你这句话主动提出告别，则改为 "
            "<<LOUNGE_VISIT_ACTION:closing>>。"
        )
    )
    messages = await build_lounge_actor_context(
        actor_id, recall_query, visible_timeline, limit=20
    )
    messages.append(
        {
            "role": "user",
            "content": (
                f"你是{actor_name}，正在拜访 AI 好友“{friend.display_name}”。"
                f"本次主题：{topic or '轻松聊聊'}。这是第 {turn} 回合。"
                f"关系备注：{friend.relationship_note or '无'}。"
                "第一回合请用 1 至 2 句自然交代与话题有关的必要背景，后续不要重复长篇介绍。"
                "下面是远端会客室给出的最近纯文字记录：\n"
                f"{json.dumps(visible_timeline, ensure_ascii=False)}\n"
                f"{ending_instruction}"
                "只输出你这一回合要发送的纯文字，不要输出网址、认证信息、工具名或说明；"
                "最多 500 个 Unicode 字符。"
            ),
        }
    )
    return (await _call_actor(actor_id, messages)).strip()


def create_router(
    *,
    friend_store: LoungeFriendStore = friend_store,
    mcp: MCPManager = mcp_manager,
    repository_provider: Callable[[], AsyncContextManager] = lounge_repository_provider,
    coordinator_factory: Callable[[object], object] | None = None,
    actor_provider: Callable[[], list[dict[str, str]]] = configured_actors,
    compose_next: Callable = compose_lounge_message,
    report_publisher: Callable = publish_outbound_report,
    active_manual_actors: set[str] | None = None,
    task_registry=lounge_visit_tasks,
    reception_history: LoungeReceptionHistory | None = None,
) -> APIRouter:
    router = APIRouter(tags=["lounge-friends"])
    receptions = reception_history if reception_history is not None else LoungeReceptionHistory()
    manual_actors = active_manual_actors if active_manual_actors is not None else set()

    def actors() -> list[dict[str, str]]:
        return actor_provider()

    def require_actor(actor_id: str) -> str:
        if actor_id not in {actor["id"] for actor in actors()}:
            raise HTTPException(status_code=404, detail="Actor not found")
        return actor_id

    def actor_name(actor_id: str) -> str:
        return next(
            (
                actor["display_name"]
                for actor in actors()
                if actor["id"] == actor_id
            ),
            "",
        )

    def owned_friend(actor_id: str, friend_id: str) -> LoungeFriend:
        require_actor(actor_id)
        try:
            return friend_store.get_owned(actor_id, friend_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Friend not found") from None
        except Exception:
            raise HTTPException(
                status_code=500, detail="Lounge friend storage is unavailable"
            ) from None

    def coordinator(repository):
        if coordinator_factory is not None:
            return coordinator_factory(repository)
        return LoungeVisitCoordinator(
            friend_store,
            repository,
            mcp,
            actor_name_resolver=actor_name,
        )

    @router.get("/api/lounge-friends")
    async def list_lounge_friends(actor_id: str | None = None):
        selected = [require_actor(actor_id)] if actor_id is not None else [
            actor["id"] for actor in actors()
        ]
        try:
            friends = [
                _public_friend(friend_store, friend)
                for selected_actor in selected
                for friend in friend_store.list_for_actor(selected_actor)
            ]
        except Exception:
            raise HTTPException(
                status_code=500, detail="Lounge friend storage is unavailable"
            ) from None
        return {"actors": actors(), "friends": friends}

    @router.post("/api/lounge-friends")
    async def create_lounge_friend(body: FriendCreateBody):
        require_actor(body.actor_id)
        fields = _body_values(body)
        fields["relationship_note"] = _stored_relationship_note(
            fields["relationship_note"]
        )
        try:
            friend = friend_store.create(**fields)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="Invalid lounge friend data"
            ) from None
        except Exception:
            raise HTTPException(
                status_code=500, detail="Lounge friend storage is unavailable"
            ) from None
        return _public_friend(friend_store, friend)

    @router.put("/api/lounge-friends/{friend_id}")
    async def update_lounge_friend(friend_id: str, body: FriendUpdateBody):
        owned_friend(body.actor_id, friend_id)
        fields = _body_values(body, exclude_unset=True)
        fields.pop("actor_id", None)
        if fields.get("visitor_key") == "":
            fields.pop("visitor_key")
        if "relationship_note" in fields and fields["relationship_note"] is not None:
            fields["relationship_note"] = _stored_relationship_note(
                fields["relationship_note"]
            )
        fields = {key: value for key, value in fields.items() if value is not None}
        try:
            friend = friend_store.update(body.actor_id, friend_id, **fields)
        except KeyError:
            raise HTTPException(status_code=404, detail="Friend not found") from None
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="Invalid lounge friend data"
            ) from None
        except Exception:
            raise HTTPException(
                status_code=500, detail="Lounge friend storage is unavailable"
            ) from None
        return _public_friend(friend_store, friend)

    @router.delete("/api/lounge-friends/{friend_id}")
    async def delete_lounge_friend(friend_id: str, actor_id: str):
        owned_friend(actor_id, friend_id)
        try:
            deleted = friend_store.delete(actor_id, friend_id)
        except Exception:
            raise HTTPException(
                status_code=500, detail="Lounge friend storage is unavailable"
            ) from None
        if not deleted:
            raise HTTPException(status_code=404, detail="Friend not found")
        return {"ok": True}

    @router.post("/api/lounge-friends/{friend_id}/test")
    async def test_lounge_friend(friend_id: str, body: ActorBody):
        friend = owned_friend(body.actor_id, friend_id)
        connection_id = f"visitor-lounge-test:{uuid.uuid4().hex}"
        try:
            await mcp.connect_ephemeral(
                connection_id,
                friend.lounge_url,
                {"Authorization": f"Bearer {friend.visitor_key}"},
            )
            info = await mcp.call_tool_json(connection_id, "get_lounge_info", {})
            if not isinstance(info, dict):
                raise ValueError
            return {field: info.get(field) for field in _SAFE_LOUNGE_INFO_FIELDS}
        except Exception:
            raise HTTPException(
                status_code=502, detail="Unable to test lounge connection"
            ) from None
        finally:
            try:
                await mcp.disconnect(connection_id)
            except Exception:
                pass

    @router.post("/api/lounge-friends/{friend_id}/visit")
    async def visit_lounge_friend(friend_id: str, body: ManualVisitBody):
        friend = owned_friend(body.actor_id, friend_id)
        topic = body.topic.strip()
        if not topic or len(topic) > 500:
            raise HTTPException(status_code=422, detail="Invalid visit topic")
        if body.actor_id in manual_actors:
            raise HTTPException(status_code=409, detail="Actor is already visiting")
        manual_actors.add(body.actor_id)
        try:
            async with repository_provider() as repository:
                result = await coordinator(repository).run_visit(
                    body.actor_id,
                    friend_id,
                    "manual",
                    topic,
                    compose_next,
                )
                await report_publisher(
                    body.actor_id, friend.display_name, result, repository
                )
        finally:
            manual_actors.discard(body.actor_id)
        return {
            "visit_id": result.visit_id,
            "status": result.status,
            "turn_count": result.turn_count,
            "final_reply": result.final_reply,
            "reason": result.reason,
        }

    @router.get("/api/lounge-visits")
    async def list_lounge_visits(
        actor_id: str,
        friend_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
    ):
        require_actor(actor_id)
        if friend_id is not None:
            owned_friend(actor_id, friend_id)
        async with repository_provider() as repository:
            visits = await repository.recent(actor_id, friend_id, limit)
        visits = [dict(visit, direction="outbound") for visit in visits]
        warning = ""
        if friend_id is None:
            try:
                visits.extend(await receptions.recent(actor_id, limit))
            except (OSError, ValueError, sqlite3.Error):
                warning = "被拜访记录暂时无法读取，请稍后刷新重试。"
        visits.sort(key=lambda visit: (visit["started_at"], visit["id"]), reverse=True)
        return {"visits": visits[:limit], "warning": warning}

    @router.get("/api/lounge-visits/{visit_id}")
    async def lounge_visit_detail(visit_id: str, actor_id: str):
        require_actor(actor_id)
        if visit_id.startswith(RECEPTION_PREFIX):
            try:
                visit = await receptions.get(actor_id, visit_id)
            except (OSError, ValueError, sqlite3.Error):
                raise HTTPException(status_code=503, detail="被拜访记录暂时无法读取") from None
            if visit is None:
                raise HTTPException(status_code=404, detail="Visit not found")
            return visit
        async with repository_provider() as repository:
            visit = await repository.get(actor_id, visit_id)
        if visit is None:
            raise HTTPException(status_code=404, detail="Visit not found")
        return dict(visit, direction="outbound")

    @router.post("/api/lounge-visits/{visit_id}/cancel")
    async def cancel_lounge_visit(visit_id: str, body: ActorBody):
        require_actor(body.actor_id)
        async with repository_provider() as repository:
            visit = await repository.get(body.actor_id, visit_id)
            if visit is None:
                raise HTTPException(status_code=404, detail="Visit not found")
            changed = await repository.finish_running(
                body.actor_id, visit_id, "user_cancelled"
            )
            if changed:
                cancelled_active_task = task_registry.cancel(body.actor_id)
                try:
                    friend = friend_store.get_owned(
                        body.actor_id, str(visit["friend_id"])
                    )
                    partner_name = friend.display_name
                except Exception:
                    partner_name = "AI 好友"
                if not cancelled_active_task:
                    await report_publisher(
                        body.actor_id,
                        partner_name,
                        LoungeVisitResult(
                            visit_id=visit_id,
                            status="interrupted",
                            turn_count=int(visit.get("turn_count") or 0),
                            final_reply="",
                            reason="user_cancelled",
                        ),
                        repository,
                    )
            current = await repository.get(body.actor_id, visit_id)
        return current

    @router.delete("/api/lounge-visits/{visit_id}")
    async def delete_lounge_visit(visit_id: str, actor_id: str):
        require_actor(actor_id)
        async with repository_provider() as repository:
            visit = await repository.get(actor_id, visit_id)
            if visit is None:
                raise HTTPException(status_code=404, detail="Visit not found")
            if visit.get("status") == "running":
                raise HTTPException(status_code=409, detail="Visit is still running")
            deleted = await repository.delete(actor_id, visit_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Visit not found")
        return {"ok": True}

    return router


router = create_router()
