"""Visitor-Key authentication adapter for the official MCP SDK."""

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier

from visitor_lounge.security import KeyService


class VisitorKeyTokenVerifier(TokenVerifier):
    def __init__(self, keys: KeyService) -> None:
        self._keys = keys

    async def verify_token(self, token: str) -> AccessToken | None:
        visitor_id = self._keys.authenticate_bearer(token)
        if visitor_id is None:
            return None
        return AccessToken(
            token="",
            client_id="visitor-key",
            scopes=["visitor:lounge"],
            subject=visitor_id,
            claims={"visitor_id": visitor_id},
        )


def require_visitor_id() -> str:
    access = get_access_token()
    if access is None:
        raise RuntimeError("authenticated visitor identity missing")
    visitor_id = access.subject or (access.claims or {}).get("visitor_id")
    if not visitor_id or not isinstance(visitor_id, str):
        raise RuntimeError("authenticated visitor identity missing")
    return visitor_id
