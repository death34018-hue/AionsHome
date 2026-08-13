# Visitor Lounge Remote MCP Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan inline, task by task, with review checkpoints. The user explicitly prohibits subagents for this project.

**Goal:** Add a Bearer-authenticated Streamable HTTP MCP entrance to the existing Visitor Lounge service while preserving one Key, one identity, one conversation, one memory, and one quota across web and MCP clients.

**Architecture:** Mount the official MCP Python SDK v2 ASGI application into the existing FastAPI visitor process, reuse the current `VisitorService`, scheduler, quota, repositories, and SQLite database, and keep protocol mapping in focused MCP modules. Add only the persistence required for visitor kind, message provenance/delivery state, and a 24-hour safety lock; keep OAuth as a separately planned second phase.

**Tech Stack:** Python 3.11+, FastAPI/Starlette, official `mcp` Python SDK 2.x, SQLite, Jinja2, vanilla JavaScript, existing shared Codex runtime.

## Global Constraints

- Work only in `F:\MyDreamWorld\trunk\AionsHome\.worktrees\visitor-lounge\AionsHome-Visitor-Lounge` on branch `codex/visitor-lounge`.
- Do not modify the original AionsHome project, the outer untracked `public/会客室接待照片.jpg`, or any global Codex installation/login.
- Do not create a second service, port, database, login, Cloudflared instance, or outbound AionsHome MCP client.
- Keep the visitor service on `127.0.0.1:8001`, the local admin on `127.0.0.1:8002`, and publish MCP at `https://visitor.aionshome.com/mcp` through the existing tunnel.
- Phase 1 supports `Authorization: Bearer <Visitor Key>` only. OAuth pairing is Phase 2 and must not be partially scaffolded here.
- MCP accepts pure text only: input is at most 500 Unicode characters and output remains at most 800 Unicode characters. No image, file, audio, attachment, resource, prompt, or link-fetching primitive is exposed.
- One `visitor_id` owns one display name, visitor kind, message line, rolling memory, 12-hour quota, visit state, and safety lock across every client.
- Display names may repeat. A claimed visitor cannot self-rename; only the loopback admin may rename or correct visitor kind.
- A malicious Connor termination locks the identity for 24 hours across web and MCP. Normal end/suspend does not lock it.
- A model/line failure is visible in admin activity, is not retried automatically, does not end the visit, does not consume quota, and does not enter future context or rolling memory.
- Use the existing global waiting limit of 3. Serialize messages for the same visitor before prompt construction so later turns see earlier replies.
- Store timestamps in UTC and render every admin timestamp in `Asia/Shanghai` as `YYYY-MM-DD HH:mm:ss`.
- Do not add or run a heavy/full test suite. Verification is limited to syntax/compile checks, dependency checks, MCP protocol discovery, non-model state checks, local health checks, and at most one real MCP model call.
- Never log, echo, store in messages, or place in a model prompt the raw Visitor Key or Bearer header.

## File Structure

### New files

- `src/visitor_lounge/turn_coordinator.py`: FIFO serialization for simultaneous web/MCP turns belonging to one visitor.
- `src/visitor_lounge/mcp_auth.py`: Visitor Key `TokenVerifier` adapter for the official MCP SDK.
- `src/visitor_lounge/mcp_service.py`: MCP-neutral tool payloads and mapping to existing lounge services.
- `src/visitor_lounge/mcp_app.py`: MCP server, six tool registrations, transport security, and ASGI app construction.
- `src/visitor_lounge/admin_time.py`: one UTC-to-`Asia/Shanghai` display formatter for admin payloads.

### Existing files modified

- `pyproject.toml`: add the official MCP SDK runtime dependency.
- `src/visitor_lounge/schema.sql`, `database.py`: additive SQLite columns and safe migration defaults.
- `src/visitor_lounge/models.py`: visitor kind/lock and message source/delivery fields.
- `src/visitor_lounge/repository.py`: identity claim/admin rename, message timeline cursor, accepted-message filters, visit end, and expiring safety lock persistence.
- `src/visitor_lounge/quota.py`: source-aware message reservation and unconditional refund for failed/no-accepted-reply generations.
- `src/visitor_lounge/scheduler.py`: source-aware host replies, failed-message marking, and 24-hour safety lock.
- `src/visitor_lounge/visitor_service.py`: source parameter, effective status resolution, terminal result waiting, and shared MCP/web state payloads.
- `src/visitor_lounge/container.py`: one shared turn coordinator reference.
- `src/visitor_lounge/visitor_app.py`: route both web sends through the coordinator, mount MCP, and compose lifespans.
- `src/visitor_lounge/admin_app.py`: typed invitation, identity edit, paged conversation data, source/status, and formatted times.
- `templates/admin_dashboard.html`, `templates/admin_visitor.html`, `static/admin.js`: visitor type selection, visible chat entry, identity edit, source/status badges, pagination, and Beijing-time labels.
- `templates/visitor_chat.html`, `static/visitor.js`: show external-AI operator identity and failed delivery state without rebuilding history.
- `README.md`: Phase 1 setup, MCP address/authentication, tool limits, 24-hour lock, and lightweight verification.

---

### Task 1: Add MCP dependency and additive schema migration

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/visitor_lounge/schema.sql`
- Modify: `src/visitor_lounge/database.py`
- Modify: `src/visitor_lounge/models.py`

**Interfaces:**
- Produces: `VisitorKind = Literal["human", "external_ai"]`
- Produces: `MessageSource = Literal["web", "mcp"]`
- Produces: `DeliveryStatus = Literal["accepted", "failed"]`
- Extends: `Message.source`, `Message.delivery_status`, `VisitorRecord.visitor_kind`, `VisitorRecord.safety_locked_until`

- [ ] **Step 1: Add the supported SDK line**

Add the plain runtime package, not the CLI extra:

```toml
dependencies = [
  "cryptography>=42.0",
  "fastapi>=0.135.0,<1",
  "jinja2>=3.1.0,<4",
  "mcp>=2.0.0,<3",
  "tiktoken>=0.12.0",
  "tzdata>=2025.2",
  "uvicorn>=0.41.0,<1",
]
```

The official v2 SDK is the current stable line and supports the 2026-07-28 protocol plus earlier revisions. Do not use the v1 `FastMCP` API in this plan.

- [ ] **Step 2: Extend fresh-database schema**

Add these columns with migration-safe defaults:

```sql
-- visitors
visitor_kind TEXT NOT NULL DEFAULT 'human'
    CHECK(visitor_kind IN ('human', 'external_ai')),
