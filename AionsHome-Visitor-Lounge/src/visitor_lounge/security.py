"""Credential, session, name-validation, and local rate-limit helpers."""

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import hmac
import secrets
import sqlite3
from threading import Lock
import time
import unicodedata

from cryptography.fernet import Fernet

from visitor_lounge.database import utc_now
from visitor_lounge.models import PlainVisitorKey, VisitorSession
from visitor_lounge.repository import VisitorRepository
from visitor_lounge.settings import Settings


class InvalidVisitorName(ValueError):
    """Raised when a visitor name is empty, too long, or reserved."""


class RateLimitExceeded(ValueError):
    """Raised when an in-process attempt bucket is empty."""


def normalize_visitor_name(value: str, reserved: set[str]) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    cleaned = "".join(ch for ch in normalized if unicodedata.category(ch)[0] != "C")
    if not 1 <= len(cleaned) <= 200 or cleaned.casefold() in {
        name.casefold() for name in reserved
    }:
        raise InvalidVisitorName("这个名字暂时不能使用")
    return cleaned


def key_digest(raw_key: str, pepper: bytes) -> str:
    return hmac.new(pepper, raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


def session_digest(raw_cookie: str, secret: bytes) -> str:
    return hmac.new(secret, raw_cookie.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    """Small process-local limiter; callers choose a stable, non-secret scope."""

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        *,
        max_buckets: int = 1024,
    ) -> None:
        if max_buckets < 1:
            raise ValueError("max_buckets must be positive")
        self._capacity = float(capacity)
        self._refill_per_second = refill_per_second
        self._max_buckets = max_buckets
        self._buckets: dict[str, _Bucket] = {}
        self._lock = Lock()

    def allow(self, scope: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(scope)
            if bucket is None:
                if len(self._buckets) >= self._max_buckets:
                    oldest_scope = min(
                        self._buckets,
                        key=lambda existing: self._buckets[existing].updated_at,
                    )
                    del self._buckets[oldest_scope]
                bucket = _Bucket(self._capacity, now)
                self._buckets[scope] = bucket
            bucket.tokens = min(
                self._capacity,
                bucket.tokens + (now - bucket.updated_at) * self._refill_per_second,
            )
            bucket.updated_at = now
            if bucket.tokens < 1:
                return False
            bucket.tokens -= 1
            return True


class AttemptLimiters:
    """The two lightweight buckets used by login and future message services."""

    def __init__(self) -> None:
        self.key_login = TokenBucketLimiter(capacity=10, refill_per_second=10 / 60)
        self.message = TokenBucketLimiter(capacity=20, refill_per_second=20 / 60)


class KeyService:
    _LOGIN_LIMIT_SCOPE = "global"

    def __init__(
        self,
        repository: VisitorRepository,
        settings: Settings,
        *,
        login_limiter: TokenBucketLimiter | None = None,
    ) -> None:
        self._repository = repository
        self._pepper = settings.key_pepper
        self._fernet = Fernet(settings.master_key)
        self._login_limiter = login_limiter or AttemptLimiters().key_login

    def create(
        self,
        visitor_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> PlainVisitorKey:
        return self._store_new_key(
            visitor_id, revoke_existing=True, connection=connection
        )

    def rotate(
        self,
        visitor_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> PlainVisitorKey:
        return self._store_new_key(
            visitor_id, revoke_existing=True, connection=connection
        )

    def authenticate(self, raw_key: str) -> str | None:
        identity = self.authenticate_identity(raw_key)
        return None if identity is None else identity[1]

    def authenticate_identity(self, raw_key: str) -> tuple[str, str] | None:
        """Rate-limited browser authentication returning Key and visitor IDs."""
        if not self._login_limiter.allow(self._LOGIN_LIMIT_SCOPE):
            raise RateLimitExceeded("登录尝试过于频繁")
        return self.authenticate_bearer_identity(raw_key)

    def authenticate_bearer(self, raw_key: str) -> str | None:
        """Verify a high-entropy API credential without retaining the raw Key."""
        identity = self.authenticate_bearer_identity(raw_key)
        return None if identity is None else identity[1]

    def authenticate_bearer_identity(self, raw_key: str) -> tuple[str, str] | None:
        """Return the active Key row and visitor IDs for an API credential."""
        digest = key_digest(raw_key, self._pepper)
        candidate = self._repository.active_key_identity_candidate(digest)
        if candidate is None:
            return None
        key_id, visitor_id, stored_digest = candidate
        return (
            (key_id, visitor_id)
            if hmac.compare_digest(stored_digest, digest)
            else None
        )

    def _store_new_key(
        self,
        visitor_id: str,
        *,
        revoke_existing: bool,
        connection: sqlite3.Connection | None,
    ) -> PlainVisitorKey:
        value = secrets.token_urlsafe(32)
        self._repository.add_key(
            visitor_id,
            key_digest(value, self._pepper),
            self._fernet.encrypt(value.encode("utf-8")),
            f"…{value[-4:]}",
            revoke_existing=revoke_existing,
            connection=connection,
        )
        return PlainVisitorKey(visitor_id=visitor_id, value=value, masked=f"…{value[-4:]}")


class SessionService:
    def __init__(
        self,
        repository: VisitorRepository,
        settings: Settings,
        *,
        lifetime: timedelta = timedelta(days=30),
    ) -> None:
        self._repository = repository
        self._secret = settings.session_secret
        self._lifetime = lifetime

    def issue(self, visitor_id: str, device_id: str) -> str:
        raw_cookie = f"{visitor_id}.{secrets.token_urlsafe(32)}"
        self._repository.replace_sessions(
            visitor_id,
            device_id,
            session_digest(raw_cookie, self._secret),
            utc_now() + self._lifetime,
        )
        return raw_cookie

    def resolve(self, raw_cookie: str) -> VisitorSession | None:
        visitor_id, separator, _ = raw_cookie.partition(".")
        if not visitor_id or not separator:
            return None
        digest = session_digest(raw_cookie, self._secret)
        return self._repository.active_session_for_visitor(visitor_id, digest, utc_now())

    def revoke(self, raw_cookie: str) -> None:
        session = self.resolve(raw_cookie)
        if session is not None:
            self._repository.revoke_session(session.id)
