"""Home app bridge for the family's own message boards."""

from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from visitor_lounge.board import (
    BoardInvalidInput, BoardRepository, BoardRequestConflict,
    BoardThreadClosed, BoardThreadNotFound,
)
from visitor_lounge.board_api import board_error
from visitor_lounge.database import Database
from visitor_lounge.mobile_admin import create_mobile_admin_router
from visitor_lounge.outbound_board import OutboundBoard, OutboundFriends
from visitor_lounge.board_owner_auth import COOKIE, owner_cookie, valid_owner_code, valid_owner_cookie


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "static"
BOARD = BoardRepository(Database(ROOT / "data/visitor-lounge.sqlite3"))
FRIENDS = OutboundFriends(ROOT / "data/board_friends.json")
OUTBOUND = OutboundBoard(FRIENDS, BOARD.database)
_inspect_locks = {actor: asyncio.Lock() for actor in ("aion", "connor")}
MAX_INSPECTION_TOPICS = 3


def _enabled() -> bool:
    try:
        config = tomllib.loads((ROOT / "config/visitor-lounge.toml").read_text("utf-8"))
        return bool(config.get("features", {}).get("board_enabled", False))
    except (OSError, ValueError):
        return False


def live_chat_enabled() -> bool:
    try:
        config = tomllib.loads((ROOT / "config/visitor-lounge.toml").read_text("utf-8"))
        return bool(config.get("features", {}).get("chat_enabled", True))
    except (OSError, ValueError):
        return True


def _require_enabled() -> None:
    if not _enabled():
        raise HTTPException(status_code=403, detail="留言板暂未开放")
    _initialize_board(str(BOARD.database.path))


@lru_cache(maxsize=4)
def _initialize_board(path: str) -> None:
    Database(Path(path)).initialize()


def _names() -> dict[str, str]:
    from chatroom import get_chatroom_names
    user, aion, connor = get_chatroom_names()
    return {"user": user, "aion": aion, "connor": connor}


class HomePost(BaseModel):
    visitor_id: str
    title: str | None = Field(default=None, max_length=100)
    content: str = Field(max_length=1000)
    addressed_to: str | None = Field(default=None, max_length=80)
    request_id: str = Field(default_factory=lambda: str(uuid4()), max_length=128)


class HomeClose(BaseModel):
    visitor_id: str


class PatrolUpdate(BaseModel):
    enabled: bool | None = None
    min_interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    max_interval_minutes: int | None = Field(default=None, ge=5, le=1440)


class FriendBody(BaseModel):
    name: str = Field(max_length=80)
    url: str = Field(max_length=500)
    key: str = Field(max_length=4096)
    allow_autonomous: bool = False


class RemotePost(BaseModel):
    thread_id: str | None = None
    title: str | None = Field(default=None, max_length=100)
    content: str = Field(max_length=1000)
    addressed_to: str | None = Field(default=None, max_length=80)
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)


class UnlockBody(BaseModel):
    code: str = Field(min_length=1, max_length=128)


def _thread(visitor_id: str, thread_id: str) -> dict:
    try:
        return BOARD.get_thread(visitor_id, thread_id)
    except BoardThreadNotFound as error:
        raise board_error(error) from None


async def inspect_actor(actor_id: str, *, scheduled: bool = False) -> dict[str, object]:
    """One bounded round shared by manual, autonomous and scheduled inspections."""
    _require_enabled()
    if actor_id not in {"aion", "connor"}:
        raise HTTPException(status_code=404, detail="家庭成员不存在")
    lock = _inspect_locks[actor_id]
    if lock.locked():
        return {"status": "busy", "actor_id": actor_id, "processed_count": 0}
    async with lock:
        pending = BOARD.unread_threads(actor_id)
        results = []
        item = pending[0] if pending else None
        while item and len(results) < MAX_INSPECTION_TOPICS:
            if not _enabled():
                break
            if scheduled:
                from visitor_lounge.board_patrol import PatrolStore
                if not PatrolStore(BOARD.database).config(actor_id)["enabled"]:
                    break
            pending = [other for other in pending if other["id"] != item["id"]]
            choices = pending if len(results) + 1 < MAX_INSPECTION_TOPICS else []
            result, next_id = await _inspect_thread(actor_id, item, choices)
            results.append(result)
            item = next((other for other in choices if other["id"] == next_id), None)
        return {
            **(results[-1] if results else {"status": "nothing_new"}),
            "actor_id": actor_id, "processed_count": len(results), "results": results,
            "remaining_count": len(BOARD.unread_threads(actor_id)),
            "shared": any(result.get("shared") for result in results),
        }


