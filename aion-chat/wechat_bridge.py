import inspect
import re
import secrets
import string
import time
from typing import Any, Callable


WECHAT_MESSAGE_PATTERN = re.compile(r"\[\s*微信消息\s*[：:]\s*([^\]]+?)\s*\]")
WECHAT_LAST_ROUTE_SETTINGS_KEY = "wechat_last_route"
WECHAT_BINDINGS_SETTINGS_KEY = "wechat_bridge_bindings"
WECHAT_PENDING_BINDINGS_SETTINGS_KEY = "wechat_bridge_pending_bindings"
WECHAT_TRANSPORT_SETTINGS_KEY = "wechat_bridge_transport"
WECHAT_LAST_SEND_SETTINGS_KEY = "wechat_bridge_last_send"
WECHAT_BINDING_TTL_SECONDS = 10 * 60
WECHAT_CONTEXT_STALE_SECONDS = 15 * 60


def _clean_text_after_command_removal(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_wechat_messages(text: str) -> tuple[str, list[str]]:
    messages: list[str] = []

    def _capture(match: re.Match) -> str:
        content = (match.group(1) or "").strip()
        if content:
            messages.append(content)
        return ""

    cleaned = WECHAT_MESSAGE_PATTERN.sub(_capture, text or "")
    return _clean_text_after_command_removal(cleaned), messages


def build_wechat_system_content(content: str) -> str:
    return f"本条为微信消息：{(content or '').strip()}"


def build_wechat_user_content(content: str) -> str:
    return f"本条消息来自于微信：{(content or '').strip()}"


def resolve_wechat_sender_label(source_type: str, sender: str) -> str:
    source_type = (source_type or "").strip().lower()
    sender = (sender or "").strip().lower()
    if source_type == "chatroom" and sender in ("aion", "connor"):
        try:
            from chatroom import get_chatroom_names

            _user_name, ai_name, connor_name = get_chatroom_names()
            return {"aion": ai_name, "connor": connor_name}.get(sender, "")
        except Exception:
            return {"aion": "AI", "connor": "第二AI"}.get(sender, "")
    return ""


def format_wechat_outbound_content(
    content: str,
    *,
    source_type: str,
    sender: str,
    sender_label: str | None = None,
) -> str:
    text = (content or "").strip()
    if (source_type or "").strip().lower() != "chatroom":
        return text
    label = (sender_label if sender_label is not None else resolve_wechat_sender_label(source_type, sender)).strip()
    if not text or not label:
        return text
    return f"{label}：{text}"


def build_wechat_route(
    *,
    source_type: str,
    source_id: str,
    sender: str = "",
    source_msg_id: str = "",
) -> dict[str, str]:
    return {
        "source_type": (source_type or "").strip(),
        "source_id": (source_id or "").strip(),
        "sender": (sender or "").strip(),
        "source_msg_id": (source_msg_id or "").strip(),
    }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def process_wechat_outbound_commands(
    text: str,
    *,
    source_type: str,
    source_id: str,
    sender: str,
    source_msg_id: str,
    save_system_message: Callable[[str], Any],
    send_wechat_message: Callable[..., Any] | None = None,
    record_route: Callable[[dict[str, str]], Any] | None = None,
) -> tuple[str, list[str]]:
    cleaned, messages = extract_wechat_messages(text)
    if not messages:
        return cleaned, messages

    route = build_wechat_route(
        source_type=source_type,
        source_id=source_id,
        sender=sender,
        source_msg_id=source_msg_id,
    )
    from config import SETTINGS
    from wechat_mode import active_wechat_modes_for_route

    if active_wechat_modes_for_route(
        SETTINGS,
        route["source_type"],
        route["source_id"],
    ):
        visible_parts = [cleaned, *messages]
        visible_text = "\n".join(part for part in visible_parts if part)
        return _clean_text_after_command_removal(visible_text), messages

    if record_route:
        await _maybe_await(record_route(route))

    for content in messages:
        outbound_content = format_wechat_outbound_content(
            content,
            source_type=route["source_type"],
            sender=route["sender"],
        )
        await _maybe_await(save_system_message(build_wechat_system_content(outbound_content)))
        if send_wechat_message:
            await _maybe_await(send_wechat_message(
                content=outbound_content,
                source_type=route["source_type"],
                source_id=route["source_id"],
                sender=route["sender"],
                source_msg_id=route["source_msg_id"],
            ))

    return cleaned, messages


def record_wechat_route(route: dict[str, str]) -> None:
    from config import SETTINGS, save_settings

    SETTINGS[WECHAT_LAST_ROUTE_SETTINGS_KEY] = dict(route or {})
    save_settings(SETTINGS)


def get_recorded_wechat_route() -> dict[str, str]:
    from config import SETTINGS

    route = SETTINGS.get(WECHAT_LAST_ROUTE_SETTINGS_KEY)
    return dict(route) if isinstance(route, dict) else {}


def _settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    if settings is not None:
        return settings
    from config import SETTINGS

    return SETTINGS


def _save_if_global(settings: dict[str, Any] | None = None) -> None:
    if settings is not None:
        return
    from config import SETTINGS, save_settings

    save_settings(SETTINGS)


def _now(now: float | None = None) -> float:
    return float(time.time() if now is None else now)


def _normalize_binding_source_type(source_type: str) -> str:
    value = (source_type or "").strip().lower()
    if value in ("private", "conversation", "conv"):
        return "aion_private"
    if value in ("room", "group"):
        return "chatroom"
    return value


def make_wechat_binding_key(source_type: str, source_id: str) -> str:
    return f"{_normalize_binding_source_type(source_type)}:{(source_id or '').strip()}"


def _get_bindings(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = settings.get(WECHAT_BINDINGS_SETTINGS_KEY)
    if isinstance(raw, dict):
        return raw
    settings[WECHAT_BINDINGS_SETTINGS_KEY] = {}
    return settings[WECHAT_BINDINGS_SETTINGS_KEY]


def _get_pending_bindings(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = settings.get(WECHAT_PENDING_BINDINGS_SETTINGS_KEY)
    if isinstance(raw, dict):
        return raw
    settings[WECHAT_PENDING_BINDINGS_SETTINGS_KEY] = {}
    return settings[WECHAT_PENDING_BINDINGS_SETTINGS_KEY]


def _new_binding_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def prune_expired_wechat_pending_bindings(
    *,
    settings: dict[str, Any] | None = None,
    now: float | None = None,
) -> None:
    store = _settings(settings)
    pending = _get_pending_bindings(store)
    current = _now(now)
    for code, item in list(pending.items()):
        try:
            expires_at = float(item.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at and expires_at < current:
            pending.pop(code, None)
    _save_if_global(settings)


def create_wechat_pending_binding(
    *,
    source_type: str,
    source_id: str,
    code: str | None = None,
    now: float | None = None,
    ttl_seconds: int = WECHAT_BINDING_TTL_SECONDS,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = _settings(settings)
    prune_expired_wechat_pending_bindings(settings=store, now=now)
    current = _now(now)
    pending = _get_pending_bindings(store)
    binding_code = (code or _new_binding_code()).strip().upper()
    item = {
        "code": binding_code,
        "source_type": _normalize_binding_source_type(source_type),
        "source_id": (source_id or "").strip(),
        "created_at": current,
        "expires_at": current + max(60, int(ttl_seconds or WECHAT_BINDING_TTL_SECONDS)),
    }
    pending[binding_code] = item
    _save_if_global(settings)
    return dict(item)


def create_wechat_binding(
    *,
    source_type: str,
    source_id: str,
    account_id: str,
    wechat_user_id: str,
    context_token: str = "",
    now: float | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = _settings(settings)
    normalized_source_type = _normalize_binding_source_type(source_type)
    key = make_wechat_binding_key(normalized_source_type, source_id)
    current = _now(now)
    binding = {
        "source_type": normalized_source_type,
        "source_id": (source_id or "").strip(),
        "account_id": (account_id or "").strip(),
        "wechat_user_id": (wechat_user_id or "").strip(),
        "context_token": (context_token or "").strip(),
        "bound_at": current,
        "last_seen_at": current,
    }
    bindings = _get_bindings(store)
    for old_key, old_binding in list(bindings.items()):
        if not isinstance(old_binding, dict):
            continue
        if old_key == key:
            continue
        if (
            old_binding.get("account_id") == binding["account_id"]
            and old_binding.get("wechat_user_id") == binding["wechat_user_id"]
        ):
            bindings.pop(old_key, None)
    bindings[key] = binding
    _save_if_global(settings)
    return dict(binding)


def update_wechat_binding_context(
    binding: dict[str, Any],
    *,
    context_token: str = "",
    now: float | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if context_token:
        binding["context_token"] = context_token
    binding["last_seen_at"] = _now(now)
    key = make_wechat_binding_key(binding.get("source_type", ""), binding.get("source_id", ""))
    _get_bindings(_settings(settings))[key] = binding
    _save_if_global(settings)
    return dict(binding)


def _context_age_seconds(binding: dict[str, Any], *, now: float | None = None) -> int | None:
    try:
        last_seen_at = float((binding or {}).get("last_seen_at") or 0)
    except (TypeError, ValueError):
        last_seen_at = 0
    if last_seen_at <= 0:
        return None
    return max(0, int(_now(now) - last_seen_at))


def _context_state(
    binding: dict[str, Any],
    *,
    now: float | None = None,
    stale_after_seconds: int | None = None,
) -> str:
    if not (binding or {}).get("context_token"):
        return "missing"
    age = _context_age_seconds(binding, now=now)
    if age is None:
        return "unknown"
    stale_after = int(stale_after_seconds or WECHAT_CONTEXT_STALE_SECONDS)
    return "stale" if age > stale_after else "fresh"


def find_wechat_binding_for_route(
    source_type: str,
    source_id: str,
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    binding = _get_bindings(_settings(settings)).get(make_wechat_binding_key(source_type, source_id))
    return dict(binding) if isinstance(binding, dict) else None


def find_wechat_binding_for_sender(
    account_id: str,
    wechat_user_id: str,
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    account_id = (account_id or "").strip()
    wechat_user_id = (wechat_user_id or "").strip()
    for binding in _get_bindings(_settings(settings)).values():
        if not isinstance(binding, dict):
            continue
        if binding.get("account_id") == account_id and binding.get("wechat_user_id") == wechat_user_id:
            return dict(binding)
    return None


def consume_wechat_binding_code(
    code: str,
    *,
    account_id: str,
    wechat_user_id: str,
    context_token: str = "",
    now: float | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    store = _settings(settings)
    prune_expired_wechat_pending_bindings(settings=store, now=now)
    binding_code = (code or "").strip().upper()
    pending = _get_pending_bindings(store)
    item = pending.get(binding_code)
    if not isinstance(item, dict):
        return None
    pending.pop(binding_code, None)
    binding = create_wechat_binding(
        source_type=item.get("source_type", ""),
        source_id=item.get("source_id", ""),
        account_id=account_id,
        wechat_user_id=wechat_user_id,
        context_token=context_token,
        now=now,
        settings=store,
    )
    _save_if_global(settings)
    return binding


def public_wechat_bindings(
    *,
    settings: dict[str, Any] | None = None,
    now: float | None = None,
    stale_after_seconds: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current = _now(now)
    stale_after = int(stale_after_seconds or WECHAT_CONTEXT_STALE_SECONDS)
    for binding in _get_bindings(_settings(settings)).values():
        if not isinstance(binding, dict):
            continue
        context_age = _context_age_seconds(binding, now=current)
        rows.append({
            "source_type": binding.get("source_type", ""),
            "source_id": binding.get("source_id", ""),
            "account_id": binding.get("account_id", ""),
            "wechat_user_id": binding.get("wechat_user_id", ""),
            "bound_at": binding.get("bound_at", 0),
            "last_seen_at": binding.get("last_seen_at", 0),
            "has_context_token": bool(binding.get("context_token")),
            "context_age_seconds": context_age,
            "context_state": _context_state(binding, now=current, stale_after_seconds=stale_after),
        })
    return rows


def record_wechat_send_attempt(
    *,
    route: dict[str, str],
    binding: dict[str, Any] | None,
    content: str,
    result: dict[str, Any],
    context_age_seconds: int | None = None,
    now: float | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = _settings(settings)
    response = result.get("response") if isinstance(result, dict) else None
    item = {
        "attempted_at": _now(now),
        "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
        "content_len": len(content or ""),
        "source_type": (route or {}).get("source_type", ""),
        "source_id": (route or {}).get("source_id", ""),
        "sender": (route or {}).get("sender", ""),
        "source_msg_id": (route or {}).get("source_msg_id", ""),
        "context_age_seconds": context_age_seconds,
    }
    if binding:
        item.update({
            "account_id": binding.get("account_id", ""),
            "wechat_user_id_tail": str(binding.get("wechat_user_id", ""))[-12:],
            "context_state": _context_state(binding, now=now),
        })
    if isinstance(result, dict):
        for key in ("skipped", "error", "status_code"):
            if key in result:
                item[key] = result[key]
        if isinstance(response, dict):
            item["response_ret"] = response.get("ret")
            item["response_errmsg"] = response.get("errmsg") or ""
    store[WECHAT_LAST_SEND_SETTINGS_KEY] = item
    _save_if_global(settings)
    return dict(item)


async def dispatch_wechat_message(
    *,
    content: str,
    source_type: str,
    source_id: str,
    sender: str,
    source_msg_id: str,
) -> dict[str, Any]:
    from config import SETTINGS

    route = build_wechat_route(
        source_type=source_type,
        source_id=source_id,
        sender=sender,
        source_msg_id=source_msg_id,
    )
    enabled = bool(SETTINGS.get("wechat_bridge_enabled", False))
    transport = str(SETTINGS.get(WECHAT_TRANSPORT_SETTINGS_KEY) or "webhook").strip().lower()
    if enabled and transport == "openclaw":
        binding = find_wechat_binding_for_route(source_type, source_id, settings=SETTINGS)
        if not binding:
            print(f"[WECHAT_BRIDGE] OpenClaw transport is enabled, but no binding exists for {source_type}:{source_id}.")
            result = {"ok": False, "skipped": "not_bound"}
            record_wechat_send_attempt(
                route=route,
                binding=None,
                content=content,
                result=result,
            )
            return result
        current = _now()
        context_age = _context_age_seconds(binding, now=current)
        stale_after = int(SETTINGS.get("wechat_bridge_context_stale_seconds") or WECHAT_CONTEXT_STALE_SECONDS)
        if _context_state(binding, now=current, stale_after_seconds=stale_after) == "stale":
            print(
                f"[WECHAT_BRIDGE] OpenClaw context token may be stale for {source_type}:{source_id}; "
                f"last_seen_age={context_age}s."
            )
        try:
            from openclaw_weixin import send_text_message

            result = await send_text_message(
                to_user_id=binding["wechat_user_id"],
                content=content,
                account_id=binding.get("account_id") or None,
                context_token=binding.get("context_token") or None,
                openclaw_home=SETTINGS.get("wechat_bridge_openclaw_home") or None,
            )
            record_wechat_send_attempt(
                route=route,
                binding=binding,
                content=content,
                result=result,
                context_age_seconds=context_age,
                now=current,
            )
            return result
        except Exception as exc:
            print(f"[WECHAT_BRIDGE] OpenClaw Weixin send failed: {exc}")
            result = {"ok": False, "error": str(exc)}
            record_wechat_send_attempt(
                route=route,
                binding=binding,
                content=content,
                result=result,
                context_age_seconds=context_age,
                now=current,
            )
            return result

    webhook_url = str(SETTINGS.get("wechat_bridge_webhook_url") or "").strip()
    if not enabled or not webhook_url:
        print("[WECHAT_BRIDGE] 已记录微信系统消息；未配置发送出口，跳过实际微信发送。")
        result = {"ok": False, "skipped": "not_configured"}
        record_wechat_send_attempt(
            route=route,
            binding=None,
            content=content,
            result=result,
        )
        return result

    payload = {
        "content": content,
        "source_type": source_type,
        "source_id": source_id,
        "sender": sender,
        "source_msg_id": source_msg_id,
    }
    headers = {"Content-Type": "application/json"}
    token = str(SETTINGS.get("wechat_bridge_webhook_token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(webhook_url, json=payload, headers=headers)
            resp.raise_for_status()
        result = {"ok": True, "status_code": resp.status_code}
        record_wechat_send_attempt(
            route=route,
            binding=None,
            content=content,
            result=result,
        )
        return result
    except Exception as exc:
        print(f"[WECHAT_BRIDGE] 微信发送失败: {exc}")
        result = {"ok": False, "error": str(exc)}
        record_wechat_send_attempt(
            route=route,
            binding=None,
            content=content,
            result=result,
        )
        return result
