"""
WebSocket 连接管理器
"""

import json, logging
from fastapi import WebSocket

log = logging.getLogger("ws")


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []
        self.tts_clients: dict[WebSocket, dict] = {}  # {ws: {"enabled": bool, "voice": str}}
        self._tts_fallback: dict = {}  # {"enabled": bool, "voice": str} — 来自 HTTP 请求的备用 TTS 状态
        self.client_ids: dict[WebSocket, str] = {}     # {ws: client_id} — 客户端唯一标识
        self._last_sender_client_id: str | None = None  # 最后发消息的客户端 ID

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        log.info("WS connected, total=%d", len(self.active))

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        self.tts_clients.pop(ws, None)
        self.client_ids.pop(ws, None)
        log.info("WS disconnected, total=%d", len(self.active))

    def register_client_id(self, ws: WebSocket, client_id: str):
        self.client_ids[ws] = client_id

    def set_last_sender(self, client_id: str):
        self._last_sender_client_id = client_id

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

    def set_tts_state(self, ws: WebSocket, enabled: bool, voice: str = ""):
        if enabled and voice:
            self.tts_clients[ws] = {"enabled": True, "voice": voice}
        else:
            self.tts_clients.pop(ws, None)

    def set_tts_fallback(self, enabled: bool, voice: str = ""):
        """从 HTTP 请求更新备用 TTS 状态（当 WS tts_state 未送达时的保底）"""
        if enabled and voice:
            self._tts_fallback = {"enabled": True, "voice": voice}
        else:
            self._tts_fallback = {}

    def any_tts_enabled(self) -> bool:
        if any(c.get("enabled") for c in self.tts_clients.values()):
            return True
        return bool(self._tts_fallback.get("enabled"))

    def get_tts_voice(self) -> str | None:
        for c in self.tts_clients.values():
            if c.get("enabled"):
                return c.get("voice")
        if self._tts_fallback.get("enabled"):
            return self._tts_fallback.get("voice")
        return None

    async def broadcast(self, data: dict, exclude: WebSocket = None):
        msg = json.dumps(data, ensure_ascii=False)
        msg_type = data.get("type", "unknown")
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
