from __future__ import annotations

import base64
import io
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_PLUGIN_VERSION = "2.4.6"
DEFAULT_ILINK_APP_ID = "bot"
DEFAULT_BOT_AGENT = "AionsHome/1.0"

MESSAGE_ITEM_TEXT = 1
MESSAGE_ITEM_IMAGE = 2
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2


@dataclass
class OpenClawWeixinAccount:
    account_id: str
    user_id: str
    base_url: str
    token: str
    raw: dict[str, Any]


def resolve_openclaw_home(openclaw_home: str | Path | None = None) -> Path:
    if openclaw_home is not None:
        return Path(openclaw_home).expanduser()
    env_home = os.environ.get("OPENCLAW_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".openclaw"


def _accounts_dir(openclaw_home: str | Path | None = None) -> Path:
    return resolve_openclaw_home(openclaw_home) / "openclaw-weixin" / "accounts"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_accounts(openclaw_home: str | Path | None = None) -> list[OpenClawWeixinAccount]:
    accounts_dir = _accounts_dir(openclaw_home)
    if not accounts_dir.exists():
        return []

    accounts: list[OpenClawWeixinAccount] = []
    for path in sorted(accounts_dir.glob("*.json")):
        name = path.name
        if name == "accounts.json" or name.endswith(".context-tokens.json") or name.endswith(".sync.json"):
            continue
        data = _read_json(path, {})
        if not isinstance(data, dict):
            continue
        account_id = str(data.get("accountId") or path.stem).strip()
        token = str(data.get("token") or data.get("botToken") or "").strip()
        if not account_id or not token:
            continue
        accounts.append(OpenClawWeixinAccount(
            account_id=account_id,
            user_id=str(data.get("userId") or data.get("user_id") or "").strip(),
            base_url=str(data.get("baseUrl") or data.get("base_url") or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
            token=token,
            raw=data,
        ))
    return accounts


def load_context_tokens(account_id: str, openclaw_home: str | Path | None = None) -> dict[str, str]:
    path = _accounts_dir(openclaw_home) / f"{account_id}.context-tokens.json"
    data = _read_json(path, {})
    if not isinstance(data, dict):
        return {}
    return {
        str(user_id): str(token)
        for user_id, token in data.items()
        if isinstance(user_id, str) and isinstance(token, str) and user_id and token
    }


def load_sync_buf(account_id: str, openclaw_home: str | Path | None = None) -> str:
    path = _accounts_dir(openclaw_home) / f"{account_id}.sync.json"
    data = _read_json(path, {})
    if not isinstance(data, dict):
        return ""
    return str(data.get("get_updates_buf") or "")


def save_sync_buf(account_id: str, get_updates_buf: str, openclaw_home: str | Path | None = None) -> None:
    path = _accounts_dir(openclaw_home) / f"{account_id}.sync.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"get_updates_buf": get_updates_buf}, ensure_ascii=False), encoding="utf-8")


def select_account_for_recipient(
    to_user_id: str,
    *,
    account_id: str | None = None,
    openclaw_home: str | Path | None = None,
) -> tuple[OpenClawWeixinAccount, str]:
    accounts = load_accounts(openclaw_home)
    if account_id:
        accounts = [account for account in accounts if account.account_id == account_id]
    if not accounts:
        raise ValueError("No OpenClaw Weixin account is available. Run OpenClaw Weixin QR login first.")

    for account in accounts:
        context_tokens = load_context_tokens(account.account_id, openclaw_home)
        if to_user_id in context_tokens:
            return account, context_tokens[to_user_id]

    if len(accounts) == 1:
        return accounts[0], ""
    raise ValueError("Multiple OpenClaw Weixin accounts exist; account_id is required.")


def build_client_version(version: str) -> int:
    parts: list[int] = []
    for raw in str(version or "").split(".")[:3]:
        try:
            parts.append(int(raw))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _random_wechat_uin() -> str:
    value = str(secrets.randbits(32)).encode("utf-8")
    return base64.b64encode(value).decode("ascii")


def build_headers(
    token: str,
    *,
    app_id: str = DEFAULT_ILINK_APP_ID,
    plugin_version: str = DEFAULT_PLUGIN_VERSION,
) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token.strip()}",
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": app_id,
        "iLink-App-ClientVersion": str(build_client_version(plugin_version)),
    }


def build_base_info(plugin_version: str = DEFAULT_PLUGIN_VERSION) -> dict[str, str]:
    return {
        "channel_version": plugin_version,
        "bot_agent": DEFAULT_BOT_AGENT,
    }