safety_locked_until TEXT,

-- messages
source TEXT NOT NULL DEFAULT 'web'
    CHECK(source IN ('web', 'mcp')),
delivery_status TEXT NOT NULL DEFAULT 'accepted'
    CHECK(delivery_status IN ('accepted', 'failed'))
```

- [ ] **Step 3: Add idempotent upgrades for existing databases**

Extend `Database.initialize()` with column introspection for `visitors` and `messages`. Existing rows must remain intact and acquire these exact values:

```python
visitor_columns = {
    # existing entries remain
    "visitor_kind": "TEXT NOT NULL DEFAULT 'human' CHECK(visitor_kind IN ('human', 'external_ai'))",
    "safety_locked_until": "TEXT",
}
message_columns = {
    "source": "TEXT NOT NULL DEFAULT 'web' CHECK(source IN ('web', 'mcp'))",
    "delivery_status": "TEXT NOT NULL DEFAULT 'accepted' CHECK(delivery_status IN ('accepted', 'failed'))",
}
```

Run each additive `ALTER TABLE` statement only when `PRAGMA table_info` reports the column missing. Do not rebuild or delete the current database.

- [ ] **Step 4: Extend immutable model types**

Use explicit aliases and defaults so existing call sites remain readable during the migration:

```python
VisitorKind = Literal["human", "external_ai"]
MessageSource = Literal["web", "mcp"]
DeliveryStatus = Literal["accepted", "failed"]

@dataclass(frozen=True)
class Message:
    id: str
    visitor_id: str
    sender: Literal["visitor", "host"]
    content: str
    created_at: datetime
    source: MessageSource = "web"
    delivery_status: DeliveryStatus = "accepted"

@dataclass(frozen=True)
class VisitorRecord:
    id: str
    display_name: str | None
    status: str
    disclosure_version: str | None = None
    visitor_kind: VisitorKind = "human"
    safety_locked_until: datetime | None = None
```

- [ ] **Step 5: Install locally and run lightweight schema checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q src
```

Expected: dependency check succeeds; compile exits 0. Do not run pytest.

- [ ] **Step 6: Commit the schema foundation**

```powershell
git add pyproject.toml src/visitor_lounge/schema.sql src/visitor_lounge/database.py src/visitor_lounge/models.py
git commit -m "feat: add MCP visitor metadata schema"
```

---

### Task 2: Implement one identity, admin-only rename, and expiring safety locks

**Files:**
- Modify: `src/visitor_lounge/repository.py`
- Modify: `src/visitor_lounge/security.py`
- Modify: `src/visitor_lounge/visitor_service.py`
- Modify: `src/visitor_lounge/scheduler.py`

**Interfaces:**
- Produces: `VisitorRepository.create_unclaimed_visitor(visitor_kind: VisitorKind = "human") -> str`
- Produces: `VisitorRepository.update_identity(visitor_id, display_name, visitor_kind) -> VisitorRecord`
- Produces: `VisitorRepository.release_expired_safety_lock(visitor_id, now) -> bool`
- Produces: `VisitorRepository.end_visit(visitor_id, ended_at, status="suspended") -> None`
- Produces: `VisitorSafetyLocked(until: datetime)`

- [ ] **Step 1: Make visitor creation type-aware**

Change the creation signature and validate before SQL:

```python
def create_unclaimed_visitor(
    self,
    visitor_kind: VisitorKind = "human",
    *,
    connection: sqlite3.Connection | None = None,
) -> str:
    if visitor_kind not in {"human", "external_ai"}:
        raise ValueError("unsupported visitor kind")
```

Insert `visitor_kind` atomically with the new visitor. Keep `visitor_id` as the only unique identity; do not add a unique index for `display_name`.

- [ ] **Step 2: Read complete visitor state and authenticate valid credentials independently of status**

Extend `_message_from_row()`/`visitor()` selections for the new fields. Split credential validity from lounge availability: a non-revoked Key must authenticate its `visitor_id` even while `paused` or `safety_lock`, so a legitimate client can receive a structured status. Revoked Keys still return no identity.

Keep the existing peppered digest and constant-time comparison in `KeyService`; only remove the visitor-status restriction from the repository candidate query.

- [ ] **Step 3: Add admin-only identity update**

Repository code must accept an already-normalized name and a validated type:

```python
def update_identity(
    self,
    visitor_id: str,
    display_name: str,
    visitor_kind: VisitorKind,
    *,
    connection: sqlite3.Connection | None = None,
) -> VisitorRecord:
    if visitor_kind not in {"human", "external_ai"}:
        raise ValueError("unsupported visitor kind")
    with self._database.transaction(immediate=True) as conn:
        updated = conn.execute(
            "UPDATE visitors SET display_name = ?, visitor_kind = ? WHERE id = ?",
            (display_name, visitor_kind, visitor_id),
        )
        if updated.rowcount != 1:
            raise VisitorNotFound(visitor_id)
    return self.visitor(visitor_id)
```

This method must never be called by visitor or MCP self-service routes. `claim_name()` remains conditional on `display_name IS NULL`, preserving first-claim atomicity.

- [ ] **Step 4: Add effective safety-lock resolution**

First add one repository operation for every normal or forced visit ending:

```python
def end_visit(
    self,
    visitor_id: str,
    ended_at: datetime,
    status: VisitorStatus = "suspended",
    *,
    connection: sqlite3.Connection | None = None,
) -> None:
    """Close the one open visit and move the visitor to the supplied status."""
```

