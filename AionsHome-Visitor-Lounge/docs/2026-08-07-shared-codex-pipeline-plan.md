# Visitor Lounge Shared Codex Pipeline Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the standalone Visitor Lounge reuse AionsHome's existing optimized Codex runtime without another installation, login, or authentication home.

**Architecture:** Keep the lounge's request-scoped App Server protocol adapter and safety checks. Add a lounge-owned bridge that reads AionsHome's existing local Codex path, chat environment, and command overrides, replaces only owner-facing prompt overrides with lounge-owned prompt settings, and fails closed if the shared runtime is unavailable.

**Tech Stack:** Python 3.11+, asyncio, Codex App Server JSONL, pytest, PowerShell.

## Global Constraints

- Modify files only under `AionsHome-Visitor-Lounge`.
- Never install Codex, run `codex login`, or create a lounge `CODEX_HOME`.
- Never read AionsHome chat, memory, worldbook, visitor-independent user data, or databases.
- Preserve the 500-character output cap, disabled capabilities, request timeout, independent queue, and independent database.
- Run focused tests only; allow exactly one short real message after local verification.

---

### Task 1: Shared runtime bridge

**Files:**
- Create: `src/visitor_lounge/shared_codex_runtime.py`
- Modify: `src/visitor_lounge/codex_adapter.py`
- Test: `tests/test_shared_codex_runtime.py`

**Interfaces:**
- Produces: `SharedCodexRuntime.resolve(lounge_root: Path) -> ResolvedCodexRuntime`.
- `ResolvedCodexRuntime` contains `command: tuple[str, ...]`, `environment: dict[str, str]`, and `workdir: Path`.
- `CodexAdapter.generate()` consumes the resolved command/environment while retaining its existing JSONL protocol and safety validation.

- [ ] Write failing tests proving the command uses AionsHome's local `codex.js`, retains trimming/disable overrides, substitutes lounge prompt overrides, and never invokes a global `codex` executable.
- [ ] Run `pytest tests/test_shared_codex_runtime.py -q` and confirm the new tests fail.
- [ ] Implement the minimal read-only resolver and wire it into `CodexAdapter`.
- [ ] Add a fail-closed test for missing local package/auth/helper contracts.
- [ ] Run `pytest tests/test_shared_codex_runtime.py tests/test_prompts_codex.py -q` and confirm they pass.

### Task 2: Remove the duplicate-login contract

**Files:**
- Modify: `src/visitor_lounge/settings.py`
- Modify: `scripts/runtime-common.ps1`
- Modify: `scripts/diagnose.ps1`
- Delete: `scripts/init-codex.ps1`
- Modify: `.env.example`
- Modify: `README.md`
- Test: `tests/test_settings_database.py`

**Interfaces:**
- `Settings` no longer exposes `codex_home`; it retains `root` and `codex_workdir`.
- Diagnostics report the shared AionsHome Codex package/auth as read-only prerequisites.

- [ ] Update the settings test first so `VISITOR_LOUNGE_CODEX_HOME` is neither required nor accepted as a runtime dependency.
- [ ] Run the focused settings test and confirm it fails.
- [ ] Remove lounge-specific Codex-home loading, validation, setup, and documentation.
- [ ] Update diagnostics to inspect shared runtime prerequisites without writing to them.
- [ ] Run `pytest tests/test_settings_database.py tests/test_shared_codex_runtime.py -q` and confirm they pass.

### Task 3: Focused verification and one real message

**Files:**
- Modify only if a focused failure identifies a defect in the files above.

**Interfaces:**
- Consumes the existing lounge start/stop scripts and visitor endpoint.
- Produces one recorded real response of at most 500 Unicode characters, or a concise blocked reason without retrying.

- [ ] Run `python -m compileall -q src/visitor_lounge`.
- [ ] Run only `tests/test_shared_codex_runtime.py`, `tests/test_prompts_codex.py`, `tests/test_settings_database.py`, and `tests/test_smoke.py`.
- [ ] Start the lounge once and verify ports 8001/8002.
- [ ] Send one short visitor message through the normal endpoint and record whether the shared pipeline returns a bounded response.
- [ ] Stop the lounge and verify both ports are closed.
