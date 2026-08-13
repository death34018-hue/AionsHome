# ChatGPT Web OAuth Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an OAuth 2.1 bridge that lets ChatGPT web bind once with an existing Visitor Key and then use the same visitor identity through the existing MCP endpoint.

**Architecture:** Use the official MCP Python SDK authorization-server routes and a focused SQLite-backed provider. Store opaque credential hashes, encrypt DCR metadata, and bind every grant to the active Visitor Key row so existing identity, context, memory, quota, revocation, and deletion semantics remain authoritative.

**Tech Stack:** Python 3.11+, FastAPI/Starlette, official `mcp` Python SDK 2.x, SQLite, Fernet, Jinja2, pytest.

## Global Constraints

- Keep ports 8001/8002, the existing database, Cloudflare tunnel, and exact public MCP URL.
- Keep static Visitor Key bearer authentication working.
- Add no account, second login, external identity provider, model call, or global install.
- OAuth scope is exactly `visitor:lounge`; resource is exactly `https://visitor.aionshome.com/mcp`.
- Access/code/pending/refresh lifetimes are 1 hour/5 minutes/10 minutes/30 days.
- Never persist or expose raw OAuth credentials.

---

### Task 1: OAuth persistence

**Files:** Modify `src/visitor_lounge/schema.sql`, `src/visitor_lounge/database.py`; create `src/visitor_lounge/oauth_repository.py`; test `tests/test_oauth_bridge.py`.

**Interfaces:** Produce `OAuthRepository` operations for clients, pending authorizations, codes, token families, lookup, rotation, and revocation.

- [ ] Write failing schema and cascade tests.
- [ ] Run `pytest tests/test_oauth_bridge.py -k schema -v` and confirm missing-table failure.
- [ ] Add the four tables, indexes, typed records, keyed digest helpers, encrypted client storage, atomic consume/exchange, and active-Key joins.
- [ ] Re-run the focused schema tests and commit.

### Task 2: OAuth provider and dual bearer verification

**Files:** Create `src/visitor_lounge/oauth_provider.py`; modify `src/visitor_lounge/mcp_auth.py`; test `tests/test_oauth_bridge.py`.

**Interfaces:** Produce `VisitorOAuthProvider` implementing the SDK provider protocol and accepting direct Visitor Keys through `load_access_token`; produce `complete_authorization(request_token, visitor_key)`.

- [ ] Write failing provider tests for DCR, resource/scopes, code exchange, refresh rotation, static-Key access, expiry, and Key rotation.
- [ ] Run the provider tests and confirm missing provider behavior.
- [ ] Implement random opaque credentials, hashed storage, exact resource/scope checks, one-time codes, rotating token families, and generic failures.
- [ ] Run provider tests and commit.

### Task 3: Consent UI and MCP wiring

**Files:** Create `src/visitor_lounge/oauth_routes.py`, `templates/oauth_consent.html`; modify `static/visitor.css`, `src/visitor_lounge/mcp_app.py`, `src/visitor_lounge/visitor_app.py`; test `tests/test_oauth_bridge.py`.

**Interfaces:** Produce GET/POST `/oauth/consent`; configure SDK DCR, revocation, protected-resource URL, provider authentication, and tool OAuth metadata.

- [ ] Write failing endpoint tests for consent, discovery, registration metadata, and six tool declarations.
- [ ] Run endpoint tests and confirm the missing routes/metadata.
- [ ] Add the consent routes and template, build `MCPServer` with `auth_server_provider`, exact resource URL, DCR/revocation settings, and per-tool `securitySchemes` metadata.
- [ ] Run endpoint tests and commit.

### Task 4: Documentation and light rollout checks

**Files:** Modify `README.md`, `.env.example` only if configuration changed.

**Interfaces:** Document ChatGPT web connection URL, one-time Key flow, static client compatibility, and revocation behavior.

- [ ] Add concise operator/friend instructions without real keys.
- [ ] Run focused OAuth tests, `compileall`, template parsing, and package dependency checks.
- [ ] Restart only Visitor Lounge using the existing scripts; do not alter the shared Cloudflared process.
- [ ] Check local/public discovery, DCR, authorization form, and unauthenticated challenge without calling the model.
- [ ] Commit documentation and report the exact ChatGPT connection steps.
