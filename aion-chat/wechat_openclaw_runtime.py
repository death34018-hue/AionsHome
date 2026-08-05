from __future__ import annotations

import asyncio
import inspect
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from wechat_bridge import (
    create_wechat_binding,
    consume_wechat_binding_code,
    find_wechat_binding_for_sender,
    update_wechat_binding_context,
)


BindingInboundHandler = Callable[..., Awaitable[Any]]
SendTextHandler = Callable[..., Any]
DefaultRouteResolver = Callable[[], Any]
PrivateRouteResolver = Callable[[], Any]
DEFAULT_BINDING_ALIASES = {"aionshome", "default", "auto", "main", "current", "默认", "主聊天", "当前"}


def parse_binding_command(text: str) -> str:
    match = re.match(r"^\s*(?:绑定|bind)\s*[:：]?\s*(\S+)\s*$", text or "", re.IGNORECASE)
    return match.group(1).strip() if match else ""


def is_default_binding_alias(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value or "").strip().lower()
    return normalized in DEFAULT_BINDING_ALIASES


def choose_latest_binding_route(
    *,
    private_id: str | None,
    private_updated_at: float | None,
    room_id: str | None,
    room_updated_at: float | None,
) -> dict[str, str] | None:
    candidates: list[tuple[float, str, str]] = []
    if private_id:
        candidates.append((float(private_updated_at or 0), "aion_private", private_id))
    if room_id:
        candidates.append((float(room_updated_at or 0), "chatroom", room_id))
    if not candidates:
        return None
    _, source_type, source_id = max(candidates, key=lambda item: item[0])
    return {"source_type": source_type, "source_id": source_id}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _default_inbound_handler(**kwargs: Any) -> Any:
    from routes.wechat import WeChatInbound, receive_wechat_message

    body = WeChatInbound(
        content=kwargs["content"],
        source_type=kwargs["source_type"],
        source_id=kwargs["source_id"],
        auto_reply=bool(kwargs.get("auto_reply", True)),
        mark_channel=bool(kwargs.get("mark_channel", True)),
    )
    return await receive_wechat_message(body, authorization=None)


async def _default_binding_route() -> dict[str, str]:
    from config import DEFAULT_MODEL
    from database import get_db

    async with get_db() as db:
        private_cur = await db.execute("SELECT id, updated_at FROM conversations ORDER BY updated_at DESC LIMIT 1")
        private_row = await private_cur.fetchone()
        room_cur = await db.execute("SELECT id, updated_at FROM chatroom_rooms ORDER BY updated_at DESC LIMIT 1")
        room_row = await room_cur.fetchone()

        route = choose_latest_binding_route(
            private_id=private_row[0] if private_row else None,
            private_updated_at=private_row[1] if private_row else None,
            room_id=room_row[0] if room_row else None,
            room_updated_at=room_row[1] if room_row else None,
        )
        if route:
            return route

        now = time.time()
        conv_id = f"conv_{time.time_ns()}"
        await db.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            (conv_id, "WeChat", DEFAULT_MODEL, now, now),
        )
        await db.commit()
        return {"source_type": "aion_private", "source_id": conv_id}


