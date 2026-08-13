# Visitor Lounge Admin Reception Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add lightweight global Connor reception settings, one-to-one Key management, 30-minute visits, editable no-cost templates, and clearer admin controls without changing AionsHome.

**Architecture:** Store the single reception configuration in the Lounge SQLite database and access it through one focused repository. The admin process edits it transactionally; the visitor process reads the current values at each relevant request. Existing visitor, quota, scheduler, and admin boundaries remain intact.

**Tech Stack:** Python 3.14, FastAPI, SQLite, Jinja2, vanilla JavaScript, pytest.

## Global Constraints

- Modify only `AionsHome-Visitor-Lounge` inside the existing isolated worktree.
- Keep visitor and admin services loopback-only in this phase.
- Reuse AionsHome's existing local Codex runtime and login; install no CLI or duplicate auth profile.
- Exactly one active Key per visitor.
- Idle suspension and return greeting threshold is exactly 30 minutes.
- Templates never consume chat quota; a real Codex call consumes quota only after visible generated text exists.
- Run only task-focused tests plus one final local black-box flow with at most one real Codex message.

---

### Task 1: Reception settings persistence

**Files:**
- Create: `src/visitor_lounge/reception_settings.py`
- Modify: `src/visitor_lounge/schema.sql`
- Modify: `src/visitor_lounge/database.py`
- Test: `tests/test_reception_settings.py`

**Interfaces:**
- Produces `ReceptionSettings` dataclass.
- Produces `ReceptionSettingsRepository(database, root).get()`, `.save(candidate)`, and `.restore_defaults()`.
- Produces `DEFAULT_*` template constants and validates allowed `{访客名字}` placeholders and bounded field sizes.

- [ ] **Step 1: Write failing persistence and validation tests**