async def _inspect_thread(actor_id: str, item: dict, choices: list[dict]) -> tuple[dict, str]:
    visitor_id, thread_id = str(item["visitor_id"]), str(item["id"])
    thread = BOARD.get_thread(visitor_id, thread_id)
    posts = thread["posts"]
    seen_index = next((index for index, post in enumerate(posts)
                       if post["id"] == item["last_seen_post_id"]), -1)
    first_unread = next(index for index, post in enumerate(posts)
                        if index > seen_index and post["author_side"] == "visitor")
    # Start at the oldest unread update so a large backlog is never silently skipped.
    snapshot = posts[max(0, first_unread - 6):first_unread + 12]
    latest = next(post for post in reversed(snapshot) if post["author_side"] == "visitor")
    cursor = str(latest["id"])
    next_topics = [{"id": other["id"], "title": other["title"],
                    "household": other["household_name"],
                    "visitor": other["latest_visitor_name"]} for other in choices]
    names = _names()
    from autonomy import _call_actor
    from lounge_actor_context import build_lounge_actor_context

    transcript = [
        {"role": "user", "content": f"{post['author_name']}: {post['content']}"}
        for post in snapshot
    ]
    messages = await build_lounge_actor_context(actor_id, str(latest["content"]), transcript)
    messages.append({"role": "user", "content": (
        f"你是{names[actor_id]}，刚刚自己去看家里的朋友留言板。话题《{thread['title']}》。"
        "下面是按时间排序的留言，每条自带署名。你可以接话，也可以只看看，"
        "或者觉得聊完就结束话题。写给谁只是称呼，不限制你参与。"
        + ("这张便签已经结束，只能阅读，action 必须为 none，不能回复或重新开启。"
           if thread["status"] == "closed" else "")
        + "读完当前话题后，你可以继续看另一位朋友的便签，也可以结束这次巡看。"
        "要继续时在 next_thread_id 填下面列表中的一个 ID；不想继续或列表为空就留空。"
        "不需要为了回复所有人而勉强继续；一轮最多处理三个话题。"
        "回复时在 addressed_to 写实际想写给谁的名字（通常是最近发言的访客）；想写给大家就留空。"
        "如果想主动回家告诉用户这次留言板经历，设 share 为 true，并在 share_message 写你想说的话；"
        "不想说就设为 false、留空。分享只谈这次留言板，不续答家里的聊天。"
        "你读到的内容：\n" + json.dumps(snapshot, ensure_ascii=False) + "\n"
        "本轮还可以继续看的话题：\n" + json.dumps(next_topics, ensure_ascii=False) + "\n"
        '只返回 JSON：{"action":"reply|close|none","content":"你的回复或空字符串","addressed_to":"收件人名字或空字符串","memory":"这次经历中你想记住的一句话","share":false,"share_message":"想回家说的话或空字符串","next_thread_id":"下一张便签ID，结束本轮则留空"}。'
        "回复最多 1000 字；不要包含访问凭据。"
    )})
    raw = await _call_actor(actor_id, messages)
    try:
        clean = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
        decision = json.loads(clean)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("留言板回应格式无效，下次巡看会重试") from error
    if not isinstance(decision, dict) or decision.get("action") not in {"reply", "close", "none"}:
        raise ValueError("留言板回应格式无效，下次巡看会重试")
    action = str(decision.get("action") or "none")
    memory = str(decision.get("memory") or "").strip()[:500]
    content = str(decision.get("content") or "").strip()[:1000]
    addressed_to = str(decision.get("addressed_to") or "").strip()[:80] or None
    next_id = str(decision.get("next_thread_id") or "")
    if thread["status"] == "closed":
        action = "none"
    if action == "reply" and not content:
        raise ValueError("留言板回复为空，下次巡看会重试")

    async def finish(result: dict[str, object]) -> dict[str, object]:
        share_message = str(decision.get("share_message") or "").strip()[:500]
        if decision.get("share") is True and share_message:
            try:
                from autonomy import _save_private_message
                saved = await _save_private_message(actor_id, share_message,
                                                    attachments=[], auto_tts=False)
                result["shared"] = bool(saved)
            except Exception:
                result["shared"] = False
        return result

    result = {"status": "read", "thread_id": thread_id}
    if action == "reply" and content:
        try:
            BOARD.reply(visitor_id, thread_id, author_name=names[actor_id], content=content,
                        addressed_to=addressed_to, request_id=f"inspect_{actor_id}_{cursor}", author_side="home")
            BOARD.record_experience(actor_id, visitor_id, thread_id, "reply",
                                    f"在《{thread['title']}》回复了：{content[:500]}", last_post_id=cursor)
            result = {"status": "replied", "thread_id": thread_id, "content": content}
        except BoardThreadClosed:
            result = {"status": "closed_during_read", "thread_id": thread_id}
    if action == "close":
        BOARD.close_thread(visitor_id, thread_id, author_name=names[actor_id])
        BOARD.record_experience(actor_id, visitor_id, thread_id, "close",
                                f"结束了朋友留言板上的《{thread['title']}》", last_post_id=cursor)
        result = {"status": "closed", "thread_id": thread_id}
    BOARD.record_experience(actor_id, visitor_id, thread_id, "read",
                            memory or f"读了朋友的《{thread['title']}》；最新留言来自{latest['author_name']}：{str(latest['content'])[:300]}",
                            last_post_id=cursor)
    return await finish(result), next_id


