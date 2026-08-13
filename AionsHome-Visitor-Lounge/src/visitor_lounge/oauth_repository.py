"""SQLite persistence for the Visitor-Key-backed OAuth bridge."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import secrets
import sqlite3
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from mcp.shared.auth import OAuthClientInformationFull

from visitor_lounge.database import Database


PENDING_LIFETIME = timedelta(minutes=10)
CODE_LIFETIME = timedelta(minutes=5)
ACCESS_LIFETIME = timedelta(hours=1)
REFRESH_LIFETIME = timedelta(days=30)


@dataclass(frozen=True)
class PendingAuthorization:
    request_hash: bytes
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    scopes: list[str]
    code_challenge: str
    resource: str
    state: str | None
    expires_at: datetime


@dataclass(frozen=True)
class StoredAuthorizationCode:
    code_hash: bytes
    client_id: str
    visitor_id: str
    visitor_key_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    scopes: list[str]
    code_challenge: str
    resource: str
    expires_at: datetime


@dataclass(frozen=True)
class StoredToken:
    raw_token: str
    family_id: str
    token_kind: str
    client_id: str
    visitor_id: str
    visitor_key_id: str
    scopes: list[str]
    resource: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedTokenPair:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime


class OAuthRepository:
    def __init__(
        self,
        database: Database,
        *,
        master_key: bytes,
        digest_secret: bytes,
    ) -> None:
        self._database = database
        self._fernet = Fernet(master_key)
        self._digest_secret = digest_secret

    def _digest(self, value: str) -> bytes:
        return hmac.new(
            self._digest_secret,
            value.encode("utf-8"),
            hashlib.sha256,
        ).digest()

    def save_client(
        self, client: OAuthClientInformationFull, now: datetime
    ) -> None:
        encrypted = self._fernet.encrypt(client.model_dump_json().encode("utf-8"))
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO oauth_clients (client_id, encrypted_metadata, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    encrypted_metadata = excluded.encrypted_metadata
                """,
                (client.client_id, encrypted, now.isoformat()),
            )

    def load_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT encrypted_metadata FROM oauth_clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = self._fernet.decrypt(bytes(row[0])).decode("utf-8")
            return OAuthClientInformationFull.model_validate_json(value)
        except (InvalidToken, UnicodeDecodeError, ValueError):
            return None

    def create_pending(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        scopes: list[str],
        code_challenge: str,
        resource: str,
        state: str | None,
        now: datetime,
    ) -> str:
        raw_request = secrets.token_urlsafe(32)
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO oauth_pending_authorizations
                    (request_hash, client_id, redirect_uri,
                     redirect_uri_provided_explicitly, scopes_json,
                     code_challenge, resource, state, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._digest(raw_request),
                    client_id,
                    redirect_uri,
                    int(redirect_uri_provided_explicitly),
                    _encode_scopes(scopes),
                    code_challenge,
                    resource,
                    state,
                    now.isoformat(),
                    (now + PENDING_LIFETIME).isoformat(),
                ),
            )
        return raw_request

    def pending(self, raw_request: str, now: datetime) -> PendingAuthorization | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT request_hash, client_id, redirect_uri,
                       redirect_uri_provided_explicitly, scopes_json,
                       code_challenge, resource, state, expires_at
                FROM oauth_pending_authorizations
                WHERE request_hash = ? AND consumed_at IS NULL AND expires_at > ?
                """,
                (self._digest(raw_request), now.isoformat()),
            ).fetchone()
        return None if row is None else _pending_from_row(row)

    def complete_pending(
        self,
        raw_request: str,
        *,
        visitor_key_id: str,
        visitor_id: str,
        now: datetime,
    ) -> tuple[PendingAuthorization, str] | None:
        request_hash = self._digest(raw_request)
        raw_code = secrets.token_urlsafe(32)
        code_hash = self._digest(raw_code)
        with self._database.transaction(immediate=True) as connection:
            key_row = connection.execute(
                """
                SELECT 1 FROM visitor_keys
                JOIN visitors ON visitors.id = visitor_keys.visitor_id
                WHERE visitor_keys.id = ? AND visitor_keys.visitor_id = ?
                  AND visitor_keys.revoked_at IS NULL
                  AND visitors.status IN ('active', 'suspended')
                """,
                (visitor_key_id, visitor_id),
            ).fetchone()
            row = connection.execute(
                """
                SELECT request_hash, client_id, redirect_uri,
                       redirect_uri_provided_explicitly, scopes_json,
                       code_challenge, resource, state, expires_at
                FROM oauth_pending_authorizations
                WHERE request_hash = ? AND consumed_at IS NULL AND expires_at > ?
                """,
                (request_hash, now.isoformat()),
            ).fetchone()
            if key_row is None or row is None:
                return None
            pending = _pending_from_row(row)
            updated = connection.execute(
                """
                UPDATE oauth_pending_authorizations SET consumed_at = ?
                WHERE request_hash = ? AND consumed_at IS NULL
                """,
                (now.isoformat(), request_hash),
            )
            if updated.rowcount != 1:
                return None
            connection.execute(
                """
                INSERT INTO oauth_authorization_codes
                    (code_hash, client_id, visitor_id, visitor_key_id,
                     redirect_uri, redirect_uri_provided_explicitly,
                     scopes_json, code_challenge, resource, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code_hash,
                    pending.client_id,
                    visitor_id,
                    visitor_key_id,
                    pending.redirect_uri,
                    int(pending.redirect_uri_provided_explicitly),
                    _encode_scopes(pending.scopes),
                    pending.code_challenge,
                    pending.resource,
                    now.isoformat(),
                    (now + CODE_LIFETIME).isoformat(),
                ),
            )
        return pending, raw_code

    def load_code(
        self, client_id: str, raw_code: str, now: datetime
    ) -> StoredAuthorizationCode | None:
        with self._database.connection() as connection:
            row = connection.execute(
                """
                SELECT codes.code_hash, codes.client_id, codes.visitor_id,
                       codes.visitor_key_id, codes.redirect_uri,
                       codes.redirect_uri_provided_explicitly,
                       codes.scopes_json, codes.code_challenge, codes.resource,
                       codes.expires_at
                FROM oauth_authorization_codes AS codes
                JOIN visitor_keys ON visitor_keys.id = codes.visitor_key_id
                JOIN visitors ON visitors.id = codes.visitor_id
                WHERE codes.code_hash = ? AND codes.client_id = ?
                  AND codes.consumed_at IS NULL AND codes.expires_at > ?
                  AND visitor_keys.revoked_at IS NULL
                  AND visitors.status IN ('active', 'suspended')
                """,
                (self._digest(raw_code), client_id, now.isoformat()),
            ).fetchone()
        return None if row is None else _code_from_row(row)

    def exchange_code(
        self, code: StoredAuthorizationCode, now: datetime
    ) -> IssuedTokenPair | None:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM oauth_authorization_codes AS codes
                JOIN visitor_keys ON visitor_keys.id = codes.visitor_key_id
                JOIN visitors ON visitors.id = codes.visitor_id
                WHERE codes.code_hash = ? AND codes.client_id = ?
                  AND codes.consumed_at IS NULL AND codes.expires_at > ?
                  AND visitor_keys.revoked_at IS NULL
                  AND visitors.status IN ('active', 'suspended')
                """,
                (code.code_hash, code.client_id, now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE oauth_authorization_codes SET consumed_at = ? WHERE code_hash = ?",
                (now.isoformat(), code.code_hash),
            )
            return self._mint_pair(connection, code, now, family_id=str(uuid4()))

    def load_token(
        self,
        raw_token: str,
        *,
        token_kind: str,
        now: datetime,
        client_id: str | None = None,
    ) -> StoredToken | None:
        query = """
            SELECT tokens.family_id, tokens.token_kind, tokens.client_id,
                   tokens.visitor_id, tokens.visitor_key_id, tokens.scopes_json,
                   tokens.resource, tokens.expires_at
            FROM oauth_tokens AS tokens
            JOIN visitor_keys ON visitor_keys.id = tokens.visitor_key_id
            JOIN visitors ON visitors.id = tokens.visitor_id
            WHERE tokens.token_hash = ? AND tokens.token_kind = ?
              AND tokens.revoked_at IS NULL AND tokens.expires_at > ?
              AND visitor_keys.revoked_at IS NULL
              AND visitors.status IN ('active', 'suspended')
        """
        values: list[object] = [
            self._digest(raw_token),
            token_kind,
            now.isoformat(),
        ]
        if client_id is not None:
            query += " AND tokens.client_id = ?"
            values.append(client_id)
        with self._database.connection() as connection:
            row = connection.execute(query, values).fetchone()
        if row is None:
            return None
        return StoredToken(
            raw_token=raw_token,
            family_id=str(row[0]),
            token_kind=str(row[1]),
            client_id=str(row[2]),
            visitor_id=str(row[3]),
            visitor_key_id=str(row[4]),
            scopes=_decode_scopes(row[5]),
            resource=str(row[6]),
            expires_at=datetime.fromisoformat(str(row[7])),
        )

    def rotate_refresh(
        self,
        refresh: StoredToken,
        scopes: list[str],
        now: datetime,
    ) -> IssuedTokenPair | None:
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM oauth_tokens AS tokens
                JOIN visitor_keys ON visitor_keys.id = tokens.visitor_key_id
                JOIN visitors ON visitors.id = tokens.visitor_id
                WHERE tokens.token_hash = ? AND tokens.token_kind = 'refresh'
                  AND tokens.client_id = ? AND tokens.revoked_at IS NULL
                  AND tokens.expires_at > ? AND visitor_keys.revoked_at IS NULL
                  AND visitors.status IN ('active', 'suspended')
                """,
                (
                    self._digest(refresh.raw_token),
                    refresh.client_id,
                    now.isoformat(),
                ),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE oauth_tokens SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL",
                (now.isoformat(), refresh.family_id),
            )
            code_like = StoredAuthorizationCode(
                code_hash=b"",
                client_id=refresh.client_id,
                visitor_id=refresh.visitor_id,
                visitor_key_id=refresh.visitor_key_id,
                redirect_uri="",
                redirect_uri_provided_explicitly=False,
                scopes=scopes,
                code_challenge="",
                resource=refresh.resource,
                expires_at=now,
            )
            return self._mint_pair(
                connection,
                code_like,
                now,
                family_id=refresh.family_id,
            )

    def revoke_family(self, family_id: str, now: datetime) -> None:
        with self._database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE oauth_tokens SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL",
                (now.isoformat(), family_id),
            )

    def _mint_pair(
        self,
        connection: sqlite3.Connection,
        identity: StoredAuthorizationCode,
        now: datetime,
        *,
        family_id: str,
    ) -> IssuedTokenPair:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        access_expires = now + ACCESS_LIFETIME
        refresh_expires = now + REFRESH_LIFETIME
        for raw, kind, expires in (
            (access, "access", access_expires),
            (refresh, "refresh", refresh_expires),
        ):
            connection.execute(
                """
                INSERT INTO oauth_tokens
                    (id, family_id, token_hash, token_kind, client_id,
                     visitor_id, visitor_key_id, scopes_json, resource,
                     created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    family_id,
                    self._digest(raw),
                    kind,
                    identity.client_id,
                    identity.visitor_id,
                    identity.visitor_key_id,
                    _encode_scopes(identity.scopes),
                    identity.resource,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
        return IssuedTokenPair(access, refresh, access_expires, refresh_expires)


def _encode_scopes(scopes: list[str]) -> str:
    return json.dumps(scopes, ensure_ascii=True, separators=(",", ":"))


def _decode_scopes(value: object) -> list[str]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        return []
    return decoded


def _pending_from_row(row: sqlite3.Row | tuple[object, ...]) -> PendingAuthorization:
    return PendingAuthorization(
        request_hash=bytes(row[0]),
        client_id=str(row[1]),
        redirect_uri=str(row[2]),
        redirect_uri_provided_explicitly=bool(row[3]),
        scopes=_decode_scopes(row[4]),
        code_challenge=str(row[5]),
        resource=str(row[6]),
        state=None if row[7] is None else str(row[7]),
        expires_at=datetime.fromisoformat(str(row[8])),
    )


def _code_from_row(row: sqlite3.Row | tuple[object, ...]) -> StoredAuthorizationCode:
    return StoredAuthorizationCode(
        code_hash=bytes(row[0]),
        client_id=str(row[1]),
        visitor_id=str(row[2]),
        visitor_key_id=str(row[3]),
        redirect_uri=str(row[4]),
        redirect_uri_provided_explicitly=bool(row[5]),
        scopes=_decode_scopes(row[6]),
        code_challenge=str(row[7]),
        resource=str(row[8]),
        expires_at=datetime.fromisoformat(str(row[9])),
    )
