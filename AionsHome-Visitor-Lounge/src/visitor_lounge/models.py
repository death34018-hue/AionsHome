"""Shared value types used across Visitor Lounge services."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Mapping


VisitorKind = Literal["human", "external_ai"]
MessageSource = Literal["web", "mcp"]
DeliveryStatus = Literal["accepted", "failed"]


@dataclass(frozen=True)
class GenerationRequest:
    job_id: str
    request_id: str
    visitor_id: str
    message_id: str
    prompt: str
    kind: Literal["chat", "summary"] = "chat"
    source: MessageSource = "web"


@dataclass(frozen=True)
class GenerationResult:
    text: str
    input_tokens: int
    output_tokens: int
    action: Literal["continue", "suspend", "safety_lock"] = "continue"
    usage_reported: bool = False


@dataclass(frozen=True)
class VisitorSession:
    id: str
    visitor_id: str
    device_id: str
    expires_at: datetime


@dataclass(frozen=True)
class PlainVisitorKey:
    visitor_id: str
    value: str
    masked: str


@dataclass(frozen=True)
class QuotaState:
    window_id: str
    limit: int
    used: int
    reserved: int
    started_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class QuotaReservation:
    job_id: str
    request_id: str
    visitor_id: str
    window_id: str


@dataclass(frozen=True)
class Message:
    id: str
    visitor_id: str
    sender: Literal["visitor", "host"]
    content: str
    created_at: datetime
    source: MessageSource = "web"
    delivery_status: DeliveryStatus = "accepted"


@dataclass(frozen=True)
class VisitorRecord:
    id: str
    display_name: str | None
    status: str
    disclosure_version: str | None = None
    visitor_kind: VisitorKind = "human"
    safety_locked_until: datetime | None = None


@dataclass(frozen=True)
class GenerationJobRecord:
    id: str
    request_id: str
    visitor_id: str
    message_id: str | None
    response_message_id: str | None
    status: str
    visible_text: str
    action: str | None
    usage_reported: bool
    input_tokens: int
    output_tokens: int
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class Summary:
    id: str
    visitor_id: str
    first_message_id: str
    last_message_id: str
    text: str


@dataclass(frozen=True)
class SummaryJob:
    id: str
    visitor_id: str
    first_message_id: str
    last_message_id: str


@dataclass(frozen=True)
class GenerationChunk:
    kind: Literal["text", "usage", "completed"]
    text: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)
    action: Literal["continue", "suspend", "safety_lock"] = "continue"


@dataclass(frozen=True)
class JobEvent:
    kind: Literal[
        "queued", "started", "text", "usage", "snapshot", "completed", "failed"
    ]
    data: Mapping[str, object] = field(default_factory=dict)
    event_id: int = 0


@dataclass(frozen=True)
class RuntimeResourceState:
    can_start: bool
    checked_at: datetime
