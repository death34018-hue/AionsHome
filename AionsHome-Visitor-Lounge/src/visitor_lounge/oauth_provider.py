"""OAuth 2.1 provider that binds ChatGPT grants to existing Visitor Keys."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from visitor_lounge.database import Database, utc_now
from visitor_lounge.oauth_repository import (
    OAuthRepository,
    StoredAuthorizationCode,
    StoredToken,
)
from visitor_lounge.security import KeyService
from visitor_lounge.settings import Settings


class InvalidOAuthConsent(ValueError):
    """The pending request or supplied Visitor Key cannot authorize access."""


class VisitorAuthorizationCode(AuthorizationCode):
    code_hash: bytes
    visitor_key_id: str


class VisitorRefreshToken(RefreshToken):
    family_id: str
    visitor_key_id: str
    resource: str


class VisitorOAuthProvider:
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        keys: KeyService,
        clock: Callable[[], datetime] = utc_now,
        public_origin: str,
        resource: str,
        scope: str,
    ) -> None:
        self._repository = OAuthRepository(
            database,
            master_key=settings.master_key,
            digest_secret=settings.session_secret,
        )
        self._keys = keys
        self._clock = clock
        self._public_origin = public_origin.rstrip("/")
        self._resource = resource
        self._scope = scope

    async def get_client(
        self, client_id: str
    ) -> OAuthClientInformationFull | None:
        return self._repository.load_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._repository.save_client(client_info, self._clock())

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        if params.resource != self._resource:
            raise AuthorizeError(
                error="invalid_target",
                error_description=(
                    "This authorization is only valid for the Visitor Lounge MCP resource."
                ),
            )
        scopes = params.scopes or []
        if scopes != [self._scope]:
            raise AuthorizeError(
                error="invalid_scope",
                error_description="The requested Visitor Lounge scope is not available.",
            )
        request_token = self._repository.create_pending(
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            scopes=scopes,
            code_challenge=params.code_challenge,
            resource=params.resource,
            state=params.state,
            now=self._clock(),
        )
        query = urlencode({"request": request_token})
        return f"{self._public_origin}/oauth/consent?{query}"

    def pending_authorization(self, request_token: str) -> bool:
        return self._repository.pending(request_token, self._clock()) is not None

    def complete_authorization(self, request_token: str, visitor_key: str) -> str:
        identity = self._keys.authenticate_identity(visitor_key)
        if identity is None:
            raise InvalidOAuthConsent("authorization request or Visitor Key is invalid")
        visitor_key_id, visitor_id = identity
        completed = self._repository.complete_pending(
            request_token,
            visitor_key_id=visitor_key_id,
            visitor_id=visitor_id,
            now=self._clock(),
        )
        if completed is None:
            raise InvalidOAuthConsent("authorization request or Visitor Key is invalid")
        pending, raw_code = completed
        return construct_redirect_uri(
            pending.redirect_uri,
            code=raw_code,
            state=pending.state,
        )

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> VisitorAuthorizationCode | None:
        stored = self._repository.load_code(
            client.client_id,
            authorization_code,
            self._clock(),
        )
        if stored is None:
            return None
        return VisitorAuthorizationCode(
            code=authorization_code,
            scopes=stored.scopes,
            expires_at=stored.expires_at.timestamp(),
            client_id=stored.client_id,
            code_challenge=stored.code_challenge,
            redirect_uri=AnyUrl(stored.redirect_uri),
            redirect_uri_provided_explicitly=stored.redirect_uri_provided_explicitly,
            resource=stored.resource,
            subject=stored.visitor_id,
            code_hash=stored.code_hash,
            visitor_key_id=stored.visitor_key_id,
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: VisitorAuthorizationCode,
    ) -> OAuthToken:
        stored = StoredAuthorizationCode(
            code_hash=authorization_code.code_hash,
            client_id=authorization_code.client_id,
            visitor_id=str(authorization_code.subject),
            visitor_key_id=authorization_code.visitor_key_id,
            redirect_uri=str(authorization_code.redirect_uri),
            redirect_uri_provided_explicitly=(
                authorization_code.redirect_uri_provided_explicitly
            ),
            scopes=authorization_code.scopes,
            code_challenge=authorization_code.code_challenge,
            resource=str(authorization_code.resource),
            expires_at=datetime.fromtimestamp(
                authorization_code.expires_at,
                tz=self._clock().tzinfo,
            ),
        )
        pair = self._repository.exchange_code(stored, self._clock())
        if pair is None:
            raise TokenError("invalid_grant", "authorization code is no longer valid")
        return OAuthToken(
            access_token=pair.access_token,
            token_type="Bearer",
            expires_in=max(
                0,
                int((pair.access_expires_at - self._clock()).total_seconds()),
            ),
            scope=" ".join(authorization_code.scopes),
            refresh_token=pair.refresh_token,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> VisitorRefreshToken | None:
        stored = self._repository.load_token(
            refresh_token,
            token_kind="refresh",
            client_id=client.client_id,
            now=self._clock(),
        )
        if stored is None:
            return None
        return VisitorRefreshToken(
            token=stored.raw_token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=int(stored.expires_at.timestamp()),
            subject=stored.visitor_id,
            family_id=stored.family_id,
            visitor_key_id=stored.visitor_key_id,
            resource=stored.resource,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: VisitorRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if any(scope not in refresh_token.scopes for scope in scopes):
            raise TokenError("invalid_scope", "requested scope was not previously granted")
        stored = StoredToken(
            raw_token=refresh_token.token,
            family_id=refresh_token.family_id,
            token_kind="refresh",
            client_id=refresh_token.client_id,
            visitor_id=str(refresh_token.subject),
            visitor_key_id=refresh_token.visitor_key_id,
            scopes=refresh_token.scopes,
            resource=refresh_token.resource,
            expires_at=datetime.fromtimestamp(
                int(refresh_token.expires_at or 0),
                tz=self._clock().tzinfo,
            ),
        )
        pair = self._repository.rotate_refresh(stored, scopes, self._clock())
        if pair is None:
            raise TokenError("invalid_grant", "refresh token is no longer valid")
        return OAuthToken(
            access_token=pair.access_token,
            token_type="Bearer",
            expires_in=max(
                0,
                int((pair.access_expires_at - self._clock()).total_seconds()),
            ),
            scope=" ".join(scopes),
            refresh_token=pair.refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        stored = self._repository.load_token(
            token,
            token_kind="access",
            now=self._clock(),
        )
        if stored is not None:
            return AccessToken(
                token="",
                client_id=stored.client_id,
                scopes=stored.scopes,
                expires_at=int(stored.expires_at.timestamp()),
                resource=stored.resource,
                subject=stored.visitor_id,
                claims={
                    "iss": self._public_origin,
                    "visitor_id": stored.visitor_id,
                },
            )
        visitor_id = self._keys.authenticate_bearer(token)
        if visitor_id is None:
            return None
        return AccessToken(
            token="",
            client_id="visitor-key",
            scopes=[self._scope],
            resource=self._resource,
            subject=visitor_id,
            claims={"visitor_id": visitor_id},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        family_id = getattr(token, "family_id", None)
        if isinstance(family_id, str):
            self._repository.revoke_family(family_id, self._clock())
            return
        if not token.token:
            return
        for kind in ("access", "refresh"):
            stored = self._repository.load_token(
                token.token,
                token_kind=kind,
                now=self._clock(),
            )
            if stored is not None:
                self._repository.revoke_family(stored.family_id, self._clock())
                return
