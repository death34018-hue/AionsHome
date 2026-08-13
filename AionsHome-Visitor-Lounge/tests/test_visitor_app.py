from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timezone
import shutil
import subprocess

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from visitor_lounge.container import Container
from visitor_lounge.models import GenerationChunk
from visitor_lounge.repository import MessageRepository, VisitorRepository
from visitor_lounge.quota import QuotaService
from visitor_lounge.reception_settings import ReceptionSettingsRepository
from visitor_lounge.scheduler import ResourceGate
from visitor_lounge.security import KeyService, SessionService
from visitor_lounge.settings import Settings
from visitor_lounge.visitor_app import create_visitor_app


NOW = datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)


class RecordingAdapter:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.action = "continue"

    async def generate(self, prompt: str) -> AsyncIterator[GenerationChunk]:
        self.prompts.append(prompt)
        yield GenerationChunk(kind="text", text="欢迎。")
        yield GenerationChunk(
            kind="usage",
            usage={"input_tokens": 23, "output_tokens": 4},
        )
        yield GenerationChunk(kind="completed", action=self.action)


@pytest.fixture
def lounge(tmp_path, database):
    root = tmp_path / "AionsHome-Visitor-Lounge"
    (root / "config").mkdir(parents=True)
    (root / "config" / "persona.md").write_text("温和接待访客。", encoding="utf-8")
    settings = Settings(
        root=root,
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
        codex_workdir=root / ".runtime" / "codex-workdir",
        host_display_name="林间接待人",
        reserved_visitor_names=("主人保留名", "林间接待人"),
        recording_disclosure="这段对话会被记录，以便保持交流连续。",
        recording_disclosure_version="2026-08-06",
    )
    database.initialize()
    repository = VisitorRepository(database)
    keys = KeyService(repository, settings)
    sessions = SessionService(repository, settings)
    adapter = RecordingAdapter()
    container = Container(
        settings=settings,
        database=database,
        codex_adapter=adapter,
        clock=lambda: NOW,
        resource_sampler=lambda: type(
            "Sample", (), {"cpu_percent": 0.0, "available_memory_bytes": 3 * 1024**3}
        )(),
    )
    app = create_visitor_app(container)
    with TestClient(app) as client:
        yield {
            "client": client,
            "repository": repository,
            "messages": MessageRepository(database),
            "keys": keys,
            "sessions": sessions,
            "adapter": adapter,
            "database": database,
            "settings": settings,
        }


def _identity(lounge, *, name: str | None = "访客甲") -> tuple[str, str, str]:
    visitor_id = lounge["repository"].create_unclaimed_visitor()
    if name is not None:
        lounge["repository"].claim_name(visitor_id, name, "test-disclosure")
    key = lounge["keys"].create(visitor_id).value
    cookie = lounge["sessions"].issue(visitor_id, "test-device")
    return visitor_id, key, cookie


def test_visitor_app_has_no_admin_routes(lounge):
    client = lounge["client"]
    paths = set(client.get("/openapi.json").json()["paths"])
    assert all(not path.startswith("/admin") for path in paths)
    assert client.get("/admin").status_code == 404


def test_send_ignores_submitted_visitor_id(lounge):
    visitor_a, _, cookie = _identity(lounge, name="访客甲")
    visitor_b, _, _ = _identity(lounge, name="访客乙")

    response = lounge["client"].post(
        "/api/messages",
        headers={"Cookie": f"visitor_session={cookie}"},
        json={
            "request_id": "r1",
            "visitor_id": visitor_b,
            "text": "hello",
        },
    )

    assert response.status_code == 202
    assert lounge["repository"].job("r1").visitor_id == visitor_a


def test_input_limits_are_server_side(lounge):
    _, _, cookie = _identity(lounge)

    response = lounge["client"].post(
        "/api/messages",
        headers={"Cookie": f"visitor_session={cookie}"},
        json={"request_id": "r2", "text": "好" * 501},
    )

    assert response.status_code == 422
    assert lounge["messages"].recent(lounge["sessions"].resolve(cookie).visitor_id) == []


