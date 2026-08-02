"""
WebSocket 连接管理器
"""

import json, logging, time
from fastapi import WebSocket
from config import load_worldbook

log = logging.getLogger("ws")


def _clean_private_ai_name(ai_name: str | None) -> str:
    return (ai_name or "").strip() or "AI"


def _current_private_ai_name() -> str:
    try:
        wb = load_worldbook()
    except Exception:
        return "AI"
    return _clean_private_ai_name(wb.get("ai_name"))


def _with_private_notification_sender(event: dict) -> dict:
    if event.get("type") != "msg_created" or not isinstance(event.get("data"), dict):
        return dict(event)

    data = dict(event["data"])
    enriched = dict(event)
    enriched["data"] = data
    if data.get("role") == "assistant":
        sender_name = _current_private_ai_name()
        data["sender"] = sender_name
        data["sender_name"] = sender_name
    return enriched


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []
        self.tts_clients: dict[WebSocket, dict] = {}  # {ws: {"enabled": bool, "voice": str, "can_play": bool, "active_at": float}}
        self._tts_fallback: dict = {}  # {"enabled": bool, "voice": str} — 来自 HTTP 请求的备用 TTS 状态
        self.client_ids: dict[WebSocket, str] = {}     # {ws: client_id} — 客户端唯一标识
        self._last_sender_client_id: str | None = None  # 最后发消息的客户端 ID
        self.pet_clients: dict[WebSocket, bool] = {}    # {ws: enabled} — 在线桌宠客户端
        # ── 各侧用户最后活跃窗口追踪 ──
        # "private" = Aion 私聊, "chatroom:<room_id>" = 群聊/Connor 私聊
        self._aion_last_active: str = "private"        # Aion 侧：用户最后在 Aion 私聊 or 群聊
        self._connor_last_active: str | None = None    # Connor 侧：用户最后在 Connor 私聊 or 群聊的 room_id

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        log.info("WS connected, total=%d", len(self.active))

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        self.tts_clients.pop(ws, None)
        self.client_ids.pop(ws, None)
        self.pet_clients.pop(ws, None)
        log.info("WS disconnected, total=%d", len(self.active))

    def register_client_id(self, ws: WebSocket, client_id: str):
        self.client_ids[ws] = client_id

    def set_last_sender(self, client_id: str):
        self._last_sender_client_id = client_id

    def set_aion_last_active(self, target: str):
        """设置 Aion 侧用户最后活跃窗口。target: 'private' 或 'chatroom:<room_id>'"""
        self._aion_last_active = target

    def set_connor_last_active(self, room_id: str):
        """设置 Connor 侧用户最后活跃窗口（群聊或 Connor 私聊的 room_id）"""
        self._connor_last_active = room_id

    def get_aion_last_active(self) -> str:
        return self._aion_last_active

    def get_connor_last_active(self) -> str | None:
        return self._connor_last_active

    def set_pet_state(self, ws: WebSocket, enabled: bool):
        if enabled:
            self.pet_clients[ws] = True
        else:
            self.pet_clients.pop(ws, None)

    def has_active_pet(self) -> bool:
        return any(self.pet_clients.values())

    async def send_to_client(self, client_id: str, data: dict):
        """定向推送消息到指定 client_id 的客户端"""
        msg = json.dumps(data, ensure_ascii=False)
        for ws, cid in list(self.client_ids.items()):
            if cid == client_id:
                try:
                    await ws.send_text(msg)
                except Exception as e:
                    log.warning("WS send_to_client failed: %s", e)

    async def send_to_last_sender(self, data: dict):
        """推送消息到最后发消息的客户端"""
        if self._last_sender_client_id:
            await self.send_to_client(self._last_sender_client_id, data)

    def set_tts_state(
        self,
        ws: WebSocket,
        enabled: bool,
        voice: str = "",
        *,
        can_play: bool = True,
        active_at: float | None = None,
    ):
        if enabled and voice:
            self.tts_clients[ws] = {
                "enabled": True,
                "voice": voice,
                "can_play": bool(can_play),
                "active_at": active_at if active_at is not None else time.time(),
            }
        else:
            self.tts_clients.pop(ws, None)

    def set_tts_fallback(self, enabled: bool, voice: str = ""):
        """从 HTTP 请求更新备用 TTS 状态（当 WS tts_state 未送达时的保底）"""
        if enabled and voice:
            self._tts_fallback = {"enabled": True, "voice": voice}
        else:
            self._tts_fallback = {}

    def any_tts_enabled(self) -> bool:
        if any(c.get("enabled") and c.get("can_play", True) for c in self.tts_clients.values()):
            return True
        return bool(self._tts_fallback.get("enabled"))

    def get_tts_voice(self) -> str | None:
        for _, c in self._sorted_tts_clients():
            if c.get("enabled") and c.get("can_play", True):
                return c.get("voice")
        if self._tts_fallback.get("enabled"):
            return self._tts_fallback.get("voice")
        return None

    def _sorted_tts_clients(self) -> list[tuple[WebSocket, dict]]:
        return sorted(
            list(self.tts_clients.items()),
            key=lambda item: float(item[1].get("active_at") or 0),
            reverse=True,
        )

    def _select_tts_client(self) -> tuple[WebSocket, dict] | None:
        for ws, state in self._sorted_tts_clients():
            if ws in self.active and state.get("enabled") and state.get("can_play", True):
                return ws, state
        return None

    async def send_tts_event(self, data: dict):
        for ws, state in self._sorted_tts_clients():
            if ws not in self.active or not state.get("enabled") or not state.get("can_play", True):
                continue
            payload = json.loads(json.dumps(data, ensure_ascii=False))
            if isinstance(payload.get("data"), dict):
                payload["data"]["target_client_id"] = self.client_ids.get(ws, "")
            try:
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
                return
            except Exception as e:
                log.warning("WS send_tts_event failed: %s", e)
                if ws in self.active:
                    self.active.remove(ws)
                self.tts_clients.pop(ws, None)
                self.client_ids.pop(ws, None)
        log.debug("TTS event dropped because no playable client is active: %s", data.get("type"))

    async def broadcast(self, data: dict, exclude: WebSocket = None):
        payload = _with_private_notification_sender(data)
        msg = json.dumps(payload, ensure_ascii=False)
        msg_type = payload.get("type", "unknown")
        targets = [ws for ws in self.active.copy() if ws is not exclude]
        sent = 0
        failed = 0
        for ws in targets:
            try:
                await ws.send_text(msg)
                sent += 1
            except Exception as e:
                log.warning("WS send failed: %s", e)
                if ws in self.active:
                    self.active.remove(ws)
                failed += 1
        log.info("broadcast type=%s sent=%d failed=%d total_clients=%d",
                 msg_type, sent, failed, len(self.active))


manager = ConnectionManager()
