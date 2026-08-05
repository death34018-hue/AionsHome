from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from wechat_bridge import find_wechat_binding_for_sender
from wechat_mode import (
    active_wechat_modes_for_route,
    make_wechat_mode_key,
    render_wechat_bubbles,
)


log = logging.getLogger("wechat_mode")
SendTextHandler = Callable[..., Any]
IdentityResolver = Callable[[str, str, str], Awaitable[tuple[str, str]] | tuple[str, str]]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def extract_wechat_event_route(
    event: dict[str, Any],
) -> tuple[str, str, str, str] | None:
    if not isinstance(event, dict):
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    message_id = str(data.get("id") or "").strip()
    if event.get("type") == "msg_created" and data.get("role") == "assistant":
        source_id = str(data.get("conv_id") or "").strip()
        return ("aion_private", source_id, message_id, "aion") if source_id and message_id else None
    if (
        event.get("type") == "chatroom_msg_created"
        and str(data.get("sender") or "").strip().lower() in {"aion", "connor"}
    ):
        source_id = str(data.get("room_id") or "").strip()
        sender = str(data.get("sender") or "").strip().lower()
        return ("chatroom", source_id, message_id, sender) if source_id and message_id else None
    return None


async def _default_identity_resolver(
    source_type: str,
    source_id: str,
    sender: str,
) -> tuple[str, str]:
    if source_type == "aion_private":
        from config import load_worldbook

        try:
            worldbook = load_worldbook()
        except Exception:
            worldbook = {}
        return "私聊", str(worldbook.get("ai_name") or "AI").strip() or "AI"

    from chatroom import get_chatroom_names

    _user_name, ai_name, connor_name = get_chatroom_names()
    sender_name = {
        "aion": ai_name,
        "connor": connor_name,
    }.get(sender, "AI")
    source_label = "群聊"
    try:
        from database import get_db

        async with get_db() as db:
            cur = await db.execute(
                "SELECT title FROM chatroom_rooms WHERE id=?",
                (source_id,),
            )
            row = await cur.fetchone()
        if row and str(row[0] or "").strip():
            source_label = str(row[0]).strip()
    except Exception:
        pass
    return source_label, str(sender_name or "AI").strip() or "AI"


class WeChatModeDispatcher:
    def __init__(
        self,
        *,
        settings: dict[str, Any] | None = None,
        send_text: SendTextHandler | None = None,
        identity_resolver: IdentityResolver | None = None,
        queue_size: int = 256,
        max_attempts: int = 3,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        completed_limit: int = 2048,
    ) -> None:
        self.settings = settings
        self.send_text = send_text
        self.identity_resolver = identity_resolver or _default_identity_resolver
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max(1, queue_size))
        self.max_attempts = max(1, int(max_attempts))
        self.sleep = sleep
        self.completed_limit = max(1, int(completed_limit))
        self._completed: OrderedDict[str, None] = OrderedDict()
        self._progress: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    def _settings(self) -> dict[str, Any]:
        if self.settings is not None:
            return self.settings
        from config import SETTINGS

        return SETTINGS

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        if not task.done():
            try:
                await asyncio.wait_for(self.queue.join(), timeout=2.0)
            except asyncio.TimeoutError:
                log.warning("WeChat mode shutdown drain timed out")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None

    def offer(self, event: dict[str, Any]) -> bool:
        try:
            self.queue.put_nowait(copy.deepcopy(event))
            return True
        except asyncio.QueueFull:
            log.warning("WeChat mode queue is full; dropping mirrored event")
            return False

    async def _run(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self.process_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("WeChat mode event processing failed")
            finally:
                self.queue.task_done()

    async def _send_text(self, **kwargs: Any) -> Any:
        if self.send_text is not None:
            return await _maybe_await(self.send_text(**kwargs))
        from openclaw_weixin import send_text_message

        return await send_text_message(**kwargs)

    def _remember_completed(self, key: str) -> None:
        self._completed[key] = None
        self._completed.move_to_end(key)
        while len(self._completed) > self.completed_limit:
            self._completed.popitem(last=False)

    async def process_event(self, event: dict[str, Any]) -> None:
        route = extract_wechat_event_route(event)
        if route is None:
            return
        settings = self._settings()
        if not bool(settings.get("wechat_bridge_enabled", False)):
            return
        if str(settings.get("wechat_bridge_transport") or "").strip().lower() != "openclaw":
            return

        source_type, source_id, source_msg_id, sender = route
        modes = active_wechat_modes_for_route(settings, source_type, source_id)
        if not modes:
            return

        for mode in modes:
            account_id = str(mode.get("account_id") or "").strip()
            wechat_user_id = str(mode.get("wechat_user_id") or "").strip()
            mode_key = make_wechat_mode_key(account_id, wechat_user_id)
            delivery_key = f"{source_type}:{source_id}:{source_msg_id}:{mode_key}"
            if delivery_key in self._completed:
                continue

            binding = find_wechat_binding_for_sender(
                account_id,
                wechat_user_id,
                settings=settings,
            )
            if not binding:
                log.warning("WeChat mode delivery skipped because binding is missing")
                continue

            source_label, sender_name = await _maybe_await(
                self.identity_resolver(source_type, source_id, sender)
            )
            bubbles = await asyncio.to_thread(
                render_wechat_bubbles,
                event,
                source_label=source_label,
                sender_name=sender_name,
            )
            if not bubbles:
                self._remember_completed(delivery_key)
                continue

            start_index = self._progress.get(delivery_key, 0)
            failed = False
            for index in range(start_index, len(bubbles)):
                content = bubbles[index]
                delivered = False
                for attempt in range(self.max_attempts):
                    try:
                        result = await self._send_text(
                            to_user_id=wechat_user_id,
                            content=content,
                            account_id=account_id or None,
                            context_token=binding.get("context_token") or None,
                            openclaw_home=settings.get("wechat_bridge_openclaw_home") or None,
                        )
                        if isinstance(result, dict) and result.get("ok") is False:
                            raise RuntimeError(str(result.get("error") or "send failed"))
                        delivered = True
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if attempt + 1 >= self.max_attempts:
                            log.warning(
                                "WeChat mode bubble delivery failed after %d attempts: %s",
                                self.max_attempts,
                                type(exc).__name__,
                            )
                            break
                        await self.sleep(0.25 * (2 ** attempt))
                if not delivered:
                    failed = True
                    break
                self._progress[delivery_key] = index + 1

            if failed:
                continue
            self._progress.pop(delivery_key, None)
            self._remember_completed(delivery_key)


wechat_mode_dispatcher = WeChatModeDispatcher()
