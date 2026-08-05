from __future__ import annotations

import copy
import json
import re
import time
from typing import Any


WECHAT_MODE_SESSIONS_KEY = "wechat_mode_sessions"
WECHAT_MODE_ENABLE_TEXT = "[微信模式开启]"
WECHAT_MODE_DISABLE_TEXT = "[微信模式关闭]"
_META_TAG_PATTERN = re.compile(r"\s*<meta\b[^>]*>.*?</meta\s*>", re.DOTALL | re.IGNORECASE)
_INNER_MONOLOGUE_PATTERN = re.compile(r"\[心里嘀咕[：:]\s*([^\]]+?)\]")


def parse_wechat_mode_command(text: str) -> str:
    value = (text or "").strip().translate(str.maketrans({"［": "[", "］": "]"}))
    if value == WECHAT_MODE_ENABLE_TEXT:
        return "enable"
    if value == WECHAT_MODE_DISABLE_TEXT:
        return "disable"
    return ""


def _normalize_source_type(source_type: str) -> str:
    value = (source_type or "").strip().lower()
    if value in ("private", "conversation", "conv"):
        return "aion_private"
    if value in ("room", "group"):
        return "chatroom"
    return value


def _normalize_route(route: dict[str, Any] | None) -> dict[str, str]:
    value = route if isinstance(route, dict) else {}
    return {
        "source_type": _normalize_source_type(str(value.get("source_type") or "")),
        "source_id": str(value.get("source_id") or "").strip(),
    }


def _valid_route(route: dict[str, str]) -> bool:
    return bool(route.get("source_type") and route.get("source_id"))