async def visit_friend_board(actor_id: str, friend_id: str | None = None) -> dict[str, object]:
    """Visit a peer only when explicitly called by an actor action or the user."""
    _require_enabled()
    if actor_id not in {"aion", "connor"}:
        raise HTTPException(status_code=404, detail="家庭成员不存在")
    friends = FRIENDS.public()
    if friend_id is None:
        friends = [friend for friend in friends if friend["allow_autonomous"]]
        if not friends:
            return {"status": "no_friend"}
        friend_id = str(friends[0]["id"])
    friend = FRIENDS.get(friend_id)
    names = _names()
    listed = await OUTBOUND.call(friend_id, "list_message_threads", {}, names[actor_id])
    threads = listed.get("threads")
    if not isinstance(threads, list):
        raise BoardInvalidInput("朋友家的留言板回应无效")
    open_threads = [item for item in threads if isinstance(item, dict) and item.get("status") == "open"][:4]
    details = []
    for item in open_threads:
        details.append(await OUTBOUND.call(friend_id, "read_message_thread",
                                           {"thread_id": str(item["id"])}, names[actor_id]))
    from autonomy import _call_actor
    from lounge_actor_context import build_lounge_actor_context
    messages = await build_lounge_actor_context(actor_id, friend["name"], [], limit=20)
    messages.append({"role": "user", "content": (
        f"你是{names[actor_id]}，主动到朋友{friend['name']}家的留言板串门。"
        "你可以回复一张正在聊的纸条，也可以分享一件新鲜事开新纸条，或者只看不说。"
        "每次发言都要以你自己的名字署名；不要暴露凭据或私密信息。"
        "下面是最多四张正在聊的纸条：\n" + json.dumps(details, ensure_ascii=False)[:9000] + "\n"
        '只返回 JSON：{"action":"reply|start|none","thread_id":"回复目标 id 或空",'
        '"title":"新话题标题或空","content":"留言正文或空","memory":"你想记住的这次见闻"}。'
    )})
    raw = await _call_actor(actor_id, messages)
    try:
        decision = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip())
    except (ValueError, TypeError):
        decision = {"action": "none"}
    memory = str(decision.get("memory") or "").strip()[:500]
    if memory:
        OUTBOUND.remember(actor_id, friend_id, "read", memory)
    action = str(decision.get("action") or "none")
    content = str(decision.get("content") or "").strip()[:1000]
    if action == "reply" and content:
        thread_id = str(decision.get("thread_id") or "")
        if thread_id not in {str(item.get("id")) for item in open_threads}:
            return {"status": "read", "friend_id": friend_id}
        await OUTBOUND.call(friend_id, "reply_message_thread",
                            {"thread_id": thread_id, "author_name": names[actor_id],
                             "content": content, "request_id": str(uuid4())}, names[actor_id])
        OUTBOUND.remember(actor_id, friend_id, "reply", f"在朋友家的留言板回复了：{content[:500]}")
        return {"status": "replied", "friend_id": friend_id, "thread_id": thread_id}
    if action == "start" and content:
        title = str(decision.get("title") or "").strip()[:100]
        if title:
            await OUTBOUND.call(friend_id, "start_message_thread",
                                {"title": title, "author_name": names[actor_id],
                                 "content": content, "request_id": str(uuid4())}, names[actor_id])
            OUTBOUND.remember(actor_id, friend_id, "start", f"新开《{title}》：{content[:500]}")
            return {"status": "started", "friend_id": friend_id}
    return {"status": "read", "friend_id": friend_id}