def test_login_claim_cookie_and_generic_denial(lounge):
    visitor_id, key, _ = _identity(lounge, name=None)
    client = lounge["client"]

    logged_in = client.post(
        "/api/login",
        json={"key": key, "device_id": "new-device"},
    )

    assert logged_in.status_code == 200
    assert logged_in.json()["next"] == "claim"
    cookie = logged_in.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" not in cookie

    denied = client.post("/api/claim", json={"name": "新访客", "consent": False})
    assert denied.status_code == 422
    assert lounge["repository"].visitor(visitor_id).display_name is None

    claimed = client.post("/api/claim", json={"name": "  新访客  ", "consent": True})
    assert claimed.status_code == 200
    record = lounge["repository"].visitor(visitor_id)
    assert record.display_name == "新访客"
    assert record.disclosure_version == "2026-08-06"

    paused_id, paused_key, _ = _identity(lounge, name="暂停访客")
    with lounge["database"].transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE visitors SET status = 'paused' WHERE id = ?", (paused_id,)
        )
    unknown = client.post("/api/login", json={"key": "unknown", "device_id": "x"})
    paused = client.post("/api/login", json={"key": paused_key, "device_id": "x"})
    assert unknown.status_code == paused.status_code == 401
    assert unknown.json() == paused.json()
    assert key not in logged_in.text + denied.text + claimed.text


def test_first_claim_and_return_after_suspension_add_one_no_cost_welcome(lounge):
    visitor_id = lounge["repository"].create_unclaimed_visitor()
    key = lounge["keys"].create(visitor_id).value
    client = lounge["client"]

    assert client.post("/api/login", json={"key": key, "device_id": "first"}).status_code == 200
    claimed = client.post("/api/claim", json={"name": "朋友甲", "consent": True})
    assert claimed.status_code == 200
    first_state = client.get("/api/state").json()
    assert [m["content"] for m in first_state["messages"]] == [
        "欢迎，朋友甲。这里是小鬣狗家的会客室，我是 Connor，由我来接待你。有什么想和我聊聊的吗？"
    ]
    assert first_state["quota"]["remaining"] == 10

    lounge["repository"].set_status(visitor_id, "suspended")
    with lounge["database"].transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE visits SET ended_at = ? WHERE visitor_id = ? AND ended_at IS NULL",
            (NOW.isoformat(), visitor_id),
        )
    assert lounge["repository"].visitor(visitor_id).status == "suspended"
    assert client.post("/api/login", json={"key": key, "device_id": "return"}).status_code == 200
    assert lounge["repository"].visitor(visitor_id).status == "active"
    returned = client.get("/api/state").json()
    assert [m["content"] for m in returned["messages"]][-1] == (
        "再次见到你很开心，朋友甲。今天想和我聊些什么？"
    )
    assert returned["quota"]["remaining"] == 10