The update must run in one immediate transaction: set `ended_at` only on the
visitor's currently open `visits` row, then update `visitors.status`. A normal
end sets `suspended` and clears `safety_locked_until`; a safety end sets
`safety_lock` and preserves the newly calculated expiry. Repeated calls must be
idempotent when there is no open visit.

Use a concrete 24-hour constant and explicit mutation:

```python
SAFETY_LOCK_DURATION = timedelta(hours=24)

def release_expired_safety_lock(self, visitor_id: str, now: datetime) -> bool:
    # UPDATE visitors SET status='active', safety_locked_until=NULL
    # WHERE id=? AND status='safety_lock' AND safety_locked_until<=?
```

When a safety action completes, the scheduler must set:

```python
locked_until = now + SAFETY_LOCK_DURATION
status = "safety_lock"
```

It must also close the open `visits` row and write `locked_until` and `job_id` into the `safety_lock` audit payload. Normal `suspend` closes the visit but leaves `safety_locked_until` null.

- [ ] **Step 5: Resolve expired locks before every user-visible state/action**

Add this service boundary:

```python
class VisitorSafetyLocked(VisitorUnavailable):
    def __init__(self, until: datetime) -> None:
        self.until = until

def effective_visitor(self, visitor_id: str) -> VisitorRecord:
    self.repository.release_expired_safety_lock(visitor_id, self.container.clock())
    return self.repository.visitor(visitor_id)
```

Use `effective_visitor()` in login recording, state, claim, begin/end visit, and send validation. A future lock raises `VisitorSafetyLocked`; an expired lock returns to `active`. Admin `unlock` must clear both status and `safety_locked_until` and write the existing unlock audit.

- [ ] **Step 6: Verify without a model call**

Run compile, then use a temporary database that cannot touch production data:

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
@'
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from visitor_lounge.database import Database
from visitor_lounge.repository import VisitorRepository

with TemporaryDirectory() as temp_dir:
    database = Database(Path(temp_dir) / "visitor.sqlite3")
    database.initialize()
    visitors = VisitorRepository(database)
    first = visitors.create_unclaimed_visitor("human")
    second = visitors.create_unclaimed_visitor("external_ai")
    visitors.update_identity(first, "Same Name", "human")
    visitors.update_identity(second, "Same Name", "external_ai")
    assert first != second
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    with database.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE visitors SET status='safety_lock', safety_locked_until=? WHERE id=?",
            ((now + timedelta(hours=24)).isoformat(), first),
        )
    assert not visitors.release_expired_safety_lock(first, now + timedelta(hours=23))
    assert visitors.release_expired_safety_lock(first, now + timedelta(hours=24))
print("identity-lock-check: ok")
'@ | .\.venv\Scripts\python.exe -
git diff --check
```

Expected: the inline check prints `identity-lock-check: ok`; all commands exit 0.

- [ ] **Step 7: Commit identity and lock semantics**

```powershell
git add src/visitor_lounge/repository.py src/visitor_lounge/security.py src/visitor_lounge/visitor_service.py src/visitor_lounge/scheduler.py
git commit -m "feat: add typed visitors and expiring safety locks"
```

---

### Task 3: Track web/MCP provenance and keep failed turns out of memory

**Files:**
- Modify: `src/visitor_lounge/models.py`
- Modify: `src/visitor_lounge/repository.py`
- Modify: `src/visitor_lounge/quota.py`
- Modify: `src/visitor_lounge/scheduler.py`
- Modify: `src/visitor_lounge/visitor_service.py`

**Interfaces:**
- Extends: `GenerationRequest.source: MessageSource`
- Produces: `MessageRepository.timeline(visitor_id, after_message_id=None, limit=30) -> list[Message]`
- Produces: `MessageRepository.mark_failed(message_id) -> bool`
- Extends: `VisitorService.send(visitor_id, request_id, text, source: MessageSource = "web")`

- [ ] **Step 1: Make every insertion explicit about provenance and delivery**

Change the shared helper to:

```python
def _insert_message(
    conn: sqlite3.Connection,
    visitor_id: str,
    sender: str,
    content: str,
    created_at: datetime,
    *,
    source: MessageSource = "web",
    delivery_status: DeliveryStatus = "accepted",
) -> Message:
    message = Message(
        id=str(uuid4()), visitor_id=visitor_id, sender=sender,
        content=content, created_at=created_at, source=source,
        delivery_status=delivery_status,
    )
    conn.execute(
        "INSERT INTO messages "
        "(id, visitor_id, sender, content, created_at, source, delivery_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (message.id, visitor_id, sender, content, _timestamp(created_at), source, delivery_status),
    )
    return message
