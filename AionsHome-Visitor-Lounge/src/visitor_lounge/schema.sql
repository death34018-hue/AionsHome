PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS visitors (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    visitor_kind TEXT NOT NULL DEFAULT 'human'
        CHECK(visitor_kind IN ('human', 'external_ai')),
    safety_locked_until TEXT,
    disclosure_version TEXT,
    disclosure_consented_at TEXT,
    last_summarized_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS visitor_keys (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    key_hash BLOB NOT NULL,
    encrypted_value BLOB NOT NULL,
    masked TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    device_id TEXT NOT NULL,
    session_hash BLOB NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    encrypted_metadata BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_pending_authorizations (
    request_hash BLOB PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    redirect_uri TEXT NOT NULL,
    redirect_uri_provided_explicitly INTEGER NOT NULL CHECK(
        redirect_uri_provided_explicitly IN (0, 1)
    ),
    scopes_json TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    resource TEXT NOT NULL,
    state TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
    code_hash BLOB PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    visitor_key_id TEXT NOT NULL REFERENCES visitor_keys(id) ON DELETE CASCADE,
    redirect_uri TEXT NOT NULL,
    redirect_uri_provided_explicitly INTEGER NOT NULL CHECK(
        redirect_uri_provided_explicitly IN (0, 1)
    ),
    scopes_json TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    resource TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    token_hash BLOB NOT NULL UNIQUE,
    token_kind TEXT NOT NULL CHECK(token_kind IN ('access', 'refresh')),
    client_id TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    visitor_key_id TEXT NOT NULL REFERENCES visitor_keys(id) ON DELETE CASCADE,
    scopes_json TEXT NOT NULL,
    resource TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS oauth_tokens_by_family
    ON oauth_tokens(family_id);

CREATE TABLE IF NOT EXISTS visits (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES auth_sessions(id) ON DELETE SET NULL,
    started_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS visits_one_open_per_visitor
    ON visits(visitor_id)
    WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS quota_windows (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    limit_count INTEGER NOT NULL,
    used_count INTEGER NOT NULL DEFAULT 0,
    reserved_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    ends_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    sender TEXT NOT NULL CHECK(sender IN ('visitor', 'host')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'web'
        CHECK(source IN ('web', 'mcp')),
    delivery_status TEXT NOT NULL DEFAULT 'accepted'
        CHECK(delivery_status IN ('accepted', 'failed'))
);

CREATE TABLE IF NOT EXISTS summaries (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    first_message_id TEXT NOT NULL REFERENCES messages(id),
    last_message_id TEXT NOT NULL REFERENCES messages(id),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_jobs (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    first_message_id TEXT NOT NULL REFERENCES messages(id),
    last_message_id TEXT NOT NULL REFERENCES messages(id),
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS summary_jobs_one_unfinished_per_visitor
    ON summary_jobs(visitor_id)
    WHERE status IN ('queued', 'running', 'failed');

CREATE TABLE IF NOT EXISTS summary_generation_attempts (
    id TEXT PRIMARY KEY,
    summary_job_id TEXT NOT NULL REFERENCES summary_jobs(id) ON DELETE CASCADE,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN (
        'running', 'completed', 'failed', 'timed_out', 'interrupted'
    )),
    usage_reported INTEGER NOT NULL DEFAULT 0 CHECK(usage_reported IN (0, 1)),
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS summary_generation_attempts_by_visitor_started
    ON summary_generation_attempts(visitor_id, started_at);

CREATE TABLE IF NOT EXISTS generation_jobs (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    response_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    quota_window_id TEXT REFERENCES quota_windows(id) ON DELETE SET NULL,
    request_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('chat', 'summary')),
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted'
    )),
    visible_text TEXT NOT NULL DEFAULT '',
    action TEXT CHECK(action IN (
        'continue', 'closing', 'end', 'suspend', 'safety_lock'
    )),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    confirmed_at TEXT,
    refunded_at TEXT,
    refund_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS generation_jobs_one_active_per_visitor
    ON generation_jobs(visitor_id)
    WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS model_calls (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES generation_jobs(id) ON DELETE CASCADE,
    usage_reported INTEGER NOT NULL DEFAULT 0 CHECK(usage_reported IN (0, 1)),
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS notification_events (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    visitor_id TEXT REFERENCES visitors(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reception_settings (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    persona_text TEXT NOT NULL,
    first_welcome TEXT NOT NULL,
    returning_welcome TEXT NOT NULL,
    quota_exhausted TEXT NOT NULL,
    unsafe_request TEXT NOT NULL,
    credential_detected TEXT,
    input_too_long TEXT NOT NULL,
    lounge_closed TEXT NOT NULL,
    system_unavailable TEXT NOT NULL,
    hourly_quota_limit INTEGER NOT NULL DEFAULT 15
        CHECK(hourly_quota_limit BETWEEN 1 AND 500),
    lounge_enabled INTEGER NOT NULL CHECK(lounge_enabled IN (0, 1)),
    idle_minutes INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