def build_text_message_body(
    to_user_id: str,
    text: str,
    *,
    context_token: str | None = None,
    client_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": client_id or f"aionshome-weixin-{uuid.uuid4().hex}",
        "message_type": MESSAGE_TYPE_BOT,
        "message_state": MESSAGE_STATE_FINISH,
    }
    if text:
        msg["item_list"] = [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": text}}]
    if context_token:
        msg["context_token"] = context_token
    if run_id:
        msg["run_id"] = run_id
    return {"msg": msg}


def extract_text_from_message(message: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in message.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == MESSAGE_ITEM_TEXT:
            text = ((item.get("text_item") or {}).get("text") or "")
            if text:
                parts.append(str(text))
            continue
        voice_text = ((item.get("voice_item") or {}).get("text") or "")
        if voice_text:
            parts.append(str(voice_text))
    return "\n".join(part.strip() for part in parts if part and part.strip())


def decrypt_image_bytes(data: bytes, image_item: dict[str, Any]) -> bytes:
    """Decode the AES-128-ECB envelope used by inbound Weixin CDN images."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    media = image_item.get("media") or {}
    hex_key = image_item.get("aeskey") or ""
    if hex_key:
        key = bytes.fromhex(hex_key)
    elif media.get("aes_key"):
        key = base64.b64decode(media["aes_key"], validate=True)
        if len(key) == 32:
            key = bytes.fromhex(key.decode("ascii"))
    else:
        return data
    if len(key) != 16:
        raise ValueError("Invalid Weixin image AES key")
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


async def download_image_item(image_item: dict[str, Any]) -> str:
    """Download a Weixin image into AionsHome uploads and return its attachment URL."""
    import httpx
    from PIL import Image
    from config import UPLOADS_DIR

    media = image_item.get("media") or image_item.get("thumb_media") or {}
    url = str(media.get("full_url") or "").strip()
    if not url and media.get("encrypt_query_param"):
        url = "https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=" + quote(
            str(media["encrypt_query_param"]), safe=""
        )
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "cdn.weixin.qq.com" or host.endswith(".cdn.weixin.qq.com")):
        raise ValueError("Unsupported Weixin image CDN URL")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.get(url)
        response.raise_for_status()
        if len(response.content) > 20 * 1024 * 1024:
            raise ValueError("Weixin image is too large")
        content = decrypt_image_bytes(response.content, image_item)
    with Image.open(io.BytesIO(content)) as image:
        image.verify()
        image_format = image.format
    suffix = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp"}.get(image_format)
    if not suffix:
        raise ValueError("Unsupported Weixin image format")
    name = f"wechat_{uuid.uuid4().hex}{suffix}"
    (UPLOADS_DIR / name).write_bytes(content)
    return f"/uploads/{name}"


async def api_post(
    account: OpenClawWeixinAccount,
    endpoint: str,
    body: dict[str, Any],
    *,
    timeout_seconds: float = 15,
) -> dict[str, Any]:
    import httpx

    payload = dict(body)
    payload["base_info"] = build_base_info()
    url = f"{account.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(url, json=payload, headers=build_headers(account.token))
        response.raise_for_status()
    if not response.text.strip():
        return {}
    data = response.json()
    if isinstance(data, dict) and data.get("ret") not in (None, 0):
        raise RuntimeError(f"{endpoint} ret={data.get('ret')} errmsg={data.get('errmsg') or ''}")
    return data if isinstance(data, dict) else {}


async def get_updates(
    account: OpenClawWeixinAccount,
    get_updates_buf: str = "",
    *,
    timeout_seconds: float = 40,
) -> dict[str, Any]:
    try:
        return await api_post(
            account,
            "ilink/bot/getupdates",
            {"get_updates_buf": get_updates_buf or ""},
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}
        raise


async def send_text_message(
    *,
    to_user_id: str,
    content: str,
    account_id: str | None = None,
    context_token: str | None = None,
    openclaw_home: str | Path | None = None,
    timeout_seconds: float = 15,
) -> dict[str, Any]:
    account, fallback_context_token = select_account_for_recipient(
        to_user_id,
        account_id=account_id,
        openclaw_home=openclaw_home,
    )
    body = build_text_message_body(
        to_user_id,
        content,
        context_token=context_token or fallback_context_token,
    )
    data = await api_post(account, "ilink/bot/sendmessage", body, timeout_seconds=timeout_seconds)
    return {
        "ok": True,
        "account_id": account.account_id,
        "wechat_user_id": to_user_id,
        "response": data,
    }


def summarize_accounts(openclaw_home: str | Path | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for account in load_accounts(openclaw_home):
        tokens = load_context_tokens(account.account_id, openclaw_home)
        rows.append({
            "account_id": account.account_id,
            "user_id": account.user_id,
            "base_url": account.base_url,
            "has_token": bool(account.token),
            "known_contacts": len(tokens),
        })
    return rows
