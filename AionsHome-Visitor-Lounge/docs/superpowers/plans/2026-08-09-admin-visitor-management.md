# Visitor Lounge Admin Visitor Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every visitor's complete chat easy to find, add explicit Key/ID management, support safe permanent single and bulk visitor deletion, and simplify the loopback admin layout.

**Architecture:** Extend the existing AdminService dashboard projection with message counts and latest-message previews, then centralize single/bulk deletion in one transactional service method. Reuse the existing visitor-detail timeline and Key endpoints; dashboard JavaScript adds filtering, action handling, and typed confirmation without creating another admin page.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLite foreign-key cascades, Jinja2, vanilla JavaScript and CSS.

## Global Constraints

- Modify only AionsHome-Visitor-Lounge; do not modify original AionsHome or the outer public/会客室接待照片.jpg.
- Keep admin loopback-only on 127.0.0.1:8002; do not add a port, process, login, Cloudflared instance, or public admin route.
- visitor_id is immutable and Keys cannot be reassigned between visitors.
- Permanent deletion removes all visitor-identifiable Key, session, visit, message, memory, quota, job, model-call, notification, note, and audit data.
- No deletion is performed automatically during implementation or verification.
- Use configuration-derived host_name; do not hardcode personal or companion names in UI or model-visible text.
- Verification is lightweight only: no pytest/full suite and no model call.

---

### Task 1: Add complete-chat discovery data to the dashboard

**Files:**
- Modify: src/visitor_lounge/admin_app.py

**Interfaces:**
- Produces: every dashboard visitor row includes message_count, latest_message_sender, latest_message_content, and identity_claimed.
- Consumes: existing messages table and visitor/Key/quota projections.

- [ ] **Step 1: Extend the visitor dashboard query**

Add these correlated projections beside active_key_masked:

~~~sql
(SELECT COUNT(*) FROM messages
 WHERE messages.visitor_id = visitors.id) AS message_count,
(SELECT sender FROM messages
 WHERE messages.visitor_id = visitors.id
 ORDER BY messages.rowid DESC LIMIT 1) AS latest_message_sender,
(SELECT content FROM messages
 WHERE messages.visitor_id = visitors.id
 ORDER BY messages.rowid DESC LIMIT 1) AS latest_message_content
~~~

Normalize the view-model fields:

~~~python
visitor["identity_claimed"] = visitor["display_name"] is not None
visitor["message_count"] = int(visitor["message_count"] or 0)
~~~

- [ ] **Step 2: Run a read-only projection check against a temporary database**

Create a temporary Database, insert two visitors and three messages, call AdminService.dashboard(), and assert one row reports two messages and its newest sender/content. Use tempfile.TemporaryDirectory(); never open data/visitor-lounge.sqlite3.

- [ ] **Step 3: Run syntax verification and commit**

~~~powershell
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
git add src/visitor_lounge/admin_app.py
git commit -m "feat: expose visitor chat activity in admin"
~~~

---

### Task 2: Centralize complete single and bulk visitor deletion

**Files:**
- Modify: src/visitor_lounge/admin_app.py

**Interfaces:**
- Produces: VisitorBusyForDeletion(RuntimeError).
- Produces: AdminService.delete_visitors(visitor_ids: list[str], client_host: str | None) -> int.
- Produces: POST /admin/api/visitor-cleanup with visitor_ids and confirmation.
- Reuses: AdminService.delete as a one-item wrapper.

- [ ] **Step 1: Define request and error types**

~~~python
class BulkDeleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visitor_ids: list[str] = Field(min_length=1, max_length=100)
    confirmation: str


class VisitorBusyForDeletion(RuntimeError):
    pass
~~~

- [ ] **Step 2: Implement one transactional deletion method**

Normalize IDs:

~~~python
visitor_ids = list(dict.fromkeys(str(value) for value in visitor_ids))
if not visitor_ids or len(visitor_ids) > 100:
    raise ValueError("需要选择 1 至 100 位访客")
~~~

Inside one transaction(immediate=True):

1. Select all requested visitors and reject if the number differs.
2. Reject with VisitorBusyForDeletion if any selected generation job is queued or running.
3. Delete audit_events whose visitor_id is selected so identifiable audit payloads do not survive ON DELETE SET NULL.
4. Delete the selected visitors; current foreign-key cascades remove all dependent data.
5. Insert one global visitors_deleted audit event with visitor_id NULL and details containing only {"count": N}. Do not store deleted IDs, names, Key masks, or message content.
6. Return the deleted count.

- [ ] **Step 3: Reuse this service for single deletion**

~~~python
def delete(self, visitor_id: str, client_host: str | None) -> None:
    self.delete_visitors([visitor_id], client_host)
~~~

- [ ] **Step 4: Add the bulk route**

~~~python
@app.post("/admin/api/visitor-cleanup")
async def delete_visitors(body: BulkDeleteBody, request: Request):
    if body.confirmation != "DELETE":
        raise HTTPException(status_code=409, detail="需要输入 DELETE 确认")
    try:
        deleted = service.delete_visitors(body.visitor_ids, client_host(request))
    except VisitorBusyForDeletion:
        raise HTTPException(
            status_code=409,
            detail="所选访客仍有排队或生成中的任务，请等待结束后再删除",
        ) from None
    except (VisitorNotFound, ValueError):
        raise HTTPException(status_code=404, detail="所选访客已不存在，请刷新页面") from None
    return {"ok": True, "deleted": deleted}
~~~

Translate VisitorBusyForDeletion to the same 409 response in the existing single-delete route.

- [ ] **Step 5: Verify deletion and atomic rejection using a temporary database**