def _mode_store(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = settings.get(WECHAT_MODE_SESSIONS_KEY)
    if isinstance(raw, dict):
        return raw
    settings[WECHAT_MODE_SESSIONS_KEY] = {}
    return settings[WECHAT_MODE_SESSIONS_KEY]


def make_wechat_mode_key(account_id: str, wechat_user_id: str) -> str:
    return json.dumps(
        [(account_id or "").strip(), (wechat_user_id or "").strip()],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def set_wechat_mode(
    settings: dict[str, Any],
    *,
    account_id: str,
    wechat_user_id: str,
    inbound_route: dict[str, Any],
    outbound_routes: list[dict[str, Any]],
    enabled: bool,
    now: float | None = None,
) -> dict[str, Any]:
    current = float(time.time() if now is None else now)
    account = (account_id or "").strip()
    peer = (wechat_user_id or "").strip()
    key = make_wechat_mode_key(account, peer)
    store = _mode_store(settings)
    previous = store.get(key) if isinstance(store.get(key), dict) else {}

    normalized_inbound = _normalize_route(inbound_route)
    normalized_outbound: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in outbound_routes or []:
        route = _normalize_route(item)
        route_key = (route["source_type"], route["source_id"])
        if not _valid_route(route) or route_key in seen:
            continue
        seen.add(route_key)
        normalized_outbound.append(route)

    mode = {
        "enabled": bool(enabled),
        "account_id": account,
        "wechat_user_id": peer,
        "inbound_route": normalized_inbound,
        "outbound_routes": normalized_outbound,
        "enabled_at": (
            float(previous.get("enabled_at") or current)
            if enabled and previous.get("enabled")
            else (current if enabled else float(previous.get("enabled_at") or 0))
        ),
        "updated_at": current,
    }
    store[key] = mode
    return copy.deepcopy(mode)


def find_wechat_mode_for_sender(
    settings: dict[str, Any],
    account_id: str,
    wechat_user_id: str,
) -> dict[str, Any] | None:
    mode = _mode_store(settings).get(make_wechat_mode_key(account_id, wechat_user_id))
    return copy.deepcopy(mode) if isinstance(mode, dict) else None


def active_wechat_modes_for_route(
    settings: dict[str, Any],
    source_type: str,
    source_id: str,
) -> list[dict[str, Any]]:
    wanted = _normalize_route({"source_type": source_type, "source_id": source_id})
    matches: list[dict[str, Any]] = []
    for mode in _mode_store(settings).values():
        if not isinstance(mode, dict) or not mode.get("enabled"):
            continue
        routes = mode.get("outbound_routes")
        if not isinstance(routes, list):
            continue
        if any(_normalize_route(route) == wanted for route in routes if isinstance(route, dict)):
            matches.append(copy.deepcopy(mode))
    return matches


def public_wechat_modes(settings: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for mode in _mode_store(settings).values():
        if not isinstance(mode, dict):
            continue
        rows.append({
            "enabled": bool(mode.get("enabled")),
            "account_id": str(mode.get("account_id") or ""),
            "wechat_user_id_tail": str(mode.get("wechat_user_id") or "")[-12:],
            "inbound_route": _normalize_route(mode.get("inbound_route")),
            "outbound_routes": [
                _normalize_route(route)
                for route in (mode.get("outbound_routes") or [])
                if isinstance(route, dict) and _valid_route(_normalize_route(route))
            ],
            "enabled_at": float(mode.get("enabled_at") or 0),
            "updated_at": float(mode.get("updated_at") or 0),
        })
    rows.sort(key=lambda item: item["updated_at"], reverse=True)
    return rows


def _clean_final_text(text: str) -> str:
    cleaned = _META_TAG_PATTERN.sub("", text or "")
    from context_builder import strip_tool_commands

    cleaned = strip_tool_commands(cleaned)
    return cleaned.strip()


def _render_visible_text(text: str) -> str:
    def replace_inner_monologue(match: re.Match[str]) -> str:
        monologue = (match.group(1) or "").strip()
        return f"💭心里嘀咕：{monologue}" if monologue else ""

    return _INNER_MONOLOGUE_PATTERN.sub(replace_inner_monologue, text or "").strip()


def _attachment_bubbles(attachments: list[dict[str, Any]]) -> list[str]:
    bubbles: list[str] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        kind = str(attachment.get("type") or "").strip().lower()
        if kind == "music":
            name = str(attachment.get("name") or "歌曲").strip()
            artist = str(attachment.get("artist") or "").strip()
            bubbles.append(f"🎵 《{name}》" + (f"— {artist}" if artist else ""))
        elif kind in ("image", "generated_image", "selfie"):
            bubbles.append("🖼️ 图片消息（请在 AionsHome 查看）")
        elif kind in ("voice", "audio"):
            bubbles.append("🎤 语音消息（请在 AionsHome 查看）")
        elif kind in ("video", "video_clip"):
            bubbles.append("🎬 视频消息（请在 AionsHome 查看）")
        elif kind == "file":
            name = str(attachment.get("name") or attachment.get("file_name") or "").strip()
            bubbles.append("📎 文件" + (f"：{name}" if name else ""))
    return bubbles


def _split_long_bubble(text: str, max_chars: int) -> list[str]:
    value = (text or "").strip()
    limit = max(100, int(max_chars or 1200))
    if len(value) <= limit:
        return [value] if value else []

    parts: list[str] = []
    remaining = value
    while len(remaining) > limit:
        window = remaining[:limit + 1]
        cut = max(
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind("；"),
        )
        if cut < limit // 3:
            cut = limit
        elif window[cut:cut + 1] != "\n":
            cut += 1
        part = remaining[:cut].strip()
        if part:
            parts.append(part)
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def _cap_bubbles(bubbles: list[str], max_bubbles: int) -> list[str]:
    limit = max(1, int(max_bubbles or 12))
    if len(bubbles) <= limit:
        return bubbles
    head = bubbles[:limit - 1]
    tail = "\n".join(item for item in bubbles[limit - 1:] if item)
    return [*head, tail] if tail else head


def render_wechat_bubbles(
    event: dict[str, Any],
    *,
    source_label: str,
    sender_name: str,
    max_chars: int = 1200,
    max_bubbles: int = 12,
) -> list[str]:
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return []

    cleaned = _clean_final_text(str(data.get("content") or ""))
    visible_text = _render_visible_text(cleaned)
    bubbles = [visible_text] if visible_text else []
    raw_attachments = data.get("attachments")
    if isinstance(raw_attachments, str):
        try:
            raw_attachments = json.loads(raw_attachments)
        except Exception:
            raw_attachments = []

    for attachment in _attachment_bubbles(
        raw_attachments if isinstance(raw_attachments, list) else []
    ):
        bubbles.extend(_split_long_bubble(attachment, max_chars))
    bubbles = _cap_bubbles([item for item in bubbles if item.strip()], max_bubbles)
    if not bubbles:
        return []

    sender = (sender_name or "").strip() or "AI"
    return [f"{sender}：{bubble}" for bubble in bubbles]
