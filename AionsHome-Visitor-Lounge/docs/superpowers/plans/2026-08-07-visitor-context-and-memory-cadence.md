# Visitor Context and Memory Cadence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep up to 30 recent raw messages in each visitor chat while updating the visitor's single rolling memory after every 15 new visitor messages.

**Architecture:** Preserve the existing one-memory-per-visitor pipeline and token-budget trimming. Centralize the raw-history size through `MAX_HISTORY_MESSAGES`, change the summary candidate and batch size together, and keep all persisted raw records available to the admin.

**Tech Stack:** Python 3, FastAPI, SQLite, Jinja2.

## Global Constraints

- Do not modify the original AionsHome application outside the Visitor Lounge worktree.
- Do not add dependencies, a second login, a global Codex installation, or subagents.
- Keep the chat Prompt budget at 6000 tokens; 30 raw messages is a maximum and oldest raw context may be trimmed.
- Run only lightweight syntax, template, diff, and local health checks.

---

### Task 1: Align raw chat context at 30 messages

**Files:**
- Modify: `src/visitor_lounge/prompts.py`
- Modify: `src/visitor_lounge/visitor_service.py`

**Interfaces:**
- Consumes: `PromptBuilder.chat(...)` and `MessageRepository.recent(visitor_id, limit)`.
- Produces: `MAX_HISTORY_MESSAGES = 30`, used for both Prompt construction and visitor state payloads.

- [x] **Step 1: Change `MAX_HISTORY_MESSAGES` from 10 to 30 in `prompts.py`.**
- [x] **Step 2: Import the constant in `visitor_service.py` and replace the `limit=11`, `[-10:]`, and `limit=10` history literals with `MAX_HISTORY_MESSAGES + 1`, `[-MAX_HISTORY_MESSAGES:]`, and `MAX_HISTORY_MESSAGES`.**
- [x] **Step 3: Keep the single rolling memory outside the discardable raw-history list, so token-budget trimming removes the oldest raw messages before memory.**
- [x] **Step 4: Confirm the current-message exclusion remains intact, so the Prompt receives at most 30 preceding messages plus the current message exactly once.**

### Task 2: Align rolling-memory batches at 15 visitor messages

**Files:**
- Modify: `src/visitor_lounge/background.py`
- Modify: `src/visitor_lounge/repository.py`

**Interfaces:**
- Consumes: `BackgroundRepository.summary_candidates(...)` and `next_unsummarized_visitor_messages(...)`.
- Produces: batches containing exactly 15 new visitor messages, with the existing rolling memory supplied to `PromptBuilder.summary(...)`.

- [x] **Step 1: Add `SUMMARY_BATCH_VISITOR_MESSAGES = 15` in `background.py`.**
- [x] **Step 2: Use the constant for `minimum_new`, source `limit`, and full-batch length validation.**
- [x] **Step 3: Change repository method defaults from 10 to 15 so direct callers preserve the same contract.**
- [x] **Step 4: Leave the 20-minute idle threshold, 30-minute scan interval, one-memory replacement, and no-retry behavior unchanged.**

### Task 3: Synchronize documentation and perform lightweight verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Produces: operator documentation that states 30 recent raw messages and 15 new visitor messages per rolling-memory update.

- [x] **Step 1: Update README references to the visitor page/Prompt history from 10 to 30 messages.**
- [x] **Step 2: Update the rolling-memory trigger and batch description from 10 to 15 visitor messages.**
- [x] **Step 3: Run `python -m compileall -q src`.**
- [x] **Step 4: Load the Jinja templates with `Environment(FileSystemLoader('templates'))`.**
- [x] **Step 5: Run `git diff --check`, restart the local Visitor Lounge, and confirm both `/healthz` endpoints report `ok`.**
- [x] **Step 6: Commit only the Visitor Lounge implementation and documentation files; leave the outer untracked reception photo untouched.**
