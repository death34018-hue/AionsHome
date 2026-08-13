"""Official MCP SDK server exposing the six Visitor Lounge tools."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from visitor_lounge.container import Container
from visitor_lounge.mcp_auth import require_visitor_id
from visitor_lounge.mcp_service import McpLoungeService
from visitor_lounge.oauth_provider import VisitorOAuthProvider
from visitor_lounge.repository import VisitorRepository
from visitor_lounge.security import KeyService
from visitor_lounge.turn_coordinator import VisitorTurnCoordinator
from visitor_lounge.visitor_service import VisitorService


MCP_SCOPE = "visitor:lounge"
MCP_PUBLIC_ORIGIN = "https://visitor.aionshome.com"
MCP_RESOURCE = f"{MCP_PUBLIC_ORIGIN}/mcp"
MCP_MAX_REQUEST_BODY_BYTES = 16 * 1024


def create_mcp_server(
    container: Container,
    coordinator: VisitorTurnCoordinator,
) -> tuple[MCPServer[Any], Starlette, VisitorOAuthProvider]:
    visitor_service = container.visitor_service
    if not isinstance(visitor_service, VisitorService):
        raise RuntimeError("visitor service must exist before MCP setup")
    keys = KeyService(VisitorRepository(container.database), container.settings)
    oauth_provider = VisitorOAuthProvider(
        database=container.database,
        settings=container.settings,
        keys=keys,
        clock=container.clock,
        public_origin=MCP_PUBLIC_ORIGIN,
        resource=MCP_RESOURCE,
        scope=MCP_SCOPE,
    )
    lounge = McpLoungeService(container, visitor_service, coordinator)
    server = MCPServer(
        name="aionshome-visitor-lounge",
        title="AionsHome Visitor Lounge",
        version="1.0.0",
        instructions=(
            "A private text-only visitor lounge. A Visitor Key identifies exactly "
            "one visitor across every client. Call get_lounge_info first. An "
            "unclaimed visitor must call claim_identity before talking to "
            f"{container.settings.host_display_name}. "
            "Only text is accepted, and each message is at most 500 characters."
        ),
        auth=AuthSettings(
            issuer_url=MCP_PUBLIC_ORIGIN,
            resource_server_url=MCP_RESOURCE,
            required_scopes=[MCP_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[MCP_SCOPE],
                default_scopes=[MCP_SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        auth_server_provider=oauth_provider,
    )

    oauth_meta = {
        "securitySchemes": [{"type": "oauth2", "scopes": [MCP_SCOPE]}]
    }

    @server.tool(
        name="get_lounge_info",
        description=(
            "Read the lounge rules, identity claim state, text limits, and current "
            "availability. Call this first."
        ),
        structured_output=True,
        meta=oauth_meta,
    )
    def get_lounge_info() -> dict[str, object]:
        return lounge.get_lounge_info(require_visitor_id())

    @server.tool(
        name="claim_identity",
        description=(
            "On first use only, consent to the visit record and permanently bind a "
            "display name to this Visitor Key. Names may repeat; only the Key is identity."
        ),
        structured_output=True,
        meta=oauth_meta,
    )
    def claim_identity(name: str, consent: bool) -> dict[str, object]:
        return lounge.claim_identity(require_visitor_id(), name, consent)

    @server.tool(
        name="begin_visit",
        description=(
            "Open or resume this Key's one shared visit and receive the latest shared "
            "text timeline, quota state, and memory availability."
        ),
        structured_output=True,
        meta=oauth_meta,
    )
    def begin_visit() -> dict[str, object]:
        return lounge.begin_visit(require_visitor_id())

    @server.tool(
        name="talk_to_host",
        description=(
            "Send exactly one pure-text message to the configured host and wait for one reply. "
            "Images, files, URLs as attachments, and binary content are not accepted. "
            "The message must not exceed 500 Unicode characters; compress or split "
            "longer input before calling. A failed generation is not retried and does "
            "not consume visitor quota."
        ),
        structured_output=True,
        meta=oauth_meta,
    )
    async def talk_to_host(
        message: str, request_id: str = ""
    ) -> dict[str, object]:
        return await lounge.talk_to_host(
            require_visitor_id(), message, request_id
        )

    @server.tool(
        name="get_visit_state",
        description=(
            "Read this Key's shared text timeline and state without calling the model. "
            "Pass the last seen message ID to receive only later messages."
        ),
        structured_output=True,
        meta=oauth_meta,
    )
    def get_visit_state(
        after_message_id: str | None = None,
    ) -> dict[str, object]:
        return lounge.get_visit_state(require_visitor_id(), after_message_id)

    @server.tool(
        name="end_visit",
        description=(
            "End the current visit without deleting the fixed identity, shared messages, "
            "visitor memory, or quota state."
        ),
        structured_output=True,
        meta=oauth_meta,
    )
    async def end_visit(final_message: str | None = None) -> dict[str, object]:
        return await lounge.end_visit(
            require_visitor_id(), final_message=final_message
        )

    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "visitor.aionshome.com",
        ],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            MCP_PUBLIC_ORIGIN,
        ],
    )
    mcp_asgi = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=MCP_MAX_REQUEST_BODY_BYTES,
        transport_security=transport_security,
    )
    return server, mcp_asgi, oauth_provider