async def _default_private_route() -> dict[str, str] | None:
    from database import get_db

    async with get_db() as db:
        cur = await db.execute(
            "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {"source_type": "aion_private", "source_id": str(row[0])}


class OpenClawWeixinBridgeRuntime:
    def __init__(
        self,
        *,
        settings: dict[str, Any] | None = None,
        save_settings: Callable[[dict[str, Any]], Any] | None = None,
        inbound_handler: BindingInboundHandler | None = None,
        send_text: SendTextHandler | None = None,
        default_route_resolver: DefaultRouteResolver | None = None,
        private_route_resolver: PrivateRouteResolver | None = None,
        now: Callable[[], float] | None = None,
        openclaw_home: str | Path | None = None,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        if settings is None:
            from config import SETTINGS

            settings = SETTINGS
        if save_settings is None:
            from config import save_settings as save_settings_func

            save_settings = save_settings_func
        self.settings = settings
        self.save_settings = save_settings
        self.inbound_handler = inbound_handler or _default_inbound_handler
        self.send_text = send_text
        self.default_route_resolver = default_route_resolver or _default_binding_route
        self.private_route_resolver = private_route_resolver or _default_private_route
        self.now = now or time.time
        self.openclaw_home = openclaw_home
        self.poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def enabled(self) -> bool:
        return (
            bool(self.settings.get("wechat_bridge_enabled", False))
            and str(self.settings.get("wechat_bridge_transport") or "").strip().lower() == "openclaw"
        )

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopping.set()
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[WECHAT_OPENCLAW] poll failed: {exc}")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> None:
        if not self.enabled():
            return
        from openclaw_weixin import (
            extract_text_from_message,
            get_updates,
            load_accounts,
            load_sync_buf,
            save_sync_buf,
        )

        home = self.settings.get("wechat_bridge_openclaw_home") or self.openclaw_home
        for account in load_accounts(home):
            cursor = load_sync_buf(account.account_id, home)
            response = await get_updates(account, cursor)
            next_cursor = str(response.get("get_updates_buf") or "")
            if next_cursor:
                save_sync_buf(account.account_id, next_cursor, home)
            for message in response.get("msgs") or []:
                if not isinstance(message, dict):
                    continue
                if message.get("message_type") == 2:
                    continue
                text = extract_text_from_message(message)
                if not text:
                    continue
                await self.handle_text_message(
                    account_id=account.account_id,
                    wechat_user_id=str(message.get("from_user_id") or ""),
                    context_token=str(message.get("context_token") or ""),
                    text=text,
                )

    async def _send_text(self, *, account_id: str, wechat_user_id: str, context_token: str, content: str) -> Any:
        if self.send_text:
            return await _maybe_await(self.send_text(
                account_id=account_id,
                wechat_user_id=wechat_user_id,
                context_token=context_token,
                content=content,
            ))
        from openclaw_weixin import send_text_message

        return await send_text_message(
            to_user_id=wechat_user_id,
            content=content,
            account_id=account_id,
            context_token=context_token,
            openclaw_home=self.settings.get("wechat_bridge_openclaw_home") or self.openclaw_home,
        )

    async def handle_text_message(
        self,
        *,
        account_id: str,
        wechat_user_id: str,
        context_token: str,
        text: str,
    ) -> bool:
        code = parse_binding_command(text)
        if code:
            binding = consume_wechat_binding_code(
                code,
                account_id=account_id,
                wechat_user_id=wechat_user_id,
                context_token=context_token,
                now=self.now(),
                settings=self.settings,
            )
            if binding:
                await _maybe_await(self.save_settings(self.settings))
                await self._send_text(
                    account_id=account_id,
                    wechat_user_id=wechat_user_id,
                    context_token=context_token,
                    content="AionsHome WeChat bridge bound.",
                )
                return True
            if is_default_binding_alias(code):
                route = await _maybe_await(self.default_route_resolver())
                source_type = str((route or {}).get("source_type") or "").strip()
                source_id = str((route or {}).get("source_id") or "").strip()
                if source_type and source_id:
                    create_wechat_binding(
                        source_type=source_type,
                        source_id=source_id,
                        account_id=account_id,
                        wechat_user_id=wechat_user_id,
                        context_token=context_token,
                        now=self.now(),
                        settings=self.settings,
                    )
                    await _maybe_await(self.save_settings(self.settings))
                    await self._send_text(
                        account_id=account_id,
                        wechat_user_id=wechat_user_id,
                        context_token=context_token,
                        content="AionsHome WeChat bridge bound to the default chat.",
                    )
                    return True

        binding = find_wechat_binding_for_sender(account_id, wechat_user_id, settings=self.settings)
        if not binding:
            await self._send_text(
                account_id=account_id,
                wechat_user_id=wechat_user_id,
                context_token=context_token,
                content="This WeChat account is not bound. Create a binding code in AionsHome, then send: bind <code>.",
            )
            return False

        from wechat_mode import (
            find_wechat_mode_for_sender,
            parse_wechat_mode_command,
            set_wechat_mode,
        )

        updated = update_wechat_binding_context(
            binding,
            context_token=context_token,
            now=self.now(),
            settings=self.settings,
        )
        mode_command = parse_wechat_mode_command(text)
        if mode_command:
            outbound_routes = [{
                "source_type": updated["source_type"],
                "source_id": updated["source_id"],
            }]
            if mode_command == "enable" and updated["source_type"] == "chatroom":
                private_route = await _maybe_await(self.private_route_resolver())
                if isinstance(private_route, dict):
                    outbound_routes.append(private_route)
            existing_mode = find_wechat_mode_for_sender(
                self.settings, account_id, wechat_user_id
            )
            if mode_command == "disable" and existing_mode:
                outbound_routes = list(existing_mode.get("outbound_routes") or outbound_routes)
            set_wechat_mode(
                self.settings,
                account_id=account_id,
                wechat_user_id=wechat_user_id,
                inbound_route={
                    "source_type": updated["source_type"],
                    "source_id": updated["source_id"],
                },
                outbound_routes=outbound_routes,
                enabled=mode_command == "enable",
                now=self.now(),
            )
            await _maybe_await(self.save_settings(self.settings))
            await self._send_text(
                account_id=account_id,
                wechat_user_id=wechat_user_id,
                context_token=context_token,
                content=(
                    "微信模式已开启。"
                    if mode_command == "enable"
                    else "微信模式已关闭。"
                ),
            )
            return True

        await _maybe_await(self.save_settings(self.settings))
        mode = find_wechat_mode_for_sender(self.settings, account_id, wechat_user_id)
        await self.inbound_handler(
            content=text,
            source_type=updated["source_type"],
            source_id=updated["source_id"],
            auto_reply=True,
            mark_channel=not bool(mode and mode.get("enabled")),
        )
        return True


openclaw_weixin_runtime = OpenClawWeixinBridgeRuntime()
