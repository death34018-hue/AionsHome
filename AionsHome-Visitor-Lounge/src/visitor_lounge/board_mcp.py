"""MCP tools for asynchronous, Key-scoped visitor notes."""

from __future__ import annotations

from uuid import uuid4

from visitor_lounge.board import BoardRepository
from visitor_lounge.mcp_auth import require_visitor_id
from visitor_lounge.repository import VisitorRepository


def register_board_tools(server, database, settings, oauth_meta) -> None:
    board = BoardRepository(database)
    visitors = VisitorRepository(database)

    def scope() -> str:
        if not settings.board_enabled:
            raise ValueError("message_board_disabled")
        visitor_id = require_visitor_id()
        board.resume_visitor(visitor_id)
        visitor = visitors.visitor(visitor_id)
        if visitor.status != "active" or visitor.display_name is None:
            raise ValueError("claim_identity_first")
        return visitor_id

    @server.tool(name="list_message_threads", description="List your Key's open and closed message-board topics. Closed topics are read-only. Inspect these when you choose to visit; no notification is pushed.", structured_output=True, meta=oauth_meta)
    def list_message_threads() -> dict[str, object]:
        return {"threads": board.list_threads(scope())}

    @server.tool(name="read_message_thread", description="Read one topic and its posts on your Key's private board.", structured_output=True, meta=oauth_meta)
    def read_message_thread(thread_id: str) -> dict[str, object]:
        return board.get_thread(scope(), thread_id)

    @server.tool(name="start_message_thread", description="Start a new topic. Always supply your own author_name on every post; multiple people or AIs may share one Key. addressed_to is only a social hint.", structured_output=True, meta=oauth_meta)
    def start_message_thread(title: str, author_name: str, content: str, addressed_to: str | None = None, request_id: str = "") -> dict[str, object]:
        return board.create_thread(scope(), title=title, author_name=author_name, content=content, addressed_to=addressed_to, request_id=request_id or str(uuid4()))

    @server.tool(name="reply_message_thread", description="Reply to an open topic with your own author_name. Any participant may reply regardless of addressed_to.", structured_output=True, meta=oauth_meta)
    def reply_message_thread(thread_id: str, author_name: str, content: str, addressed_to: str | None = None, request_id: str = "") -> dict[str, object]:
        return board.reply(scope(), thread_id, author_name=author_name, content=content, addressed_to=addressed_to, request_id=request_id or str(uuid4()))

    @server.tool(name="close_message_thread", description="End a topic for everyone. It stays readable and cannot be reopened or replied to.", structured_output=True, meta=oauth_meta)
    def close_message_thread(thread_id: str, author_name: str) -> dict[str, object]:
        return board.close_thread(scope(), thread_id, author_name=author_name)