Create two visitors with Keys, sessions, visits, messages, summaries, quota windows, audit events and completed jobs. Delete both and assert no visitor-linked row remains, while the global audit contains only the count. Create a queued third visitor, attempt a batch containing it and an idle visitor, assert VisitorBusyForDeletion, and assert both still exist. Never use the live database or invoke the model.

- [ ] **Step 6: Compile/check and commit**

~~~powershell
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
git add src/visitor_lounge/admin_app.py
git commit -m "feat: permanently clean up selected visitors"
~~~

---

### Task 3: Rebuild the dashboard visitor-management hierarchy

**Files:**
- Modify: templates/admin_dashboard.html
- Modify: static/admin.js
- Modify: static/lounge.css

**Interfaces:**
- Consumes: Task 1 visitor fields and Task 2 /admin/api/visitor-cleanup.
- Reuses: existing Key create, rotate, revoke, and copy-disclosure routes.
- Produces: searchable/filterable rows, Key actions, complete-chat links, and single/bulk deletion.

- [ ] **Step 1: Reorder the page and simplify hierarchy**

Make 访客管理 the first full-width panel after a compact metric strip. Move latest memory and activity below it. Add reusable CSS classes:

~~~css
.visitor-management-toolbar {}
.visitor-table-wrap { overflow-x: auto; }
.visitor-row-actions {}
.chat-preview {}
.danger-zone {}
.selection-bar {}
.admin-dialog {}
~~~

Keep the existing dark neutral/gold palette, consistent 14–18px panel radii, and readable horizontal overflow on narrow screens.

- [ ] **Step 2: Render identity, chat, and Key controls per row**

Each row gets safe search/filter fields:

~~~html
<tr data-visitor-row
    data-search="{{ ((visitor.display_name or '') ~ ' ' ~ visitor.id)|lower }}"
    data-kind="{{ visitor.visitor_kind }}">
~~~

Render a checkbox, fixed name/type/claim state, read-only ID plus copy button, Key state, message count/latest-message sender and clamped preview, status/quota, and:

~~~html
<a class="button-link compact" href="/admin/visitors/{{ visitor.id }}">
  查看全部 {{ visitor.message_count }} 条聊天
</a>
~~~

Never put message content in attributes or JavaScript. Add data-key-command buttons for copy, rotate, revoke and create, plus data-delete-one. No ID edit/rebind control is rendered.

- [ ] **Step 3: Add search, type filter, and selection bar**

Add visitor-search, visitor-kind-filter, select-visible-visitors, selection-count, and delete-selected. JavaScript filters by data-search/data-kind; select-visible affects only visible rows. Nothing is selected on load.

- [ ] **Step 4: Add one reusable typed-confirmation dialog**

Use a native dialog that lists selected visible row identity fields and message counts. Require DELETE, then:

~~~javascript
await fetch("/admin/api/visitor-cleanup", {
  method: "POST",
  cache: "no-store",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({visitor_ids: selectedIds, confirmation: "DELETE"}),
});
~~~

On success reload. On failure keep the dialog open and show the response error. Single and bulk delete open the same dialog.

- [ ] **Step 5: Handle Key actions**

Use event delegation. Copy calls copy-disclosure. Rotate requires ROTATE; revoke requires REVOKE; create uses a normal confirmation. New/rotated Keys reuse the current reveal panel and 30-second timer; never store raw Keys in data attributes.

- [ ] **Step 6: Check scripts/templates/names and commit**

~~~powershell
node --check static/admin.js
.\.venv\Scripts\python.exe -c "from pathlib import Path; from jinja2 import Environment; [Environment().parse(p.read_text(encoding='utf-8')) for p in Path('templates').glob('*.html')]; print('templates: ok')"
rg -n "Connor|Ithil|Aion" templates/admin_dashboard.html static/admin.js static/lounge.css
git diff --check
git add templates/admin_dashboard.html static/admin.js static/lounge.css
git commit -m "feat: simplify admin visitor management"
~~~

Any personal-name match must be removed unless it is configuration data rather than a reusable UI label.

---

### Task 4: Clarify visitor detail management and finish lightly

**Files:**
- Modify: templates/admin_visitor.html
- Modify: README.md

**Interfaces:**
- Reuses: existing paged 100-message timeline and detail Key/identity controls.
- Documents: immutable ID, rotate/revoke/delete semantics, complete chat access and bulk cleanup.

- [ ] **Step 1: Clarify detail-page sections**

Keep existing data and actions, but group them as:

- 身份（访客 ID 不可修改）
- Key 管理（轮换保留身份，撤销保留记录）
- 完整聊天记录
- 危险操作（永久删除全部相关数据）

Move permanent delete into the danger area and retain DELETE confirmation. Keep message pagination and source/failure badges unchanged.

- [ ] **Step 2: Update README**

Document complete paged chat access, immutable IDs, Key rotation/revocation preserving identity/history, and irreversible complete single/bulk deletion.

- [ ] **Step 3: Run the full lightweight gate**

~~~powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q src
node --check static/admin.js
.\.venv\Scripts\python.exe -c "from pathlib import Path; from jinja2 import Environment; [Environment().parse(p.read_text(encoding='utf-8')) for p in Path('templates').glob('*.html')]; print('templates: ok')"
git diff --check
git status --short
~~~

Confirm the only unrelated untracked file remains the outer public/会客室接待照片.jpg. Do not run pytest, call the model, or delete live visitors.

- [ ] **Step 4: Commit and stop before live deletion**

~~~powershell
git add templates/admin_visitor.html README.md
git commit -m "docs: clarify visitor identity and cleanup controls"
~~~

Report the controls and leave all current visitor rows untouched for administrator review.
