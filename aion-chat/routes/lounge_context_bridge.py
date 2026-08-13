"""Credential-protected, loopback-oriented Visitor Lounge context route."""

from __future__ import annotations

import hmac
from typing import Callable

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from lounge_context_bridge import build_host_context, get_bridge_token
from lounge_visit_reporting import publish_inbound_report


class ContextMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: str = "inbound"
    content: str = Field(max_length=500)


class HostContextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor_id: str
    query_text: str = Field(max_length=500)
    recent_messages: list[ContextMessage] = Field(default_factory=list, max_length=6)


class ReceptionReportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visitor_name: str = Field(min_length=1, max_length=80)
    status: str = Field(default="completed", max_length=24)
    turn_count: int = Field(default=0, ge=0, le=100)
    messages: list[ContextMessage] = Field(default_factory=list, max_length=16)


def create_router(
    *,
    token_provider: Callable[[], str] = get_bridge_token,
    context_builder: Callable = build_host_context,
    inbound_publisher: Callable = publish_inbound_report,
) -> APIRouter:
    router = APIRouter(tags=["lounge-context-bridge"])

    @router.post("/api/internal/lounge/host-context")
    async def host_context(
        body: HostContextBody,
        authorization: str = Header(default=""),
    ):
        expected = f"Bearer {token_provider()}"
        if not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")
        if body.actor_id != "connor":
            raise HTTPException(status_code=404, detail="Actor not found")
        timeline = [
            item.model_dump() if hasattr(item, "model_dump") else item.dict()
            for item in body.recent_messages
        ]
        context = await context_builder(body.actor_id, body.query_text, timeline)
        trusted = "\n".join(
            str(item.get("content") or "") for item in context if item.get("content")
        )[:12000]
        return {"trusted_home_context": trusted}

    @router.post("/api/internal/lounge/reception-report")
    async def reception_report(
        body: ReceptionReportBody,
        authorization: str = Header(default=""),
    ):
        expected = f"Bearer {token_provider()}"
        if not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")
        messages = [
            item.model_dump() if hasattr(item, "model_dump") else item.dict()
            for item in body.messages
        ]
        message = await inbound_publisher(
            "connor",
            body.visitor_name,
            messages,
            status=body.status,
            turn_count=body.turn_count,
        )
        return {"ok": True, "message_id": (message or {}).get("id", "")}

    return router


router = create_router()
