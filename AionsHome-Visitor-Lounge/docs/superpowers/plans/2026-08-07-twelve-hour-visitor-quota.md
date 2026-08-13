# Twelve-Hour Visitor Quota Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each visitor a 15-message quota in an independently anchored 12-hour window.

**Architecture:** Reuse the existing durable `quota_windows` table and reservation lifecycle. Change the canonical window duration and default limit, normalize recent legacy windows from their original start time, and update user-facing copy while retaining the internal `hourly_quota_limit` compatibility field.

**Tech Stack:** Python 3, FastAPI, SQLite, Jinja2.

## Global Constraints

- Keep all changes inside the Visitor Lounge worktree.
- Do not add dependencies, a second login, global Codex, or subagents.
- Do not invoke a model or run the heavy full test suite.
- Validate with syntax compilation, template loading, diff checks, service restart, and local health endpoints only.

---

### Task 1: Change the canonical quota window and defaults

**Files:**
- Modify: `src/visitor_lounge/quota.py`
- Modify: `src/visitor_lounge/reception_settings.py`
- Modify: `src/visitor_lounge/schema.sql`
- Modify: `src/visitor_lounge/database.py`

**Interfaces:**
- Consumes: `QuotaService._active_or_new_window(...)` and `_normalize_window(...)`.
- Produces: `QUOTA_WINDOW = timedelta(hours=12)` and a default per-window limit of 15.

- [x] **Step 1: Change `QUOTA_LIMIT` to 15 and `QUOTA_WINDOW` to 12 hours.**
- [x] **Step 2: Select any latest window started within the last 12 hours even if its legacy one-hour `ends_at` has passed, then normalize it to `started_at + QUOTA_WINDOW`.**
- [x] **Step 3: Change reception and new-database defaults from 10 to 15 while retaining the compatibility column name `hourly_quota_limit`.**
- [x] **Step 4: Replace internal validation errors that say “hourly” with “quota window”.**

### Task 2: Normalize live admin behavior and copy

**Files:**
- Modify: `src/visitor_lounge/admin_app.py`
- Modify: `templates/admin_settings.html`
- Modify: `src/visitor_lounge/reception_settings.py`

**Interfaces:**
- Consumes: saved `ReceptionSettings.hourly_quota_limit`.
- Produces: current-window updates that preserve counts and set `ends_at = started_at + 12 hours`.

- [x] **Step 1: Rename `_apply_hourly_quota` to `_apply_quota_window`, use a 12-hour cutoff, and normalize recent windows even when their old end time has passed.**
- [x] **Step 2: Update dashboard and visitor-detail expiry messages to “下一条消息开始新 12 小时窗口”.**
- [x] **Step 3: Update the settings label and help text to describe a 12-hour quota window.**
- [x] **Step 4: Change the default exhaustion message from “今天” to “本轮”.**

### Task 3: Documentation and lightweight verification

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-08-07-twelve-hour-visitor-quota.md`

**Interfaces:**
- Produces: documentation and verified local services consistent with the 12-hour behavior.

- [x] **Step 1: Replace hourly quota documentation with the 12-hour, default-15 rule.**
- [x] **Step 2: Run `python -m compileall -q src` and load every Jinja template.**
- [x] **Step 3: Run `git diff --check`, restart both local services, and confirm both `/healthz` endpoints return `ok`.**
- [x] **Step 4: Mark this plan complete and commit only scoped Visitor Lounge files, preserving the outer untracked reception photo.**
