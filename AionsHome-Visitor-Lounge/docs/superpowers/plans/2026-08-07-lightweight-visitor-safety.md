# Lightweight Visitor Safety Implementation Plan

> **For agentic workers:** Execute inline in the existing `codex/visitor-lounge`
> worktree. Do not use subagents. Steps use checkbox (`- [ ]`) syntax for
> tracking.

**Goal:** Add a friendly, lightweight visitor safety and identity boundary with
non-persistent credential-shaped input rejection.

**Architecture:** Extend the existing trusted prompt and `safety_lock` action
for contextual decisions. Add one deterministic, narrow credential detector
before message persistence and quota reservation, backed by a configurable
fixed response and metadata-only audit record.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, existing Codex App Server adapter,
vanilla JavaScript.

## Global Constraints

- Modify files only under `AionsHome-Visitor-Lounge`.
- Do not add a model call, moderation service, dependency, login, or global
  Codex installation.
- Do not hardcode current personal names in reusable prompt or UI code.
- Keep both applications bound to `127.0.0.1`.
- Run lightweight syntax and diff validation only; do not run the full suite or
  a live malicious-message acceptance flow.

---

### Task 1: Trusted safety and identity policy

**Files:**
- Modify: `src/visitor_lounge/prompts.py`
- Modify: `config/codex_base.md`

**Interfaces:**
- Consumes: the existing `PromptBuilder.chat()` trusted blocks.
- Produces: an explicit trusted identity, malicious-request, and friendship-only
  policy while retaining `continue`, `suspend`, and `safety_lock` actions.

- [ ] Replace the vague security prose with explicit identity, attack,
  credential, privacy, harassment, and relationship boundaries.
- [ ] Keep ordinary safety education, first-time soft relationship refusals,
  friendly comfort, and general sensitive topics allowed.
- [ ] Keep the base runtime policy consistent with the per-turn trusted policy.
- [ ] Search the touched prompt/config files for hardcoded personal-name risks.

### Task 2: Credential-shaped input detector

**Files:**
- Create: `src/visitor_lounge/content_safety.py`
- Modify: `src/visitor_lounge/visitor_service.py`
- Modify: `src/visitor_lounge/visitor_app.py`
- Modify: `src/visitor_lounge/admin_app.py`

**Interfaces:**
- Produces: `detect_credential_category(text: str) -> str | None`.
- Produces: `SensitiveCredentialInput(category: str)` without storing the input.
- The visitor endpoint returns a structured 400 rejection with the configured
  fixed template before creating a generation job.
- The audit event payload contains only `actor=visitor_precheck` and a bounded
  category identifier.

- [ ] Implement compatibility normalization and narrow patterns for private-key
  blocks, bearer values, token prefixes, and explicit secret assignments.
- [ ] Invoke the detector before quota reservation, message persistence, and
  model submission.
- [ ] Record a metadata-only `credential_input_rejected` audit event.
- [ ] Convert the service exception to the existing template-bearing API error
  shape so the browser renders a non-persistent host bubble.

### Task 3: Configurable credential warning

**Files:**
- Modify: `src/visitor_lounge/reception_settings.py`
- Modify: `src/visitor_lounge/database.py`
- Modify: `templates/admin_settings.html`
- Modify: `src/visitor_lounge/admin_app.py`
- Modify: `static/admin.js` only if its existing form serialization requires an
  explicit field.

**Interfaces:**
- Adds `credential_detected` to `ReceptionSettings` with a polite generic
  default that never echoes rejected input.
- Existing databases receive the value through the reception-settings default
  merge/migration path.

- [ ] Add the default fixed copy and persist it with the other reception
  settings.
- [ ] Add an administrator-editable textarea using generic labels rather than a
  hardcoded personal name.
- [ ] Include the field in save, reset, and response serialization paths.

### Task 4: Documentation and lightweight verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Documents the friendly refusal, terminal lock, credential precheck, audit
  privacy, and administrator unlock behavior.

- [ ] Update the operating guide without claiming enterprise-grade protection.
- [ ] Run `python -m compileall -q src/visitor_lounge` with the worktree virtual
  environment.
- [ ] Run a JavaScript syntax check for each changed `.js` file, if any.
- [ ] Run `git diff --check` and inspect `git status --short` for secrets,
  databases, logs, authentication files, and runtime files.
- [ ] Commit the bounded implementation as `feat: add lightweight visitor safety`.