def test_idle_suspended_visitor_login_resumes_without_resetting_history_or_quota(
    lounge,
):
    visitor_id, key, _ = _identity(lounge, name="回来访客")
    lounge["messages"].append(
        visitor_id,
        "visitor",
        "先前保留的消息",
        created_at=NOW,
    )
    with lounge["database"].transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO quota_windows
                (id, visitor_id, limit_count, used_count, reserved_count,
                 started_at, ends_at)
            VALUES ('kept-window', ?, 10, 1, 0, ?, ?)
            """,
            (visitor_id, NOW.isoformat(), NOW.replace(day=7).isoformat()),
        )
        connection.execute(
            "UPDATE visitors SET status = 'suspended' WHERE id = ?",
            (visitor_id,),
        )

    logged_in = lounge["client"].post(
        "/api/login", json={"key": key, "device_id": "returning-device"}
    )
    state = lounge["client"].get("/api/state")

    assert logged_in.status_code == 200
    assert lounge["repository"].visitor(visitor_id).status == "active"
    assert state.status_code == 200
    assert state.json()["quota"]["remaining"] == 9
    assert [item["content"] for item in state.json()["messages"]] == [
        "先前保留的消息"
    ]


def test_chat_state_is_configured_scoped_and_limited_to_ten_messages(lounge):
    visitor_id, _, cookie = _identity(lounge, name="配置访客")
    for number in range(12):
        lounge["messages"].append(
            visitor_id,
            "visitor" if number % 2 == 0 else "host",
            f"message-{number}",
            created_at=NOW.replace(microsecond=number),
        )

    response = lounge["client"].get(
        "/api/state", headers={"Cookie": f"visitor_session={cookie}"}
    )

    assert response.status_code == 200
    state = response.json()
    assert state["host_name"] == "林间接待人"
    assert state["visitor_name"] == "配置访客"
    assert [message["content"] for message in state["messages"]] == [
        f"message-{number}" for number in range(2, 12)
    ]
    assert state["quota"]["remaining"] == 10

    page = lounge["client"].get(
        "/", headers={"Cookie": f"visitor_session={cookie}"}
    )
    assert page.status_code == 200
    assert "林间接待人" in page.text
    assert "配置访客" in page.text
    assert 'id="quota-reset"' in page.text
    assert "尚未开始" in page.text
    assert all(
        forbidden not in page.text.casefold()
        for forbidden in ("sidebar", "model picker", "attachment", "/admin")
    )


def test_chat_prompt_injects_only_the_three_most_recent_coarse_summaries(lounge):
    visitor_id, _, cookie = _identity(lounge, name="摘要访客")
    source = lounge["messages"].append(
        visitor_id, "visitor", "摘要来源", created_at=NOW
    )
    with lounge["database"].transaction(immediate=True) as connection:
        connection.executemany(
            """
            INSERT INTO summaries
                (id, visitor_id, first_message_id, last_message_id, text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"summary-{number}",
                    visitor_id,
                    source.id,
                    source.id,
                    f"coarse-summary-{number}",
                    NOW.replace(microsecond=number).isoformat(),
                )
                for number in range(4)
            ],
        )

    sent = lounge["client"].post(
        "/api/messages",
        headers={"Cookie": f"visitor_session={cookie}"},
        json={"request_id": "with-summaries", "text": "继续聊"},
    )
    job_id = sent.json()["job_id"]
    with lounge["client"].stream(
        "GET",
        f"/api/jobs/{job_id}/events",
        headers={"Cookie": f"visitor_session={cookie}"},
    ) as streamed:
        streamed.read()

    prompt = lounge["adapter"].prompts[-1]
    assert "coarse-summary-0" not in prompt
    assert all(f"coarse-summary-{number}" in prompt for number in range(1, 4))


def test_next_chat_prompt_uses_live_global_persona(lounge):
    visitor_id, _, cookie = _identity(lounge, name="人设访客")
    reception = ReceptionSettingsRepository(
        lounge["database"], lounge["settings"].root
    )
    current = reception.get()
    reception.save(replace(current, persona_text="这是后台刚保存的新接待人设。"))

    sent = lounge["client"].post(
        "/api/messages",
        headers={"Cookie": f"visitor_session={cookie}"},
        json={"request_id": "live-persona", "text": "你好"},
    )
    with lounge["client"].stream(
        "GET",
        f"/api/jobs/{sent.json()['job_id']}/events",
        headers={"Cookie": f"visitor_session={cookie}"},
    ) as streamed:
        streamed.read()

    assert "这是后台刚保存的新接待人设。" in lounge["adapter"].prompts[-1]