```python
settings = repository.get()
assert settings.idle_minutes == 30
saved = repository.save(replace(settings, first_welcome="你好，{访客名字}"))
assert repository.get() == saved
with pytest.raises(InvalidReceptionSettings):
    repository.save(replace(settings, first_welcome="你好，{未知字段}"))
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `python -m pytest -q tests/test_reception_settings.py`

- [ ] **Step 3: Add the singleton table, defaults, validation, and transactional repository**

```sql
CREATE TABLE IF NOT EXISTS reception_settings (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    persona_text TEXT NOT NULL,
    first_welcome TEXT NOT NULL,
    returning_welcome TEXT NOT NULL,
    quota_exhausted TEXT NOT NULL,
    unsafe_request TEXT NOT NULL,
    input_too_long TEXT NOT NULL,
    lounge_closed TEXT NOT NULL,
    system_unavailable TEXT NOT NULL,
    lounge_enabled INTEGER NOT NULL CHECK(lounge_enabled IN (0, 1)),
    idle_minutes INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
```

- [ ] **Step 4: Run the focused test and commit**

Run: `python -m pytest -q tests/test_reception_settings.py`

Commit: `feat: persist lounge reception settings`

### Task 2: Admin reception settings page

**Files:**
- Modify: `src/visitor_lounge/admin_app.py`
- Create: `templates/admin_settings.html`
- Modify: `templates/admin_dashboard.html`
- Modify: `templates/admin_visitor.html`
- Modify: `static/admin.js`
- Modify: `static/lounge.css`
- Test: `tests/test_admin_app.py`

**Interfaces:**
- Adds `GET /admin/settings`.
- Adds `PUT /admin/api/settings` with all-or-nothing validation.
- Adds `POST /admin/api/settings/restore-defaults`.

- [ ] **Step 1: Add failing API and rendered-page tests**

```python
page = client.get("/admin/settings")
assert page.status_code == 200
saved = client.put("/admin/api/settings", json={**payload, "idle_minutes": 30})
assert saved.status_code == 200
assert client.get("/admin/settings").text.count("再次见到你") == 1
```

- [ ] **Step 2: Run only the new admin tests and verify failure**

Run: `python -m pytest -q tests/test_admin_app.py -k reception_settings`

- [ ] **Step 3: Implement the form, navigation, save/reset behavior, and unsaved-change warning**

Use one page with one save button; do not add a frontend framework. Preserve form values on validation errors and show the last update timestamp.

- [ ] **Step 4: Run focused tests and commit**

Run: `python -m pytest -q tests/test_admin_app.py -k reception_settings`

Commit: `feat: add reception settings admin page`

### Task 3: Strict one-to-one Key controls and dashboard visibility

**Files:**
- Modify: `src/visitor_lounge/schema.sql`
- Modify: `src/visitor_lounge/database.py`
- Modify: `src/visitor_lounge/security.py`
- Modify: `src/visitor_lounge/admin_app.py`
- Modify: `templates/admin_dashboard.html`
- Modify: `static/admin.js`
- Test: `tests/test_security.py`
- Test: `tests/test_admin_app.py`

**Interfaces:**
- Enforces a partial unique index on active keys per visitor.
- `KeyService.create()` atomically revokes an existing active Key before inserting the replacement.
- Dashboard rows expose only the masked Key and use the existing disclosure endpoint for copying.

- [ ] **Step 1: Write failing one-active-Key and dashboard-copy tests**

```python
first = keys.create(visitor_id)
second = keys.create(visitor_id)
assert keys.authenticate(first.value) is None
assert keys.authenticate(second.value) == visitor_id
assert repository.active_key_count(visitor_id) == 1
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest -q tests/test_security.py tests/test_admin_app.py -k "one_active_key or dashboard_key"`

- [ ] **Step 3: Normalize legacy active keys, add the unique index, and wire dashboard copy/revoke actions**

When upgrading an existing database, keep the newest active Key and revoke older active Keys before creating the unique index.

- [ ] **Step 4: Run focused tests and commit**

Run: `python -m pytest -q tests/test_security.py tests/test_admin_app.py -k "one_active_key or dashboard_key"`

Commit: `feat: simplify visitor key management`

### Task 4: First and returning greetings with 30-minute visits

**Files:**
- Modify: `src/visitor_lounge/repository.py`
- Modify: `src/visitor_lounge/background.py`
- Modify: `src/visitor_lounge/visitor_app.py`
- Modify: `src/visitor_lounge/visitor_service.py`
- Test: `tests/test_visitor_app.py`
- Test: `tests/test_background.py`

**Interfaces:**
- Adds one transaction-safe operation for claim plus first greeting.
- Adds one transaction-safe operation for opening a new visit plus returning greeting.
- `BackgroundCoordinator.suspend_idle_visits()` reads `idle_minutes=30` from reception settings.

- [ ] **Step 1: Add failing welcome lifecycle tests**

```python
claim = client.post("/api/claim", json={"name": "朋友甲", "consent": True})
assert claim.status_code == 200
assert recent_host_messages() == ["欢迎，朋友甲。这里是小鬣狗家的会客室……"]
refresh_state_twice()
assert len(recent_host_messages()) == 1
clock.advance(minutes=30)
resume_with_same_key()
assert len(recent_host_messages()) == 2
```

- [ ] **Step 2: Run focused visitor/background tests and verify failure**

Run: `python -m pytest -q tests/test_visitor_app.py tests/test_background.py -k "welcome or idle_visit"`

- [ ] **Step 3: Implement atomic greeting insertion and the exact 30-minute boundary**

Refreshing an open visit must not add a message. Both greetings are stored as `sender='host'` and never touch quota tables.

- [ ] **Step 4: Run focused tests and commit**

Run: `python -m pytest -q tests/test_visitor_app.py tests/test_background.py -k "welcome or idle_visit"`

Commit: `feat: add configurable visitor greetings`

### Task 5: Dynamic persona, templates, and lounge switch

**Files:**
- Modify: `src/visitor_lounge/visitor_service.py`
- Modify: `src/visitor_lounge/visitor_app.py`
- Modify: `src/visitor_lounge/scheduler.py`
- Modify: `static/visitor.js`
- Test: `tests/test_visitor_app.py`
- Test: `tests/test_scheduler.py`
- Test: `tests/test_prompts_codex.py`

**Interfaces:**
- Prompt construction reads the latest global `persona_text` immediately before building a chat prompt.
- API rejections include `template_text` for the visitor UI.
- A disabled lounge rejects new messages without affecting login, state, history, or admin.
- A real `safety_lock` displays the configured fixed template while preserving usage and consuming one confirmed visible generation.

- [ ] **Step 1: Add failing dynamic-persona, no-cost-template, and disabled-lounge tests**

```python
admin_save(persona_text="新的全局接待人设")
send_message("你好")
assert "新的全局接待人设" in adapter.prompts[-1]
before = quota_state()
rejected = send_overlong_message()
assert rejected.json()["detail"]["template_text"] == configured.input_too_long
assert quota_state() == before
```

- [ ] **Step 2: Run the three focused files with a narrow expression and verify failure**

Run: `python -m pytest -q tests/test_visitor_app.py tests/test_scheduler.py tests/test_prompts_codex.py -k "dynamic_persona or template_reply or lounge_disabled or safety_template"`

- [ ] **Step 3: Implement current-setting reads and render template responses as non-persistent host bubbles**

Do not save repeated rejection templates in `messages`. First/returning greetings remain the only persisted templates.

- [ ] **Step 4: Run focused tests and commit**

Run: `python -m pytest -q tests/test_visitor_app.py tests/test_scheduler.py tests/test_prompts_codex.py -k "dynamic_persona or template_reply or lounge_disabled or safety_template"`

Commit: `feat: apply live reception templates`

### Task 6: Lightweight admin metrics, documentation, and final verification

**Files:**
- Modify: `src/visitor_lounge/admin_app.py`
- Modify: `templates/admin_dashboard.html`
- Modify: `README.md`
- Test: `tests/test_admin_app.py`

**Interfaces:**
- Dashboard reports enabled state, active generation count, queue depth, today's completed/failed jobs, and reported input/output tokens.

- [ ] **Step 1: Add one focused dashboard metrics test**

```python
dashboard = client.get("/admin")
assert dashboard.status_code == 200
for label in ("会客室开放", "正在生成", "排队", "今日完成", "今日失败"):
    assert label in dashboard.text
```

- [ ] **Step 2: Implement metrics and update the local operating guide**

Document settings, Key rotation, 30-minute visits, no-cost templates, start/stop, and the fact that公网 is still disabled.

- [ ] **Step 3: Run bounded verification**

Run the tests changed by Tasks 1-6 only, once, in one pytest invocation. Run `python -m compileall -q src/visitor_lounge` and `git diff --check`.

- [ ] **Step 4: Run one browser black-box flow and commit**

Create or reuse one local test visitor, confirm first welcome, send at most one real Codex message, confirm one quota unit is consumed, inspect the admin record, then stop the Lounge.

Commit: `feat: complete lightweight lounge administration`