```

All SQL selects feeding `_message_from_row()` must use the same order:

```sql
SELECT id, visitor_id, sender, content, created_at, source, delivery_status
```

- [ ] **Step 2: Separate model context from visible timeline**

Keep `MessageRepository.recent()` as the model-context method and filter it with `delivery_status = 'accepted'`. Add a timeline method that includes accepted and failed records and supports a cursor by resolving the owner-scoped `rowid` of `after_message_id`:

```python
def timeline(
    self,
    visitor_id: str,
    *,
    after_message_id: str | None = None,
    limit: int = 30,
) -> list[Message]:
    with self._database.connection() as conn:
        cursor = None
        if after_message_id is not None:
            cursor = conn.execute(
                "SELECT rowid FROM messages WHERE id = ? AND visitor_id = ?",
                (after_message_id, visitor_id),
            ).fetchone()
        if cursor is not None:
            rows = conn.execute(
                "SELECT id, visitor_id, sender, content, created_at, source, delivery_status "
                "FROM messages WHERE visitor_id = ? AND rowid > ? ORDER BY rowid LIMIT ?",
                (visitor_id, int(cursor[0]), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, visitor_id, sender, content, created_at, source, delivery_status "
                "FROM messages WHERE visitor_id = ? ORDER BY rowid DESC LIMIT ?",
                (visitor_id, limit),
            ).fetchall()
            rows = list(reversed(rows))
    return [_message_from_row(row) for row in rows]
```

An unknown or foreign cursor returns the most recent page for that visitor; it must not expose whether the cursor belongs to someone else.

- [ ] **Step 3: Carry source through quota, prompt, and host reply**

Extend these exact calls:

```python
QuotaService.reserve_message(visitor_id, request_id, content, now, source=source)
GenerationRequest(
    job_id=reservation.job_id,
    request_id=request_id,
    visitor_id=visitor_id,
    message_id=job.message_id or "",
    prompt=prompt,
    source=source,
)
VisitorService.send(visitor_id, request_id, text, source="web")
```

The scheduler inserts the host reply using `request.source`. Claim/return fixed greetings use the entry source that triggered them.

- [ ] **Step 4: Persist failed attempts but exclude them from context and memory**

When a generation ends without an accepted terminal reply:

1. set its visitor message to `delivery_status='failed'`;
2. mark the job/model call failed and keep reported usage;
3. refund the reservation exactly once even if partial text was streamed;
4. do not insert a host message from partial text;
5. do not automatically retry.

Apply this path to model exceptions, generation timeout, queue timeout, scheduler cancellation, and startup recovery of an abandoned no-reply job.

Add a quota method with exact semantics:

```python
def refund_failed_generation(self, request_id: str, reason: str) -> QuotaState:
    """Refund a failed generation once even when visible_text contains a partial stream."""
```

Do not loosen `refund_once()` for successful/visible replies; keep the exceptional path named and confined to scheduler failure handling.

- [ ] **Step 5: Filter rolling-memory queries**

Add `delivery_status = 'accepted'` to every visitor-message query used by:

- `summary_candidates()`;
- `next_unsummarized_visitor_messages()`;
- `summary_context()` bounds and range;
- any count of unsummarized visitor turns.

This preserves one 15-message rolling-memory cadence while preventing failed input from advancing it.

- [ ] **Step 6: Add source/status to shared state payloads**

Each message returned by `VisitorService.state()` must contain:

```python
{
    "id": message.id,
    "sender": message.sender,
    "content": message.content,
    "created_at": message.created_at.isoformat(),
    "source": message.source,
    "delivery_status": message.delivery_status,
}
```

Use `timeline()` for client display state and `recent()` only for Connor prompt construction.

- [ ] **Step 7: Run lightweight consistency checks and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
rg -n "FROM messages|INSERT INTO messages" src\visitor_lounge
git diff --check
```

Manually verify every message select matches `_message_from_row()` and every insert supplies/defaults source/status. Then commit:

```powershell
git add src/visitor_lounge/models.py src/visitor_lounge/repository.py src/visitor_lounge/quota.py src/visitor_lounge/scheduler.py src/visitor_lounge/visitor_service.py
git commit -m "feat: track visitor message delivery and source"
```

---

### Task 4: Serialize simultaneous turns before prompt construction

**Files:**
- Create: `src/visitor_lounge/turn_coordinator.py`
- Modify: `src/visitor_lounge/container.py`
- Modify: `src/visitor_lounge/visitor_service.py`
- Modify: `src/visitor_lounge/visitor_app.py`

**Interfaces:**
- Produces: `VisitorTurnCoordinator.submit(visitor_id, request_id, text, source) -> QueueTicket | QueueTicketSnapshot`
- Produces: `VisitorTurnCoordinator.shutdown() -> None`
- Produces: `VisitorService.wait_for_job(job_id, visitor_id) -> GenerationJobRecord`

- [ ] **Step 1: Create a focused FIFO coordinator**

Define a small `TurnSender` protocol instead of importing the concrete service back into the coordinator. Its exact async methods are `send(visitor_id: str, request_id: str, text: str, *, source: MessageSource) -> QueueTicket | QueueTicketSnapshot` and `wait_for_job(job_id: str, visitor_id: str) -> GenerationJobRecord`.

Initialize the coordinator with these concrete fields:

```python
class VisitorTurnQueueFull(RuntimeError):
    """Raised before persistence when one visitor already has three waiters."""

class VisitorTurnCoordinator:
    def __init__(self, sender: TurnSender, max_waiting_per_visitor: int = 3):
        self._sender = sender
        self._max_waiting_per_visitor = max_waiting_per_visitor
        self._lanes: dict[str, deque[TurnSubmission]] = {}
        self._request_owners: dict[str, str] = {}
        self._lane_tasks: dict[str, asyncio.Task[None]] = {}
```

For each visitor, keep one running submission plus at most three FIFO waiters. Set a queued caller's result as soon as its generation is accepted by the existing scheduler, then keep the visitor lane occupied until `wait_for_job()` reaches a terminal job. Remove empty lane state to avoid an ever-growing identity map.

- [ ] **Step 2: Deduplicate while a request is waiting**

Maintain a process-local map from `request_id` to the same submission future until persistence occurs. The same visitor/request returns that future; the same `request_id` from another visitor raises the existing `RequestConflict`. Once `VisitorService.send()` persists the job, repository idempotency becomes authoritative.

- [ ] **Step 3: Add terminal waiting to the existing service**

Implement:

```python
async def wait_for_job(
    self, job_id: str, visitor_id: str
) -> GenerationJobRecord:
    self.assert_job_owner(job_id, visitor_id)
    try:
        ticket = self.scheduler.ticket(job_id)
    except KeyError:
        return self.repository.job_by_id(job_id)
    await ticket.final()
    return self.assert_job_owner(job_id, visitor_id)
```

Terminal tickets evicted from memory fall back to SQLite. Do not poll or call the model again.

- [ ] **Step 4: Share one coordinator between web and MCP**

Add `turn_coordinator: Any | None = None` to `Container`. Construct exactly one coordinator inside `create_visitor_app()` after `VisitorService`, assign it to the container, and route `/api/messages` through `coordinator.submit(session.visitor_id, body.request_id, body.text, source="web")`.

Map `VisitorTurnQueueFull` to HTTP 409 with “这位访客已有三条消息等待处理”. Existing global `QueueFull` remains HTTP 503.

- [ ] **Step 5: Shut down without stranded callers**

In the visitor lifespan, call `await coordinator.shutdown()` before scheduler shutdown. Shutdown must fail not-yet-submitted futures with `SchedulerShuttingDown`, cancel lane workers, and never persist an unstarted message or consume quota.

- [ ] **Step 6: Verify FIFO behavior without a real model**

Run a process-local fake sender; it must observe `one`, `two`, `three` for visitor A while visitor B can start independently:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
@'
import asyncio
from types import SimpleNamespace
from visitor_lounge.turn_coordinator import VisitorTurnCoordinator

class FakeSender:
    def __init__(self):
        self.order = []
        self.gates = {}

    async def send(self, visitor_id, request_id, text, *, source):
        self.order.append((visitor_id, request_id))
        gate = asyncio.Event()
        self.gates[request_id] = gate
        return SimpleNamespace(job_id=request_id)

    async def wait_for_job(self, job_id, visitor_id):
        await self.gates[job_id].wait()
        return SimpleNamespace(id=job_id, visitor_id=visitor_id, status="completed")

async def wait_until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("coordinator did not advance")

async def main():
    sender = FakeSender()
    coordinator = VisitorTurnCoordinator(sender, max_waiting_per_visitor=3)
    one = asyncio.create_task(coordinator.submit("A", "one", "1", source="web"))
    await wait_until(lambda: ("A", "one") in sender.order)
    two = asyncio.create_task(coordinator.submit("A", "two", "2", source="mcp"))
    three = asyncio.create_task(coordinator.submit("A", "three", "3", source="web"))
    other = asyncio.create_task(coordinator.submit("B", "other", "x", source="mcp"))
    await wait_until(lambda: ("B", "other") in sender.order)
    sender.gates["other"].set()
    sender.gates["one"].set()
    await wait_until(lambda: ("A", "two") in sender.order)
    sender.gates["two"].set()
    await wait_until(lambda: ("A", "three") in sender.order)
    sender.gates["three"].set()
    await asyncio.gather(one, two, three, other)
    assert [request for visitor, request in sender.order if visitor == "A"] == ["one", "two", "three"]
    await coordinator.shutdown()
    print("turn-fifo-check: ok")

asyncio.run(main())
'@ | .\.venv\Scripts\python.exe -
git diff --check
```

Expected: the inline check prints `turn-fifo-check: ok`; compile and diff checks exit 0.

- [ ] **Step 7: Commit turn serialization**

```powershell
git add src/visitor_lounge/turn_coordinator.py src/visitor_lounge/container.py src/visitor_lounge/visitor_service.py src/visitor_lounge/visitor_app.py
git commit -m "feat: serialize shared visitor turns"
```

---

### Task 5: Build Bearer authentication and MCP tool service

**Files:**
- Create: `src/visitor_lounge/mcp_auth.py`
- Create: `src/visitor_lounge/mcp_service.py`
- Modify: `src/visitor_lounge/repository.py`
- Modify: `src/visitor_lounge/visitor_service.py`

**Interfaces:**
- Produces: `VisitorKeyTokenVerifier.verify_token(token) -> AccessToken | None`
- Produces: `McpLoungeService.get_lounge_info(visitor_id) -> dict[str, object]`
- Produces: `claim_identity`, `begin_visit`, `talk_to_host`, `get_visit_state`, `end_visit`

- [ ] **Step 1: Adapt the existing Key service to MCP authentication**

Implement the official SDK protocol:

```python
from mcp.server.auth.provider import AccessToken, TokenVerifier

class VisitorKeyTokenVerifier(TokenVerifier):
    def __init__(self, keys: KeyService) -> None:
        self._keys = keys

    async def verify_token(self, token: str) -> AccessToken | None:
        visitor_id = self._keys.authenticate(token)
        if visitor_id is None:
            return None
        return AccessToken(
            token="",
            client_id="visitor-key",
            scopes=["visitor:lounge"],
            subject=visitor_id,
            claims={"visitor_id": visitor_id},
        )
```

Never put the raw `token` into `AccessToken`, claims, logs, exceptions, or audit payloads.

- [ ] **Step 2: Centralize the authenticated principal lookup**

Use the SDK auth context, not `ctx.headers`:

```python
from mcp.server.auth.middleware.auth_context import get_access_token

def require_visitor_id() -> str:
    access = get_access_token()
    if access is None or not access.subject:
        raise RuntimeError("authenticated visitor identity missing")
    return access.subject
```

Headers are client input; only the verified access-token subject becomes identity.

- [ ] **Step 3: Implement tool-neutral lounge payloads**

`McpLoungeService` receives `Container`, `VisitorService`, `VisitorTurnCoordinator`, `VisitorRepository`, `MessageRepository`, and reception settings. Implement these exact methods:

- `get_lounge_info(visitor_id: str) -> dict[str, object]`
- `claim_identity(visitor_id: str, name: str, consent: bool) -> dict[str, object]`
- `begin_visit(visitor_id: str) -> dict[str, object]`
- `async talk_to_host(visitor_id: str, message: str, request_id: str | None) -> dict[str, object]`
- `get_visit_state(visitor_id: str, after_message_id: str | None) -> dict[str, object]`
- `end_visit(visitor_id: str) -> dict[str, object]`

No method accepts a Key, file, URL, binary data, or attachment.

- [ ] **Step 4: Enforce first-claim rules**

`claim_identity` must normalize the name through the existing `normalize_visitor_name`, enforce 1–200 Unicode characters and disclosure consent, and call the same atomic repository claim as the web flow with `source="mcp"`. If already claimed, return:

```python
{
    "status": "already_claimed",
    "visitor_name": visitor.display_name,
    "visitor_kind": visitor.visitor_kind,
    "message": "名字已经固定，如需修改请联系 Ithil。",
}
```

It must not overwrite the name or visitor kind.

- [ ] **Step 5: Implement state and visit tools without model calls**

`get_lounge_info` always returns `max_input_chars=500`, `max_output_chars=800`, `accepted_content=["text"]`, recording disclosure, claim state, visitor kind, and lounge state.

`begin_visit` records/reopens a visit with `source="mcp"`, returns the latest 30 timeline messages, quota, lock status, and only `memory_available` plus `memory_updated_at`; it does not return the owner-facing memory text.

`get_visit_state` returns messages after the owner-scoped cursor, current quota, current job, and lock/reset times without recording a new model call.

`end_visit` closes the current visit, sets normal `suspended`, and returns `visit_status="ended"`; it does not clear memory, messages, identity, or quota.

- [ ] **Step 6: Implement the one model-calling tool**

Validate before queueing:

```python
if not isinstance(message, str):
    return {"status": "invalid_message", "message": "只接受纯文本。"}
received = len(message)
if received > 500:
    return {
        "status": "message_too_long",
        "limit": 500,
        "received": received,
        "message": "本次输入超过 500 字，请压缩或拆分后重新发送。",
    }
```

An empty/whitespace message follows the existing visitor validation and does not call the model. Use a supplied 1–128 character `request_id`, or generate a server UUID. Submit with `source="mcp"`, await the terminal job once, then return Connor reply/action, `visit_status`, message IDs, UTC timestamps, quota remaining/reset, and `request_id`.

- [ ] **Step 7: Map domain errors to stable structured statuses**

Return these statuses without throwing tool-level protocol errors:

| Domain condition | MCP `status` | Calls model | Consumes quota |
|---|---|---:|---:|
| unclaimed | `identity_unclaimed` | no | no |
| lounge disabled | `lounge_closed` | no | no |
| future safety lock | `visitor_locked` | no | no |
| paused by admin | `visitor_paused` | no | no |
| quota exhausted | `quota_exhausted` | no | no |
| credential-shaped input | `credential_rejected` | no | no |
| more than three same-visitor waiters | `visitor_busy` | no | no |
| scheduler unavailable/full | `service_busy` | no | no |
| model/line failure | `generation_failed` | yes | no |
| successful reply | `ok` | yes | yes |

Include `reset_at` or `locked_until` where applicable. Do not include raw exceptions.

- [ ] **Step 8: Compile and commit the service boundary**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

Then commit:

```powershell
git add src/visitor_lounge/mcp_auth.py src/visitor_lounge/mcp_service.py src/visitor_lounge/repository.py src/visitor_lounge/visitor_service.py
git commit -m "feat: add MCP visitor tool service"
```

---

### Task 6: Register six MCP tools and mount `/mcp` into port 8001

**Files:**
- Create: `src/visitor_lounge/mcp_app.py`
- Modify: `src/visitor_lounge/visitor_app.py`
- Modify: `src/visitor_lounge/settings.py`

**Interfaces:**
- Produces: `create_mcp_server(container, coordinator) -> tuple[MCPServer, ASGIApp]`
- Mounts: exact public endpoint `/mcp`

- [ ] **Step 1: Construct an authenticated official SDK server**

Use v2 imports and a reserved Phase 2 issuer value without creating OAuth routes:

```python
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings

server = MCPServer(
    name="aionshome-visitor-lounge",
    title="AionsHome Visitor Lounge",
    version="1.0.0",
    instructions=(
        "A private text-only visitor lounge. A Visitor Key identifies exactly "
        "one visitor. Call get_lounge_info first; unclaimed visitors must call "
        "claim_identity before talking to the host. Each message is at most 500 characters."
    ),
    auth=AuthSettings(
        issuer_url="https://visitor.aionshome.com",
        resource_server_url=None,
        required_scopes=["visitor:lounge"],
    ),
    token_verifier=VisitorKeyTokenVerifier(keys),
)
```

`resource_server_url=None` deliberately avoids advertising an OAuth discovery flow in Phase 1.

- [ ] **Step 2: Register only the six approved tools**

Use `@server.tool()` and typed scalar parameters. Register exactly:

```text
get_lounge_info
claim_identity
begin_visit
talk_to_host
get_visit_state
end_visit
```

Do not register MCP resources or prompts. The `talk_to_host` docstring must contain the exact 500-character and pure-text warning so the visiting AI sees it during tool discovery.

- [ ] **Step 3: Configure transport security for local and Cloudflare hosts**

Use `TransportSecuritySettings` with this explicit allowlist:

```python
allowed_hosts = [
    "127.0.0.1:*",
    "localhost:*",
    "visitor.aionshome.com",
]
allowed_origins = [
    "http://127.0.0.1:*",
    "http://localhost:*",
    "https://visitor.aionshome.com",
]
```

Phase 1 does not enable permissive cross-origin browser CORS. Native/server MCP clients do not require it, and OAuth/browser pairing belongs to Phase 2.

- [ ] **Step 4: Build the ASGI app before lifespan entry**

Call:

```python
mcp_asgi = server.streamable_http_app(
    json_response=True,
    stateless_http=True,
    max_request_body_size=16 * 1024,
    transport_security=transport_security,
)
```

The official SDK creates a default `/mcp` route inside this ASGI app.

- [ ] **Step 5: Compose the MCP session-manager lifespan**

Inside the top-level visitor FastAPI lifespan, enter `server.session_manager.run()` after building `mcp_asgi` and before yielding. Preserve current startup recovery, generation scheduler, and background coordinator ordering. Shutdown order is:

1. turn coordinator;
2. background coordinator;
3. generation scheduler;
4. MCP session manager exits through its context manager.

- [ ] **Step 6: Mount without changing ports or routes**

After registering all existing FastAPI routes, mount the MCP ASGI app last:

```python
app.mount("/", mcp_asgi, name="mcp")
```

Because existing routes are registered first and the SDK app contains `/mcp`, this produces the exact endpoint `/mcp` while leaving `/`, `/api/*`, `/static/*`, and `/healthz` reachable. Do not mount a second uvicorn process.

- [ ] **Step 7: Perform protocol discovery only**

With services running and a valid test Key held only in a process environment variable, run:

```powershell
# Before this command, set VISITOR_MCP_SMOKE_KEY in this PowerShell process
# from one temporary Key; do not place the Key in this file or shell history.
@'
import asyncio
import os
from mcp import ClientSession
from mcp.client.streamable_http import (
    create_mcp_http_client,
    streamable_http_client,
)

EXPECTED = {
    "get_lounge_info", "claim_identity", "begin_visit",
    "talk_to_host", "get_visit_state", "end_visit",
}

async def main():
    headers = {"Authorization": "Bearer " + os.environ["VISITOR_MCP_SMOKE_KEY"]}
    http_client = create_mcp_http_client(headers=headers)
    async with http_client:
        async with streamable_http_client(
            "http://127.0.0.1:8001/mcp", http_client=http_client
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == EXPECTED
                result = await session.call_tool("get_lounge_info", {})
                payload = result.structured_content
                assert payload["max_input_chars"] == 500
                assert payload["max_output_chars"] == 800
                assert payload["accepted_content"] == ["text"]
    print("mcp-discovery-check: ok")

asyncio.run(main())
'@ | .\.venv\Scripts\python.exe -
Remove-Item Env:\VISITOR_MCP_SMOKE_KEY
```

Expected: `mcp-discovery-check: ok`. Do not call `talk_to_host` in this step.

Also verify:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/healthz
Invoke-RestMethod http://127.0.0.1:8002/healthz
```

Expected: both existing health responses remain unchanged.

- [ ] **Step 8: Commit the mounted MCP endpoint**

```powershell
git add src/visitor_lounge/mcp_app.py src/visitor_lounge/visitor_app.py src/visitor_lounge/settings.py
git commit -m "feat: mount authenticated remote MCP endpoint"
```

---

### Task 7: Make visitor type and admin-only identity editing usable

**Files:**
- Modify: `src/visitor_lounge/admin_app.py`
- Modify: `templates/admin_dashboard.html`
- Modify: `templates/admin_visitor.html`
- Modify: `static/admin.js`

**Interfaces:**
- Produces: `InvitationBody.visitor_kind`
- Produces: `VisitorIdentityBody.name`, `VisitorIdentityBody.visitor_kind`
- Adds: `PUT /admin/api/visitors/{visitor_id}/identity`

- [ ] **Step 1: Add strict admin request models**

```python
class InvitationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visitor_kind: Literal["human", "external_ai"]

class VisitorIdentityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    visitor_kind: Literal["human", "external_ai"]
```

- [ ] **Step 2: Require a type when creating invitations**

Change `AdminService.create_invitation` to accept `visitor_kind` and pass it to `create_unclaimed_visitor`. Add the selected type to the `visitor_invited` audit details. Change `POST /admin/api/invitations` to accept `InvitationBody`.

- [ ] **Step 3: Add audited admin rename/type correction**

Normalize the submitted name with the existing reserved-name rules, call `update_identity`, and write `visitor_identity_updated` with old/new name and old/new kind. The route remains protected by the existing loopback/origin middleware.

- [ ] **Step 4: Update dashboard invitation UI**

Replace the single create button with a small selector defaulting to “人类访客” and a second option “外部 AI”. JavaScript sends:

```javascript
body: JSON.stringify({visitor_kind: kindSelect.value}),
headers: {"Content-Type": "application/json"}
```

After creation, the reveal panel displays the selected type and the existing 30-second Key hiding behavior remains unchanged.

- [ ] **Step 5: Update visitor detail identity UI**

Display name, internal ID, human/external-AI badge, claim state, and safety unlock time. Add a local-only edit form for name and kind. Successful save reloads the detail page; duplicate display names are accepted.

- [ ] **Step 6: Make chat discoverability explicit**

Keep the existing visitor-name link and add visible wording such as “查看聊天与记忆” in each dashboard visitor row. Do not create a second management page.

- [ ] **Step 7: Compile/check and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
node --check static/admin.js
git diff --check
```

Then commit:

```powershell
git add src/visitor_lounge/admin_app.py templates/admin_dashboard.html templates/admin_visitor.html static/admin.js
git commit -m "feat: manage human and AI visitor identities"
```

---

### Task 8: Add paged conversation provenance and Beijing-time rendering

**Files:**
- Create: `src/visitor_lounge/admin_time.py`
- Modify: `src/visitor_lounge/admin_app.py`
- Modify: `templates/admin_dashboard.html`
- Modify: `templates/admin_visitor.html`
- Modify: `templates/visitor_chat.html`
- Modify: `static/admin.js`
- Modify: `static/visitor.js`

**Interfaces:**
- Produces: `format_admin_timestamp(value, timezone_name) -> str | None`
- Extends: `AdminService.visitor_detail(visitor_id, message_page=1, message_page_size=100)`

- [ ] **Step 1: Create one timezone formatter**

```python
def format_admin_timestamp(
    value: object,
    timezone_name: str = "Asia/Shanghai",
) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S")
```

Invalid values return `None`; they do not leak parsing exceptions into the admin page.

- [ ] **Step 2: Format only after calculations**

Keep raw UTC values while calculating latency, daily boundaries, expiration, and quota. At the final presentation boundary, format these keys wherever present:

```text
created_at, completed_at, started_at, finished_at, updated_at,
expires_at, revoked_at, ends_at, reset_at, last_activity_at,
resource_checked_at, disclosure_consented_at, safety_locked_until
```

Return `timezone_name="Asia/Shanghai"` and label pages “北京时间”. Do not change database values or Windows time.

- [ ] **Step 3: Page complete conversation records**

Change visitor-detail message loading to newest-page-first, 100 records per page, then reverse the selected page for chronological reading. Return:

```python
{
    "messages": page_messages,
    "message_page": page,
    "message_pages": max(1, math.ceil(total / 100)),
    "message_total": total,
}
```

The route accepts `?message_page=N`, clamps values below 1 to 1, and redirects/clamps past the last page. No message is deleted or hidden from admin; paging only limits rendering cost.

- [ ] **Step 4: Show source and delivery state**

Each chat bubble displays:

- `网页` for `source=web`;
- `MCP` for `source=mcp`;
- a red `发送失败` badge for `delivery_status=failed`.

Failed visitor content is visible to the owner but is explicitly marked as excluded from Connor context and rolling memory.

- [ ] **Step 5: Preserve web chat append behavior**

Update `static/visitor.js` to render `source` and `delivery_status` on newly synchronized messages without clearing and rebuilding the entire message list. An external-AI identity displays a quiet banner: “你正在以「名称 · 外部 AI」身份操作，记录归入该身份。”

- [ ] **Step 6: Check admin and visitor scripts/templates**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
node --check static/admin.js
node --check static/visitor.js
git diff --check
```

Open the existing local admin visitor detail and confirm the visible chat section, source badges, pagination controls, and Beijing-time values. Do not send a model message.

- [ ] **Step 7: Commit admin visibility/time fixes**

```powershell
git add src/visitor_lounge/admin_time.py src/visitor_lounge/admin_app.py templates/admin_dashboard.html templates/admin_visitor.html templates/visitor_chat.html static/admin.js static/visitor.js
git commit -m "feat: show sourced visitor chats in Beijing time"
```

---

### Task 9: Document, lightly verify, and stop before OAuth

**Files:**
- Modify: `README.md`
- Review only: `scripts/start.ps1`, `scripts/status.ps1`, `scripts/stop.ps1`, `scripts/diagnose.ps1`

**Interfaces:**
- Documents: `https://visitor.aionshome.com/mcp`
- Documents: six tools, Bearer Key storage, 500/800 limits, one identity line, 24-hour safety lock, and Phase 2 OAuth boundary

- [ ] **Step 1: Update operating documentation**

Add a “Remote MCP（第一阶段）” section that states:

```text
URL: https://visitor.aionshome.com/mcp
Authentication: Authorization: Bearer <Visitor Key>
Transport: Streamable HTTP
Content: pure text only, 500 input characters, 800 output characters
Identity: one Key = one visitor identity, one conversation, one memory, one quota
Tools: get_lounge_info, claim_identity, begin_visit, talk_to_host,
       get_visit_state, end_visit
```

Explain that a Key must be configured in the MCP client, never pasted into a chat message/tool argument, and that official-client confirmations or plan restrictions cannot be bypassed by the server.

- [ ] **Step 2: Correct existing safety and admin wording**

Replace “安全锁只能由管理员解锁” with the approved 24-hour automatic lock plus optional early admin unlock. State that the admin visitor-name link opens the complete paged chat timeline and all shown times are Beijing time.

- [ ] **Step 3: Confirm operational scripts need no second process**

Read the four scripts and verify they still start/stop only visitor 8001 and admin 8002. Do not add a third PID, port, window, shortcut, or Cloudflared process. If no script change is needed, leave scripts untouched.

- [ ] **Step 4: Run the complete lightweight static gate**

Run:

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q src
node --check static/admin.js
node --check static/visitor.js
git diff --check
```

Expected: every command exits 0. Do not run pytest.

- [ ] **Step 5: Run no-model black-box checks**

After a normal service restart, verify:

1. `/healthz` on 8001 and 8002 remains healthy;
2. `/mcp` without a Key returns 401 and does not reveal visitor existence;
3. a valid unclaimed external-AI Key lists exactly six tools;
4. `get_lounge_info` reports 500/800 and text-only;
5. `claim_identity` fixes a name and repeating it cannot rename;
6. the same Key from web and MCP reads the same recent timeline;
7. a 501-character `talk_to_host` returns `message_too_long`, makes no model call, and consumes no quota;
8. admin displays the claimed type/name, MCP source, full content, and Beijing time;
9. stopping with the existing shortcut closes 8001/8002 and leaves shared Cloudflared untouched.

- [ ] **Step 6: Perform at most one real MCP chat smoke call**

Only if the shared Codex line is healthy and the user approves spending one call, send one short message through `talk_to_host`. Confirm:

- reply is no more than 800 Unicode characters;
- the message appears as MCP source in admin;
- one quota unit is consumed;
- one model activity row appears;
- `get_visit_state` returns the same reply;
- no automatic retry occurs.

Otherwise record this step as `SKIPPED: shared model call intentionally deferred`; do not substitute repeated attempts.

- [ ] **Step 7: Inspect the final scope and commit documentation**

Run:

```powershell
git status --short
git diff --stat HEAD~8..HEAD
```

Confirm no database, log, `.env`, `.runtime`, original AionsHome file, or outer photo is staged. Then commit README only if changed since the previous task:

```powershell
git add README.md
git commit -m "docs: explain remote MCP visitor access"
```

Stop here. Do not begin OAuth pairing, an outbound AionsHome visitor client, or additional UI work.

## Phase 1 Completion Criteria

- `https://visitor.aionshome.com/mcp` is served by the existing 8001 process and accepts standard Streamable HTTP MCP clients with a Visitor Key Bearer header.
- Exactly six text-only tools are exposed; only `talk_to_host` can call the model.
- An unclaimed Key must choose one fixed name before chatting; type is selected by the owner and display names may repeat.
- Web and MCP share the same `visitor_id`, recent 30-message window, rolling memory, 12-hour quota, and complete owner-visible history.
- Same-visitor concurrent submissions are FIFO before prompt construction; no prompt skips an earlier reply.
- Invalid/overlong input does not call the model; failed generation does not auto-retry, consume quota, or enter future context/memory.
- Safety termination locks every entrance for 24 hours and exposes a structured unlock time to the valid Key holder and admin.
- Admin can find full paged chat content, distinguish web/MCP and failed delivery, edit fixed identity fields, and see Beijing time everywhere.
- Existing startup/stop behavior, ports, shared Cloudflared, original AionsHome, and local admin boundary remain unchanged.
- OAuth remains explicitly deferred to a second design/implementation plan.