def test_template_rejections_do_not_change_quota(lounge):
    _, _, cookie = _identity(lounge, name="模板访客")
    headers = {"Cookie": f"visitor_session={cookie}"}
    before = lounge["client"].get("/api/state", headers=headers).json()["quota"]

    response = lounge["client"].post(
        "/api/messages",
        headers=headers,
        json={"request_id": "too-long-template", "text": "字" * 501},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["template_text"] == (
        "这条消息太长了，请缩短到 500 字以内再发送。"
    )
    assert lounge["client"].get("/api/state", headers=headers).json()["quota"] == before


def test_disabled_lounge_uses_template_without_starting_a_model_job(lounge):
    _, _, cookie = _identity(lounge, name="暂停访客")
    reception = ReceptionSettingsRepository(
        lounge["database"], lounge["settings"].root
    )
    reception.save(replace(reception.get(), lounge_enabled=False))

    response = lounge["client"].post(
        "/api/messages",
        headers={"Cookie": f"visitor_session={cookie}"},
        json={"request_id": "closed-lounge", "text": "有人吗"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["template_text"] == "会客室现在暂时休息，请稍后再来。"
    assert lounge["adapter"].prompts == []


def test_safety_lock_persists_the_fixed_template_instead_of_generated_text(lounge):
    visitor_id, _, cookie = _identity(lounge, name="安全访客")
    lounge["adapter"].action = "safety_lock"
    sent = lounge["client"].post(
        "/api/messages",
        headers={"Cookie": f"visitor_session={cookie}"},
        json={"request_id": "safety-template", "text": "不合适的请求"},
    )
    with lounge["client"].stream(
        "GET",
        f"/api/jobs/{sent.json()['job_id']}/events",
        headers={"Cookie": f"visitor_session={cookie}"},
    ) as streamed:
        streamed.read()

    assert lounge["repository"].visitor(visitor_id).status == "safety_lock"
    assert lounge["messages"].recent(visitor_id)[-1].content == (
        "这项请求不适合在会客室继续，我会先结束本次会面。"
        "如果你认为这是误判，可以联系邀请你来的人。"
    )


def test_service_persists_usage_action_and_scopes_model_state(lounge):
    visitor_a, _, cookie_a = _identity(lounge, name="动作访客")
    visitor_b, _, _ = _identity(lounge, name="旁观访客")
    lounge["adapter"].action = "suspend"

    sent = lounge["client"].post(
        "/api/messages",
        headers={"Cookie": f"visitor_session={cookie_a}"},
        json={"request_id": "action-1", "text": "不要重复我"},
    )
    assert sent.status_code == 202
    job_id = sent.json()["job_id"]

    with lounge["client"].stream(
        "GET",
        f"/api/jobs/{job_id}/events",
        headers={"Cookie": f"visitor_session={cookie_a}"},
    ) as streamed:
        body = streamed.read().decode()

    assert "event: text" in body
    assert "event: usage" in body
    assert '"action":"suspend"' in body
    assert lounge["adapter"].prompts[0].count("不要重复我") == 1
    assert lounge["repository"].visitor(visitor_a).status == "suspended"
    assert lounge["repository"].visitor(visitor_b).status == "active"
    job = lounge["repository"].job_by_id(job_id)
    assert job.action == "suspend"
    assert (job.input_tokens, job.output_tokens) == (23, 4)
    assert [message.content for message in lounge["messages"].recent(visitor_a)][-1] == "欢迎。"


def test_duplicate_request_survives_suspension_and_in_memory_ticket_eviction(lounge):
    visitor_id, _, cookie = _identity(lounge, name="幂等访客")
    lounge["adapter"].action = "suspend"
    request = {
        "headers": {"Cookie": f"visitor_session={cookie}"},
        "json": {"request_id": "durable-retry", "text": "persist once"},
    }
    first = lounge["client"].post("/api/messages", **request)
    job_id = first.json()["job_id"]
    with lounge["client"].stream(
        "GET",
        f"/api/jobs/{job_id}/events",
        headers={"Cookie": f"visitor_session={cookie}"},
    ) as streamed:
        streamed.read()
    service = lounge["client"].app.state.visitor_service
    service.scheduler.evict_ticket(job_id)
    prompts_before = len(lounge["adapter"].prompts)
    with lounge["database"].connection() as connection:
        calls_before = connection.execute(
            "SELECT COUNT(*) FROM model_calls WHERE job_id = ?", (job_id,)
        ).fetchone()[0]

    duplicate = lounge["client"].post("/api/messages", **request)

    assert duplicate.status_code == 202
    assert duplicate.json() == {"job_id": job_id, "queue_position": None}
    assert lounge["repository"].visitor(visitor_id).status == "suspended"
    assert len(lounge["adapter"].prompts) == prompts_before
    assert [message.content for message in lounge["messages"].recent(visitor_id)].count(
        "persist once"
    ) == 1
    with lounge["database"].connection() as connection:
        calls_after = connection.execute(
            "SELECT COUNT(*) FROM model_calls WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    assert calls_after == calls_before == 1


def test_evicted_job_sse_falls_back_to_authoritative_persisted_snapshot(lounge):
    _, _, cookie = _identity(lounge, name="重连访客")
    sent = lounge["client"].post(
        "/api/messages",
        headers={"Cookie": f"visitor_session={cookie}"},
        json={"request_id": "fallback-sse", "text": "hello"},
    )
    job_id = sent.json()["job_id"]
    with lounge["client"].stream(
        "GET",
        f"/api/jobs/{job_id}/events",
        headers={"Cookie": f"visitor_session={cookie}"},
    ) as first:
        first.read()
    lounge["client"].app.state.visitor_service.scheduler.evict_ticket(job_id)

    with lounge["client"].stream(
        "GET",
        f"/api/jobs/{job_id}/events",
        headers={"Cookie": f"visitor_session={cookie}", "Last-Event-ID": "2"},
    ) as reconnected:
        body = reconnected.read().decode()

    assert "event: snapshot" in body
    assert '"visible_text":"欢迎。"' in body
    assert "event: completed" in body
    assert "prompt" not in body.casefold()


def test_job_events_revalidate_job_owner(lounge):
    _, _, cookie_a = _identity(lounge, name="持有访客")
    _, _, cookie_b = _identity(lounge, name="其他访客")
    sent = lounge["client"].post(
        "/api/messages",
        headers={"Cookie": f"visitor_session={cookie_a}"},
        json={"request_id": "owned-job", "text": "hello"},
    )

    response = lounge["client"].get(
        f"/api/jobs/{sent.json()['job_id']}/events",
        headers={"Cookie": f"visitor_session={cookie_b}"},
    )

    assert response.status_code == 404


def test_logout_revokes_server_session_and_clears_cookie(lounge):
    _, _, cookie = _identity(lounge)

    response = lounge["client"].post(
        "/api/logout", headers={"Cookie": f"visitor_session={cookie}"}
    )

    assert response.status_code == 200
    assert lounge["sessions"].resolve(cookie) is None
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_second_server_side_submission_is_rejected_without_persisting(lounge):
    visitor_id, _, cookie = _identity(lounge)
    quota = QuotaService(lounge["database"])
    quota.reserve_message(visitor_id, "already-pending", "first", NOW)

    response = lounge["client"].post(
        "/api/messages",
        headers={"Cookie": f"visitor_session={cookie}"},
        json={"request_id": "second", "text": "must not persist"},
    )

    assert response.status_code == 409
    assert [message.content for message in lounge["messages"].recent(visitor_id)] == [
        "first"
    ]
    assert quota.state(visitor_id).reserved == 1


def test_queue_full_rolls_back_unaccepted_message_and_quota(lounge):
    service = lounge["client"].app.state.visitor_service
    service.scheduler.resource_gate = ResourceGate(
        sampler=lambda: type(
            "Sample", (), {"cpu_percent": 0.0, "available_memory_bytes": 0}
        )()
    )
    for number in range(3):
        _, _, cookie = _identity(lounge, name=f"排队访客{number}")
        accepted = lounge["client"].post(
            "/api/messages",
            headers={"Cookie": f"visitor_session={cookie}"},
            json={"request_id": f"queued-{number}", "text": "wait"},
        )
        assert accepted.status_code == 202

    rejected_id, _, rejected_cookie = _identity(lounge, name="队满访客")
    rejected = lounge["client"].post(
        "/api/messages",
        headers={"Cookie": f"visitor_session={rejected_cookie}"},
        json={"request_id": "queue-full", "text": "keep in composer"},
    )

    assert rejected.status_code == 503
    assert lounge["messages"].recent(rejected_id) == []
    assert lounge["repository"].latest_job(rejected_id) is None


def test_duplicate_request_returns_the_existing_ticket_without_cancelling_it(lounge):
    service = lounge["client"].app.state.visitor_service
    service.scheduler.resource_gate = ResourceGate(
        sampler=lambda: type(
            "Sample", (), {"cpu_percent": 0.0, "available_memory_bytes": 0}
        )()
    )
    visitor_id, _, cookie = _identity(lounge)
    request = {
        "headers": {"Cookie": f"visitor_session={cookie}"},
        "json": {"request_id": "retry-safe", "text": "only once"},
    }

    first = lounge["client"].post("/api/messages", **request)
    second = lounge["client"].post("/api/messages", **request)

    assert first.status_code == second.status_code == 202
    assert second.json() == first.json()
    assert [message.content for message in lounge["messages"].recent(visitor_id)] == [
        "only once"
    ]
    assert lounge["repository"].job("retry-safe").status == "queued"


def test_running_job_is_exposed_as_generating_visitor_state(lounge):
    visitor_id, _, cookie = _identity(lounge)
    reservation = QuotaService(lounge["database"]).reserve_message(
        visitor_id, "running-state", "hello", NOW
    )
    with lounge["database"].transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE generation_jobs SET status = 'running' WHERE id = ?",
            (reservation.job_id,),
        )

    response = lounge["client"].get(
        "/api/state", headers={"Cookie": f"visitor_session={cookie}"}
    )

    assert response.status_code == 200
    assert response.json()["job"]["status"] == "generating"


def test_quota_reset_is_rendered_from_api_state_initially_and_after_send(lounge):
    _, _, cookie = _identity(lounge, name="额度访客")
    headers = {"Cookie": f"visitor_session={cookie}"}
    before = lounge["client"].get("/", headers=headers)
    assert "尚未开始" in before.text

    sent = lounge["client"].post(
        "/api/messages",
        headers=headers,
        json={"request_id": "quota-reset", "text": "hello"},
    )
    assert sent.status_code == 202
    state = lounge["client"].get("/api/state", headers=headers).json()
    reset_at = state["quota"]["reset_at"]
    after = lounge["client"].get("/", headers=headers)

    assert reset_at is not None
    assert reset_at in after.text


def test_safety_lock_generation_action_is_persisted_as_an_audit_event(lounge):
    visitor_id, _, cookie = _identity(lounge, name="安全访客")
    lounge["adapter"].action = "safety_lock"
    sent = lounge["client"].post(
        "/api/messages",
        headers={"Cookie": f"visitor_session={cookie}"},
        json={"request_id": "safety-audit", "text": "需要暂停"},
    )
    with lounge["client"].stream(
        "GET",
        f"/api/jobs/{sent.json()['job_id']}/events",
        headers={"Cookie": f"visitor_session={cookie}"},
    ) as streamed:
        streamed.read()

    with lounge["database"].connection() as connection:
        audit = connection.execute(
            """
            SELECT kind, payload FROM audit_events
            WHERE visitor_id = ? ORDER BY rowid DESC LIMIT 1
            """,
            (visitor_id,),
        ).fetchone()
    assert audit[0] == "safety_lock"
    assert "job_id" in audit[1]
    assert "safety-audit" not in audit[1]
    assert "需要暂停" not in audit[1]


def test_browser_counts_code_points_and_does_not_html_truncate(lounge, project_root):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the lightweight browser contract")
    _, _, cookie = _identity(lounge, name="unicode-visitor")

    page = lounge["client"].get(
        "/", headers={"Cookie": f"visitor_session={cookie}"}
    )
    textarea = page.text.split('id="message-text"', 1)[1].split(">", 1)[0]
    result = subprocess.run(
        [
            node,
            str(project_root / "tests" / "visitor_unicode_contract.cjs"),
            str(project_root / "static" / "visitor.js"),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert "maxlength" not in textarea
    assert result.returncode == 0, result.stderr