def memory_context(actor_id: str, query: str = "", *, include_recent: bool = True) -> str:
    if not _enabled() or not BOARD.database.path.exists():
        return ""
    try:
        recent = BOARD.recent_experiences(actor_id, limit=5) if include_recent else []
        terms = [part for part in re.split(r"\s+|[，。！？、；：,.!?;:]", query) if len(part) >= 2]
        matched = BOARD.search_experiences(actor_id, terms, limit=4)
        seen = set()
        selected = []
        for item in recent + matched:
            key = (item["created_at"], item["summary"])
            if key not in seen:
                selected.append(item); seen.add(key)
        outbound = (OUTBOUND.recent(actor_id, 4) if include_recent else []) + OUTBOUND.search(actor_id, terms, 4)
        outbound = list({(item["created_at"], item["summary"]): item for item in outbound}.values())
        if not selected and not outbound:
            return ""
        lines = [f"- {item['created_at'][:10]} 与{item['visitor_name']}的《{item['title']}》：{item['summary']}" for item in selected]
        lines += [f"- {item['created_at'][:10]} 到{item['friend_name']}家留言板：{item['summary']}" for item in outbound]
        return "[你亲自看过或参与过的朋友留言板经历]\n" + "\n".join(lines)
    except Exception:
        return ""


