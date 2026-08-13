from cryptography.fernet import Fernet
import pytest

from visitor_lounge.repository import VisitorRepository
from visitor_lounge.security import (
    InvalidVisitorName,
    KeyService,
    RateLimitExceeded,
    SessionService,
    TokenBucketLimiter,
    normalize_visitor_name,
)
from visitor_lounge.settings import Settings


class IdentityHarness:
    def __init__(self, database, tmp_path):
        database.initialize()
        settings = Settings(
            root=tmp_path,
            database_path=database.path,
            visitor_host="127.0.0.1",
            visitor_port=8001,
            admin_host="127.0.0.1",
            admin_port=8002,
            max_generations=1,
            max_generations_hard_limit=2,
            max_waiting=3,
            queue_timeout_seconds=120,
            generation_timeout_seconds=120,
            key_pepper=b"key-pepper",
            master_key=Fernet.generate_key(),
            session_secret=b"session-secret",
            codex_workdir=tmp_path / "codex-workdir",
        )
        self.repository = VisitorRepository(database)
        self.keys = KeyService(self.repository, settings)
        self.sessions = SessionService(self.repository, settings)
        self.visitor_id = self.repository.create_unclaimed_visitor()

    def create_unclaimed_visitor(self):
        visitor_id = self.repository.create_unclaimed_visitor()
        return visitor_id, self.keys.create(visitor_id).value

    def rotate_key(self, visitor_id):
        return self.keys.rotate(visitor_id).value

    def authenticate(self, raw_key):
        return self.keys.authenticate(raw_key)

    def login(self, raw_key, device_id):
        visitor_id = self.keys.authenticate(raw_key)
        assert visitor_id is not None
        return self.sessions.issue(visitor_id, device_id)

    def resolve_session(self, cookie):
        return self.sessions.resolve(cookie)

    def claim_name(self, value):
        return self.repository.claim_name(
            self.visitor_id,
            normalize_visitor_name(value, {"Ithil"}),
        )


@pytest.fixture
def identity(database, tmp_path):
    return IdentityHarness(database, tmp_path)


def test_key_rotation_keeps_identity_and_invalidates_old_key(identity):
    visitor_id, first = identity.create_unclaimed_visitor()

    replacement = identity.rotate_key(visitor_id)

    assert identity.authenticate(first) is None
    assert identity.authenticate(replacement) == visitor_id


def test_creating_a_second_key_replaces_the_only_active_key(identity):
    visitor_id, first = identity.create_unclaimed_visitor()

    second = identity.keys.create(visitor_id).value

    assert identity.authenticate(first) is None
    assert identity.authenticate(second) == visitor_id
    with identity.repository._database.connection() as connection:
        active = connection.execute(
            "SELECT COUNT(*) FROM visitor_keys WHERE visitor_id = ? AND revoked_at IS NULL",
            (visitor_id,),
        ).fetchone()[0]
    assert active == 1


def test_new_device_replaces_old_session(identity):
    visitor_id, key = identity.create_unclaimed_visitor()

    first = identity.login(key, device_id="laptop")
    second = identity.login(key, device_id="phone")

    assert identity.resolve_session(first) is None
    assert identity.resolve_session(second).visitor_id == visitor_id


def test_claim_normalizes_name_and_rejects_reserved(identity):
    assert identity.claim_name("  Ａｌｉｃｅ  ") == "Alice"

    with pytest.raises(InvalidVisitorName):
        identity.claim_name("Ithil")


def test_different_bad_keys_share_default_login_limit(identity):
    for attempt in range(10):
        assert identity.authenticate(f"invalid-key-{attempt}") is None

    with pytest.raises(RateLimitExceeded):
        identity.authenticate("invalid-key-10")


def test_token_bucket_evicts_oldest_scope_at_capacity():
    limiter = TokenBucketLimiter(
        capacity=1,
        refill_per_second=0,
        max_buckets=2,
    )

    assert limiter.allow("oldest")
    assert not limiter.allow("oldest")
    assert limiter.allow("middle")
    assert limiter.allow("newest")
    assert limiter.allow("oldest")
