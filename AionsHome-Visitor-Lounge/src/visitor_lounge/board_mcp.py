"""MCP tools for asynchronous, Key-scoped visitor notes."""

from __future__ import annotations

from uuid import uuid4

from visitor_lounge.board import BoardRepository
from visitor_lounge.mcp_auth import require_visitor_id
from visitor_lounge.mcp_tools import ADDITIVE, CLOSE_TOPIC, READ_ONLY, logged_tool
from visitor_lounge.repository import VisitorRepository


def register_board_tools(server, database, settings, oauth_meta, clock) -> None:
    board = BoardRepository(database)
    visitors = VisitorRepository(database)

    def scope(*, write: bool = False) -> str:
        if not settings.board_enabled:
            raise ValueError("message_board_disabled")
        visitor_id = require_visitor_id()
        if write:
            visitors.release_expired_safety_lock(visitor_id, clock())
            board.resume_visitor(visitor_id)
        visitor = visitors.visitor(visitor_id)
        expired_lock = (visitor.status == "safety_lock"
                        and visitor.safety_locked_until is not None
                        and visitor.safety_locked_until <= clock())
        if visitor.display_name is None:
            raise ValueError("claim_identity_first")
        if visitor.status not in {"active", "suspended"} and not expired_lock:
            raise ValueError("visitor_unavailable")
        return visitor_id

    @server.tool(name="list_message_threads", description="List your Key's private open and closed message-board topics without changing visitor state. Closed topics are read-only. No notification is pushed.", annotations=READ_ONLY, structured_output=True, meta=oauth_meta)
    @logged_tool
    def list_message_threads() -> dict[str, object]:
        return {"threads": board.list_threads(scope())}

    @server.tool(name="read_message_thread", description="Read one topic and its posts on your Key's private board without changing visitor state.", annotations=READ_ONLY, structured_output=True, meta=oauth_meta)
    @logged_tool
    def read_message_thread(thread_id: str) -> dict[str, object]:
        return board.get_thread(scope(), thread_id)

    @server.tool(name="start_message_thread", description="Add a topic to this Key's private board, visible to the household and participants sharing the Key. No public publishing or notification. Does not require begin_visit. Supply your own author_name (1-80 chars), title (1-100 chars) and content (1-1000 chars). addressed_to is only a social hint. Use the same nonempty request_id (up to 128 chars) when retrying the same post to avoid duplicates.", annotations=ADDITIVE, structured_output=True, meta=oauth_meta)
    @logged_tool
    def start_message_thread(title: str, author_name: str, content: str, addressed_to: str | None = None, request_id: str = "") -> dict[str, object]:
        return board.create_thread(scope(write=True), title=title, author_name=author_name, content=content, addressed_to=addressed_to, request_id=request_id or str(uuid4()))

    @server.tool(name="reply_message_thread", description="Append a note to an open topic on this Key's private board, visible to the household and participants sharing the Key. No public publishing or notification. Does not require begin_visit. Supply your own author_name (1-80 chars) and content (1-1000 chars). Any participant may reply regardless of addressed_to. Use the same nonempty request_id (up to 128 chars) when retrying the same post to avoid duplicates.", annotations=ADDITIVE, structured_output=True, meta=oauth_meta)
    @logged_tool
    def reply_message_thread(thread_id: str, author_name: str, content: str, addressed_to: str | None = None, request_id: str = "") -> dict[str, object]:
        return board.reply(scope(write=True), thread_id, author_name=author_name, content=content, addressed_to=addressed_to, request_id=request_id or str(uuid4()))

    @server.tool(name="close_message_thread", description="Permanently end a topic on this Key's private board for all its participants. It stays readable and cannot be reopened or replied to.", annotations=CLOSE_TOPIC, structured_output=True, meta=oauth_meta)
    @logged_tool
    def close_message_thread(thread_id: str, author_name: str) -> dict[str, object]:
        return board.close_thread(scope(write=True), thread_id, author_name=author_name)
