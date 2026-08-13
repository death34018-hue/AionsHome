"""Best-effort bridge to the adjacent AionsHome process."""

from __future__ import annotations

from pathlib import Path

import httpx


class HomeContextBridge:
    def __init__(
        self,
        token_path: Path,
        *,
        endpoint: str = "http://127.0.0.1:8080/api/internal/lounge/host-context",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.token_path = token_path
        self.endpoint = endpoint
        self.transport = transport

    async def fetch(self, query_text: str, recent_messages: list[dict]) -> str:
        try:
            token = self.token_path.read_text("utf-8").strip()
            if not token:
                return ""
            async with httpx.AsyncClient(timeout=5.0, transport=self.transport) as client:
                response = await client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "actor_id": "connor",
                        "query_text": str(query_text or "")[:500],
                        "recent_messages": recent_messages[-6:],
                    },
                )
            if response.status_code != 200:
                return ""
            return str(response.json().get("trusted_home_context") or "")[:12000]
        except Exception:
            return ""

    async def publish_reception_report(
        self,
        visitor_name: str,
        messages: list[dict],
        *,
        turn_count: int,
        status: str = "completed",
    ) -> bool:
        try:
            token = self.token_path.read_text("utf-8").strip()
            if not token:
                return False
            endpoint = self.endpoint.rsplit("/", 1)[0] + "/reception-report"
            async with httpx.AsyncClient(timeout=5.0, transport=self.transport) as client:
                response = await client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "visitor_name": str(visitor_name or "朋友")[:80],
                        "status": status,
                        "turn_count": max(0, int(turn_count or 0)),
                        "messages": messages[-16:],
                    },
                )
            return response.status_code == 200
        except Exception:
            return False