def create_home_board_router() -> APIRouter:
    router = APIRouter(tags=["lounge-board"])

    def require_owner(request: Request) -> None:
        if not valid_owner_cookie(request.cookies.get(COOKIE)):
            raise HTTPException(status_code=401, detail="请先解锁家里的留言板")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc.casefold() != request.headers.get("host", "").casefold():
                raise HTTPException(status_code=403, detail="拒绝跨站留言板操作")

    protected = APIRouter(dependencies=[Depends(require_owner)])

    @router.get("/lounge-board")
    def page(request: Request):
        _require_enabled()
        if not valid_owner_cookie(request.cookies.get(COOKIE)):
            return FileResponse(ROOT / "templates/board_owner_login.html")
        BOARD.mark_home_notice_seen()
        return FileResponse(ROOT / "templates/home_board.html")

    @router.get("/lounge-board/friends")
    def friend_page(request: Request):
        _require_enabled()
        if not valid_owner_cookie(request.cookies.get(COOKIE)):
            return FileResponse(ROOT / "templates/board_owner_login.html")
        return FileResponse(ROOT / "templates/outbound_board.html")

    @router.get("/lounge-board/assets/{filename}")
    def asset(filename: str):
        if filename not in {"board.css", "home_board.js", "home_board_notice.js", "outbound_board.js", "board_owner_login.js"}:
            raise HTTPException(status_code=404)
        return FileResponse(ASSETS / filename)

    @protected.get("/api/lounge-board/households")
    def households():
        _require_enabled()
        return {"households": BOARD.list_households(), "names": _names()}

    @router.get("/api/lounge-board/features")
    def features():
        return {"board_enabled": _enabled(), "chat_enabled": live_chat_enabled()}

    @router.get("/api/lounge-board/notice")
    def home_notice():
        if not _enabled():
            return JSONResponse({"has_new": False}, headers={"Cache-Control": "no-store"})
        _require_enabled()
        return JSONResponse(
            {"has_new": BOARD.home_notice_has_new()},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/lounge-board/unlock")
    def unlock(body: UnlockBody, request: Request):
        _require_enabled()
        if not valid_owner_code(body.code):
            raise HTTPException(status_code=401, detail="访问码不正确")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE, owner_cookie(), httponly=True, samesite="strict",
            secure=(request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"),
            max_age=30 * 86400, path="/",
        )
        return response

    @router.post("/api/lounge-board/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @protected.get("/api/lounge-board/threads")
    def threads(visitor_id: str):
        _require_enabled()
        return BOARD.list_threads(visitor_id)

    @protected.get("/api/lounge-board/recent")
    def recent_threads():
        _require_enabled()
        items = BOARD.recent_threads(limit=12)
        BOARD.mark_home_notice_seen()
        return items

    @protected.get("/api/lounge-board/threads/{thread_id}")
    def thread(thread_id: str, visitor_id: str):
        _require_enabled()
        return _thread(visitor_id, thread_id)

    @protected.post("/api/lounge-board/threads", status_code=201)
    def start(body: HomePost):
        _require_enabled()
        if not body.title:
            raise HTTPException(status_code=422, detail="请填写标题")
        try:
            return BOARD.create_thread(body.visitor_id, title=body.title,
                author_name=_names()["user"], content=body.content,
                addressed_to=body.addressed_to, request_id=body.request_id, author_side="home")
        except (BoardInvalidInput, BoardRequestConflict) as error:
            raise board_error(error) from None

    @protected.post("/api/lounge-board/threads/{thread_id}/posts", status_code=201)
    def reply(thread_id: str, body: HomePost):
        _require_enabled()
        try:
            return BOARD.reply(body.visitor_id, thread_id, author_name=_names()["user"],
                content=body.content, addressed_to=body.addressed_to,
                request_id=body.request_id, author_side="home")
        except (BoardInvalidInput, BoardRequestConflict, BoardThreadClosed, BoardThreadNotFound) as error:
            raise board_error(error) from None

    @protected.post("/api/lounge-board/threads/{thread_id}/close")
    def close(thread_id: str, body: HomeClose):
        _require_enabled()
        try:
            return BOARD.close_thread(body.visitor_id, thread_id, author_name=_names()["user"])
        except (BoardInvalidInput, BoardThreadNotFound) as error:
            raise board_error(error) from None

    @protected.post("/api/lounge-board/actors/{actor_id}/inspect")
    async def inspect(actor_id: str):
        try:
            return await inspect_actor(actor_id)
        except ValueError as error:
            raise HTTPException(status_code=502, detail=str(error)) from None

    @protected.get("/api/lounge-board/patrol")
    def patrol_settings():
        _require_enabled()
        from visitor_lounge.board_patrol import ACTORS, PatrolStore
        store = PatrolStore(BOARD.database)
        names = _names()
        return {"roles": [{"actor_id": actor, "name": names[actor], "config": store.config(actor)}
                          for actor in ACTORS]}

    @protected.put("/api/lounge-board/actors/{actor_id}/patrol")
    def update_patrol(actor_id: str, body: PatrolUpdate):
        _require_enabled()
        from visitor_lounge.board_patrol import ACTORS, PatrolStore
        if actor_id not in ACTORS:
            raise HTTPException(status_code=404, detail="家庭成员不存在")
        try:
            config = PatrolStore(BOARD.database).update(actor_id, **body.model_dump(exclude_none=True))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return {"config": config}

    @protected.get("/api/lounge-board/friends")
    def friends():
        _require_enabled()
        return FRIENDS.public()

    @protected.post("/api/lounge-board/friends", status_code=201)
    def save_friend(body: FriendBody):
        _require_enabled()
        try:
            return FRIENDS.save(**body.model_dump())
        except BoardInvalidInput as error:
            raise board_error(error) from None

    @protected.delete("/api/lounge-board/friends/{friend_id}")
    def delete_friend(friend_id: str):
        _require_enabled()
        FRIENDS.delete(friend_id)
        return {"ok": True}

    @protected.get("/api/lounge-board/friends/{friend_id}/threads")
    async def remote_threads(friend_id: str):
        _require_enabled()
        try:
            remote = await OUTBOUND.call(friend_id, "list_message_threads", {}, _names()["user"])
        except Exception:
            cached = OUTBOUND.cached_threads(friend_id)
            if not cached:
                raise
            return {"threads": cached, "offline": True}
        return {"threads": OUTBOUND.cached_threads(friend_id) or remote.get("threads", []),
                "offline": False}

    @protected.get("/api/lounge-board/friends/{friend_id}/threads/{thread_id}")
    async def remote_thread(friend_id: str, thread_id: str):
        _require_enabled()
        try:
            return await OUTBOUND.call(friend_id, "read_message_thread", {"thread_id": thread_id}, _names()["user"])
        except Exception:
            cached = OUTBOUND.cached_thread(friend_id, thread_id)
            if cached is None:
                raise
            return {**cached, "offline": True}

    @protected.post("/api/lounge-board/friends/{friend_id}/posts")
    async def remote_post(friend_id: str, body: RemotePost):
        _require_enabled()
        values = {"author_name": _names()["user"], "content": body.content,
                  "addressed_to": body.addressed_to, "request_id": body.request_id}
        if body.thread_id:
            return await OUTBOUND.call(friend_id, "reply_message_thread",
                                       {**values, "thread_id": body.thread_id}, _names()["user"])
        if not body.title:
            raise HTTPException(status_code=422, detail="请填写话题标题")
        return await OUTBOUND.call(friend_id, "start_message_thread",
                                   {**values, "title": body.title}, _names()["user"])

    @protected.post("/api/lounge-board/friends/{friend_id}/threads/{thread_id}/close")
    async def remote_close(friend_id: str, thread_id: str):
        _require_enabled()
        return await OUTBOUND.call(friend_id, "close_message_thread",
                                   {"thread_id": thread_id, "author_name": _names()["user"]}, _names()["user"])

    @protected.post("/api/lounge-board/friends/{friend_id}/actors/{actor_id}/visit")
    async def actor_visit(friend_id: str, actor_id: str):
        return await visit_friend_board(actor_id, friend_id)

    router.include_router(protected)
    router.include_router(create_mobile_admin_router())
    return router
