from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from visitor_lounge.admin_app import create_admin_app
from visitor_lounge.container import Container
from visitor_lounge.quota import QuotaService
from visitor_lounge.repository import (
    MessageRepository,
    RuntimeStateRepository,
    VisitorRepository,
)
from visitor_lounge.security import KeyService
from visitor_lounge.settings import Settings


NOW = datetime(2026, 8, 6, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
def admin_lounge(database, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "persona.md").write_text(
        "温和而稳重。", encoding="utf-8"
    )
    settings = Settings(
        root=tmp_path,
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
        codex_workdir=tmp_path / "codex-workdir",
        host_display_name="接待人",
    )
    database.initialize()
    visitors = VisitorRepository(database)
    visitor_id = visitors.create_unclaimed_visitor()
    visitors.claim_name(visitor_id, "青鸟", "1")
    raw_key = KeyService(visitors, settings).create(visitor_id).value
    messages = MessageRepository(database)
    first = messages.append(visitor_id, "visitor", "完整原始来信", created_at=NOW)
    reply = messages.append(
        visitor_id, "host", "完整原始回复", created_at=NOW + timedelta(seconds=3)
    )
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO visits
                (id, visitor_id, started_at, last_activity_at, ended_at)
            VALUES ('visit-a', ?, ?, ?, NULL)
            """,
            (visitor_id, NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO summaries
                (id, visitor_id, first_message_id, last_message_id, text, created_at)
            VALUES ('summary-a', ?, ?, ?, '最新摘要内容', ?)
            """,
            (visitor_id, first.id, reply.id, NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO notification_events
                (id, visitor_id, kind, payload, created_at)
            VALUES ('notice-a', ?, 'summary_ready', '{"summary_id":"summary-a"}', ?)
            """,
            (visitor_id, NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO summary_jobs
                (id, visitor_id, first_message_id, last_message_id, status,
                 attempt_count, input_tokens, output_tokens, created_at,
                 started_at, finished_at)
            VALUES ('summary-job-a', ?, ?, ?, 'completed', 0, 400, 100, ?, ?, ?)
            """,
            (
                visitor_id,
                first.id,
                reply.id,
                NOW.isoformat(),
                NOW.isoformat(),
                (NOW + timedelta(seconds=1)).isoformat(),
            ),
        )
        connection.executemany(
            """
            INSERT INTO summary_generation_attempts
                (id, summary_job_id, visitor_id, status, usage_reported,
                 input_tokens, output_tokens, failure_reason, started_at,
                 finished_at)
            VALUES (?, 'summary-job-a', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "summary-attempt-failed",
                    visitor_id,
                    "failed",
                    0,
                    0,
                    0,
                    "RuntimeError",
                    (NOW - timedelta(seconds=3)).isoformat(),
                    (NOW - timedelta(seconds=1)).isoformat(),
                ),
                (
                    "summary-attempt-completed",
                    visitor_id,
                    "completed",
                    1,
                    400,
                    100,
                    None,
                    NOW.isoformat(),
                    (NOW + timedelta(seconds=1)).isoformat(),
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO quota_windows
                (id, visitor_id, limit_count, used_count, reserved_count,
                 started_at, ends_at)
            VALUES ('quota-seeded', ?, 10, 4, 0, ?, ?)
            """,
            (visitor_id, NOW.isoformat(), (NOW + timedelta(hours=24)).isoformat()),
        )
        connection.execute(
            """
            INSERT INTO generation_jobs
                (id, visitor_id, message_id, response_message_id, request_id,
                 kind, status, visible_text, created_at, started_at, finished_at)
            VALUES ('job-a', ?, ?, ?, 'request-a', 'chat', 'completed',
                    '完整原始回复', ?, ?, ?)
            """,
            (
                visitor_id,
                first.id,
                reply.id,
                NOW.isoformat(),
                (NOW + timedelta(seconds=1)).isoformat(),
                (NOW + timedelta(seconds=3)).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO model_calls
                (id, visitor_id, job_id, usage_reported, input_tokens, output_tokens,
                 created_at, completed_at)
            VALUES ('call-a', ?, 'job-a', 1, 1000, 250, ?, ?)
            """,
            (
                visitor_id,
                (NOW + timedelta(seconds=1)).isoformat(),
                (NOW + timedelta(seconds=3)).isoformat(),
            ),
        )
    container = Container(settings=settings, database=database, clock=lambda: NOW)
    app = create_admin_app(container)
    return {
        "app": app,
        "client": TestClient(app),
        "container": container,
        "database": database,
        "settings": settings,
        "visitor_id": visitor_id,
        "raw_key": raw_key,
        "visitors": visitors,
    }


def _audit_kinds(admin_lounge):
    with admin_lounge["database"].connection() as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT kind FROM audit_events ORDER BY created_at, rowid"
            ).fetchall()
        ]


def test_admin_dashboard_reports_activity_without_visitor_routes(admin_lounge):
    response = admin_lounge["client"].get("/admin")

    assert response.status_code == 200
    assert "今日有活动" in response.text
    assert "模型调用" in response.text
    assert "最新摘要" in response.text
    assert "当前队列" in response.text
    assert "资源暂停" in response.text
    assert "创建邀请" in response.text
    assert "青鸟" in response.text
    assert "剩余 6 / 10" in response.text
    assert "额度恢复" in response.text
    assert "模型调用</span><strong>3" in response.text
    assert "chat calls + summary attempts" in response.text
    assert admin_lounge["client"].post("/api/login", json={"key": "x"}).status_code == 404


def test_dashboard_and_detail_retain_backfilled_legacy_summary_usage(admin_lounge):
    with admin_lounge["database"].transaction(immediate=True) as connection:
        connection.execute("DELETE FROM summary_generation_attempts")
    admin_lounge["database"].initialize()

    dashboard = admin_lounge["client"].get("/admin")
    detail = admin_lounge["client"].get(
        f"/admin/visitors/{admin_lounge['visitor_id']}"
    )

    assert "模型调用</span><strong>2" in dashboard.text
    assert "legacy-summary-attempt:summary-job-a" in detail.text
    assert "1,400 输入" in detail.text
    assert "350 输出" in detail.text


def test_empty_dashboard_creates_an_unclaimed_invitation_atomically(admin_lounge):
    with admin_lounge["database"].transaction(immediate=True) as connection:
        connection.execute("DELETE FROM visitors")
        connection.execute("DELETE FROM audit_events")

    response = admin_lounge["client"].post("/admin/api/invitations")

    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    invitation = response.json()
    assert invitation["hide_after_seconds"] == 30
    visitor = admin_lounge["visitors"].visitor(invitation["visitor_id"])
    assert visitor.display_name is None
    assert (
        KeyService(admin_lounge["visitors"], admin_lounge["settings"]).authenticate(
            invitation["key"]
        )
        == invitation["visitor_id"]
    )
    with admin_lounge["database"].connection() as connection:
        audit = connection.execute(
            "SELECT kind, payload FROM audit_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert audit[0] == "visitor_invited"
    assert invitation["key"] not in audit[1]

    disclosure = admin_lounge["client"].post(
        f"/admin/api/visitors/{invitation['visitor_id']}/key/copy-disclosure"
    )
    assert disclosure.status_code == 200
    assert disclosure.json()["key"] == invitation["key"]
    assert _audit_kinds(admin_lounge)[-1] == "key_copy_disclosed"


def test_invitation_rolls_back_when_audit_cannot_be_persisted(
    admin_lounge, monkeypatch
):
    with admin_lounge["database"].transaction(immediate=True) as connection:
        connection.execute("DELETE FROM visitors")
        connection.execute("DELETE FROM audit_events")
    service = admin_lounge["app"].state.admin_service

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("audit unavailable")

    import sqlite3

    monkeypatch.setattr(service, "_audit", fail_audit)
    response = TestClient(
        admin_lounge["app"], raise_server_exceptions=False
    ).post("/admin/api/invitations")

    assert response.status_code == 500
    with admin_lounge["database"].connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM visitors").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM visitor_keys").fetchone()[0] == 0


def test_key_copy_disclosure_returns_active_key_and_audits_without_plaintext(
    admin_lounge, caplog,
):
    with caplog.at_level(logging.INFO):
        response = admin_lounge["client"].post(
            f"/admin/api/visitors/{admin_lounge['visitor_id']}/key/copy-disclosure"
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "key": admin_lounge["raw_key"],
        "hide_after_seconds": 30,
        "purpose": "copy",
    }
    with admin_lounge["database"].connection() as connection:
        audit = connection.execute(
            "SELECT kind, payload FROM audit_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert audit[0] == "key_copy_disclosed"
    assert admin_lounge["raw_key"] not in audit[1]
    assert admin_lounge["raw_key"] not in caplog.text
    assert "masked" in audit[1]


def test_key_copy_disclosure_does_not_return_key_when_audit_fails(
    admin_lounge, monkeypatch
):
    service = admin_lounge["app"].state.admin_service

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("audit unavailable")

    import sqlite3

    monkeypatch.setattr(service, "_audit", fail_audit)
    response = TestClient(
        admin_lounge["app"], raise_server_exceptions=False
    ).post(
        f"/admin/api/visitors/{admin_lounge['visitor_id']}/key/copy-disclosure"
    )

    assert response.status_code == 500
    with admin_lounge["database"].connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE kind = 'key_copy_disclosed'"
        ).fetchone()[0]
    assert count == 0


def test_copy_controls_use_server_disclosure_before_clipboard_write(
    admin_lounge, project_root
):
    dashboard = admin_lounge["client"].get("/admin")
    detail = admin_lounge["client"].get(
        f"/admin/visitors/{admin_lounge['visitor_id']}"
    )
    script = (project_root / "static" / "admin.js").read_text("utf-8")

    assert 'id="copy-invitation-key" type="button" disabled' in dashboard.text
    assert 'id="copy-key" type="button"' in detail.text
    disclosure_calls = [
        index
        for index in range(len(script))
        if script.startswith("key/copy-disclosure", index)
    ]
    clipboard_calls = [
        index
        for index in range(len(script))
        if script.startswith("navigator.clipboard.writeText", index)
    ]
    assert len(disclosure_calls) == len(clipboard_calls) == 2
    assert all(disclosure < clipboard for disclosure, clipboard in zip(disclosure_calls, clipboard_calls))
    assert "key/copied" not in script
    assert "copyButton.disabled = true" not in script
    assert "已为复制而披露，但写入剪贴板失败" in script


def test_admin_dashboard_reads_persisted_resource_state_and_expires_it(admin_lounge):
    runtime = RuntimeStateRepository(admin_lounge["database"])
    runtime.record_resource_gate(can_start=False, checked_at=NOW)

    paused = admin_lounge["client"].get("/admin")
    stale_app = create_admin_app(
        Container(
            settings=admin_lounge["settings"],
            database=admin_lounge["database"],
            clock=lambda: NOW + timedelta(seconds=31),
        )
    )
    stale = TestClient(stale_app).get("/admin")

    assert "资源暂停" in paused.text
    assert "已暂停" in paused.text
    assert "状态过期" in stale.text


def test_visitor_detail_shows_complete_history_summary_usage_and_latency(admin_lounge):
    visitor_id = admin_lounge["visitor_id"]

    response = admin_lounge["client"].get(f"/admin/visitors/{visitor_id}")

    assert response.status_code == 200
    assert "完整原始来信" in response.text
    assert "完整原始回复" in response.text
    assert "最新摘要内容" in response.text
    assert "1,000" in response.text
    assert "250" in response.text
    assert "2.00 秒" in response.text
    assert "总 Token" in response.text
    assert "1,400 输入" in response.text
    assert "350 输出" in response.text
    assert "模型报告 usage" in response.text
    assert "attempt 结果表示模型生成与内容校验" in response.text
    assert "summary-attempt-completed" in response.text
    assert "summary-attempt-failed" in response.text
    assert "completed" in response.text
    assert "failed" in response.text
    assert "1.00 秒" in response.text
    assert "usage 未报告" in response.text
    assert "费用估算" not in response.text
    assert admin_lounge["raw_key"] not in response.text
    assert admin_lounge["raw_key"][-4:] in response.text


def test_cost_is_rendered_only_when_both_token_prices_are_configured(admin_lounge):
    priced = replace(
        admin_lounge["settings"],
        input_token_price_per_million=2.0,
        output_token_price_per_million=8.0,
    )
    app = create_admin_app(
        Container(settings=priced, database=admin_lounge["database"], clock=lambda: NOW)
    )

    response = TestClient(app).get(
        f"/admin/visitors/{admin_lounge['visitor_id']}"
    )

    assert "费用估算" in response.text
    assert "¥0.0040" in response.text
    assert "¥0.0056" in response.text


def test_chat_usage_totals_and_rows_only_claim_explicitly_reported_usage(admin_lounge):
    visitor_id = admin_lounge["visitor_id"]
    with admin_lounge["database"].transaction(immediate=True) as connection:
        connection.executemany(
            """
            INSERT INTO generation_jobs
                (id, visitor_id, request_id, kind, status, created_at,
                 started_at, finished_at)
            VALUES (?, ?, ?, 'chat', 'completed', ?, ?, ?)
            """,
            [
                (
                    "job-usage-unknown",
                    visitor_id,
                    "request-usage-unknown",
                    NOW.isoformat(),
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
                (
                    "job-usage-zero",
                    visitor_id,
                    "request-usage-zero",
                    NOW.isoformat(),
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO model_calls
                (id, visitor_id, job_id, usage_reported, input_tokens,
                 output_tokens, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "call-usage-unknown",
                    visitor_id,
                    "job-usage-unknown",
                    0,
                    9000,
                    8000,
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
                (
                    "call-usage-zero",
                    visitor_id,
                    "job-usage-zero",
                    1,
                    0,
                    0,
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            ],
        )
    priced = replace(
        admin_lounge["settings"],
        input_token_price_per_million=2.0,
        output_token_price_per_million=8.0,
    )
    app = create_admin_app(
        Container(settings=priced, database=admin_lounge["database"], clock=lambda: NOW)
    )

    response = TestClient(app).get(f"/admin/visitors/{visitor_id}")

    unknown_row = response.text.split("request-usage-unknown", 1)[1].split(
        "</tr>", 1
    )[0]
    zero_row = response.text.split("request-usage-zero", 1)[1].split("</tr>", 1)[0]
    assert "1,400 \u8f93\u5165" in response.text
    assert "350 \u8f93\u51fa" in response.text
    assert "usage \u672a\u62a5\u544a" in unknown_row
    assert "9000" not in unknown_row
    assert "8000" not in unknown_row
    assert "\u2014" in unknown_row
    assert "0 in" in zero_row
    assert "0 out" in zero_row
    assert "\u00a50.0000" in zero_row


def test_session_status_uses_revocation_and_current_expiry(admin_lounge):
    visitor_id = admin_lounge["visitor_id"]
    with admin_lounge["database"].transaction(immediate=True) as connection:
        connection.executemany(
            """
            INSERT INTO auth_sessions
                (id, visitor_id, device_id, session_hash, created_at, expires_at,
                 revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "expired-session",
                    visitor_id,
                    "expired-device",
                    "expired-hash",
                    (NOW - timedelta(days=2)).isoformat(),
                    (NOW - timedelta(seconds=1)).isoformat(),
                    None,
                ),
                (
                    "revoked-session",
                    visitor_id,
                    "revoked-device",
                    "revoked-hash",
                    (NOW - timedelta(days=2)).isoformat(),
                    (NOW + timedelta(days=1)).isoformat(),
                    NOW.isoformat(),
                ),
            ],
        )

    response = admin_lounge["client"].post(
        f"/admin/api/visitors/{visitor_id}/export",
        json={"confirmation": "EXPORT"},
    )

    statuses = {session["device_id"]: session["status"] for session in response.json()["sessions"]}
    assert statuses == {"expired-device": "expired", "revoked-device": "revoked"}


def test_viewing_one_visitor_marks_only_their_summaries_read(admin_lounge):
    other_id = admin_lounge["visitors"].create_unclaimed_visitor()
    with admin_lounge["database"].transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO notification_events
                (id, visitor_id, kind, payload, created_at)
            VALUES ('notice-other', ?, 'summary_ready', '{}', ?)
            """,
            (other_id, NOW.isoformat()),
        )

    before = admin_lounge["client"].get("/admin")
    viewed = admin_lounge["client"].get(
        f"/admin/visitors/{admin_lounge['visitor_id']}"
    )
    after = admin_lounge["client"].get("/admin")

    assert "未读摘要</span><strong>2" in before.text
    assert viewed.status_code == 200
    assert "未读摘要</span><strong>1" in after.text
    with admin_lounge["database"].connection() as connection:
        rows = connection.execute(
            "SELECT visitor_id, delivered_at FROM notification_events ORDER BY id"
        ).fetchall()
    assert {row[0] for row in rows if row[1] is None} == {other_id}


def test_summary_delivery_marks_only_notification_ids_in_rendered_snapshot(
    admin_lounge,
):
    service = admin_lounge["app"].state.admin_service
    visitor_id = admin_lounge["visitor_id"]
    snapshot = service.visitor_detail(visitor_id)
    assert snapshot["summary_notification_ids"] == ["notice-a"]
    with admin_lounge["database"].transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO notification_events
                (id, visitor_id, kind, payload, created_at)
            VALUES ('notice-concurrent', ?, 'summary_ready', ?, ?)
            """,
            (
                visitor_id,
                json.dumps({"summary_id": "summary-a"}),
                (NOW + timedelta(seconds=1)).isoformat(),
            ),
        )

    service.mark_summary_notifications_delivered(
        visitor_id, snapshot["summary_notification_ids"]
    )

    with admin_lounge["database"].connection() as connection:
        rows = connection.execute(
            "SELECT id, delivered_at FROM notification_events ORDER BY id"
        ).fetchall()
    delivered = {row[0]: row[1] for row in rows}
    assert delivered["notice-a"] == NOW.isoformat()
    assert delivered["notice-concurrent"] is None


def test_expired_quota_is_presented_as_a_fresh_full_window(admin_lounge):
    visitor_id = admin_lounge["visitor_id"]
    expired_at = NOW - timedelta(seconds=1)
    with admin_lounge["database"].transaction(immediate=True) as connection:
        connection.execute("DELETE FROM quota_windows WHERE visitor_id = ?", (visitor_id,))
        connection.execute(
            """
            INSERT INTO quota_windows
                (id, visitor_id, limit_count, used_count, reserved_count,
                 started_at, ends_at)
            VALUES ('expired-window', ?, 10, 9, 1, ?, ?)
            """,
            (
                visitor_id,
                (NOW - timedelta(days=1, seconds=1)).isoformat(),
                expired_at.isoformat(),
            ),
        )

    dashboard = admin_lounge["client"].get("/admin")
    detail = admin_lounge["client"].post(
        f"/admin/api/visitors/{visitor_id}/export",
        json={"confirmation": "EXPORT"},
    ).json()

    assert "剩余 10 / 10" in dashboard.text
    assert "下一条消息开始新 24h 窗口" in dashboard.text
    assert detail["quota_current"] == {
        "limit_count": 10,
        "used_count": 0,
        "reserved_count": 0,
        "remaining": 10,
        "reset_at": None,
        "message": "下一条消息开始新 24h 窗口",
    }


def test_today_visitors_uses_configured_local_day_and_activity(admin_lounge):
    local_settings = replace(admin_lounge["settings"], timezone_name="Asia/Shanghai")
    with admin_lounge["database"].transaction(immediate=True) as connection:
        connection.execute("DELETE FROM visits")
        connection.execute(
            """
            INSERT INTO visits
                (id, visitor_id, started_at, last_activity_at, ended_at)
            VALUES ('local-day-visit', ?, ?, ?, ?)
            """,
            (
                admin_lounge["visitor_id"],
                "2026-08-05T15:00:00+00:00",
                "2026-08-05T17:00:00+00:00",
                "2026-08-05T17:00:00+00:00",
            ),
        )
    app = create_admin_app(
        Container(local_settings, admin_lounge["database"], clock=lambda: NOW)
    )

    response = TestClient(app).get("/admin")

    assert "今日有活动" in response.text
    assert "今日有活动</span><strong>1" in response.text


def test_dashboard_shows_readable_recent_security_and_admin_activity(admin_lounge):
    events = [
        "safety_lock",
        "visitor_unlocked",
        "visitor_paused",
        "key_created",
        "key_revealed",
        "key_revoked",
        "key_rotated",
        "quota_reset",
        "visitor_exported",
        "visitor_deleted",
    ]
    with admin_lounge["database"].transaction(immediate=True) as connection:
        for index, kind in enumerate(events):
            connection.execute(
                """
                INSERT INTO audit_events
                    (id, visitor_id, kind, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    f"activity-{index}",
                    None if kind == "visitor_deleted" else admin_lounge["visitor_id"],
                    kind,
                    json.dumps(
                        {"details": {"visitor_id": admin_lounge["visitor_id"]}},
                        ensure_ascii=False,
                    ),
                    (NOW + timedelta(microseconds=index)).isoformat(),
                ),
            )

    response = admin_lounge["client"].get("/admin")

    for label in (
        "安全锁定",
        "已解锁",
        "已暂停",
        "Key 已创建",
        "Key 已显示",
        "Key 已撤销",
        "Key 已轮换",
        "额度已重置",
        "访客数据已导出",
        "访客已删除",
    ):
        assert label in response.text


def test_delete_warning_names_all_irrecoverable_related_data(admin_lounge):
    response = admin_lounge["client"].get(
        f"/admin/visitors/{admin_lounge['visitor_id']}"
    )

    for label in ("消息", "摘要", "Key", "Session", "额度", "任务", "不可恢复"):
        assert label in response.text


def test_key_reveal_is_audited_not_cached_and_never_put_on_detail_page(admin_lounge):
    visitor_id = admin_lounge["visitor_id"]

    response = admin_lounge["client"].post(
        f"/admin/api/visitors/{visitor_id}/key/reveal"
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "key": admin_lounge["raw_key"],
        "hide_after_seconds": 30,
    }
    assert _audit_kinds(admin_lounge)[-1] == "key_revealed"
    with admin_lounge["database"].connection() as connection:
        payload = connection.execute(
            "SELECT payload FROM audit_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
    assert admin_lounge["raw_key"] not in payload


def test_create_rotate_and_revoke_key_are_audited_and_masked(admin_lounge):
    visitor_id = admin_lounge["visitor_id"]
    client = admin_lounge["client"]

    created = client.post(f"/admin/api/visitors/{visitor_id}/key")
    rotated = client.post(f"/admin/api/visitors/{visitor_id}/key/rotate")
    revoked = client.post(f"/admin/api/visitors/{visitor_id}/key/revoke")

    assert created.status_code == rotated.status_code == 200
    assert created.headers["Cache-Control"] == "no-store"
    assert rotated.headers["Cache-Control"] == "no-store"
    assert created.json()["hide_after_seconds"] == 30
    assert rotated.json()["hide_after_seconds"] == 30
    assert revoked.status_code == 204
    detail = client.get(f"/admin/visitors/{visitor_id}").text
    assert created.json()["key"] not in detail
    assert rotated.json()["key"] not in detail
    assert {"key_created", "key_rotated", "key_revoked"} <= set(
        _audit_kinds(admin_lounge)
    )


def test_status_quota_and_note_actions_mutate_state_and_are_audited(admin_lounge):
    visitor_id = admin_lounge["visitor_id"]
    client = admin_lounge["client"]
    quota = QuotaService(admin_lounge["database"])
    quota.reserve(visitor_id, "quota-a", NOW)
    with admin_lounge["database"].transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO quota_windows
                (id, visitor_id, limit_count, used_count, reserved_count,
                 started_at, ends_at)
            VALUES ('historical-window', ?, 10, 7, 0, ?, ?)
            """,
            (
                visitor_id,
                (NOW - timedelta(days=2)).isoformat(),
                (NOW - timedelta(days=1)).isoformat(),
            ),
        )
        connection.execute(
            """
            UPDATE quota_windows SET used_count = 4
            WHERE visitor_id = ? AND id != 'historical-window'
            """,
            (visitor_id,),
        )

    assert client.post(f"/admin/api/visitors/{visitor_id}/pause").status_code == 204
    assert admin_lounge["visitors"].visitor(visitor_id).status == "paused"
    assert client.post(f"/admin/api/visitors/{visitor_id}/unlock").status_code == 204
    assert admin_lounge["visitors"].visitor(visitor_id).status == "active"
    assert client.post(f"/admin/api/visitors/{visitor_id}/suspend").status_code == 204
    assert admin_lounge["visitors"].visitor(visitor_id).status == "suspended"
    assert client.post(f"/admin/api/visitors/{visitor_id}/quota/reset").status_code == 204
    assert (quota.state(visitor_id).used, quota.state(visitor_id).reserved) == (0, 1)
    assert quota.confirm("quota-a").used == 1
    with admin_lounge["database"].connection() as connection:
        assert connection.execute(
            "SELECT used_count FROM quota_windows WHERE id = 'historical-window'"
        ).fetchone()[0] == 7
    note = client.post(
        f"/admin/api/visitors/{visitor_id}/notes", json={"note": "仅管理员可见"}
    )
    assert note.status_code == 201
    assert "仅管理员可见" in client.get(f"/admin/visitors/{visitor_id}").text
    assert {
        "visitor_paused",
        "visitor_unlocked",
        "visitor_suspended",
        "quota_reset",
        "note_added",
    } <= set(_audit_kinds(admin_lounge))


def test_export_requires_confirmation_is_audited_and_excludes_key_secrets(admin_lounge):
    visitor_id = admin_lounge["visitor_id"]
    client = admin_lounge["client"]

    denied = client.post(
        f"/admin/api/visitors/{visitor_id}/export", json={"confirmation": "no"}
    )
    exported = client.post(
        f"/admin/api/visitors/{visitor_id}/export",
        json={"confirmation": "EXPORT"},
    )

    assert denied.status_code == 409
    assert exported.status_code == 200
    assert exported.headers["Cache-Control"] == "no-store"
    payload = exported.json()
    assert payload["visitor"]["id"] == visitor_id
    assert payload["messages"][0]["content"] == "完整原始来信"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert admin_lounge["raw_key"] not in serialized
    assert "encrypted_value" not in serialized
    assert "key_hash" not in serialized
    assert _audit_kinds(admin_lounge)[-1] == "visitor_exported"


def test_delete_requires_exact_confirmation_and_is_audited(admin_lounge):
    visitor_id = admin_lounge["visitor_id"]
    client = admin_lounge["client"]

    denied = client.request(
        "DELETE",
        f"/admin/api/visitors/{visitor_id}",
        json={"confirmation": "delete"},
    )
    accepted = client.request(
        "DELETE",
        f"/admin/api/visitors/{visitor_id}",
        json={"confirmation": "DELETE"},
    )

    assert denied.status_code == 409
    assert accepted.status_code == 204
    with admin_lounge["database"].connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM visitors WHERE id = ?", (visitor_id,)
        ).fetchone() is None
        deleted = connection.execute(
            "SELECT kind, payload FROM audit_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert deleted[0] == "visitor_deleted"
    assert visitor_id in deleted[1]


def test_admin_app_rejects_non_loopback_binding(admin_lounge):
    public_settings = replace(admin_lounge["settings"], admin_host="0.0.0.0")

    with pytest.raises(ValueError, match="loopback"):
        create_admin_app(Container(public_settings, admin_lounge["database"]))


def test_admin_app_rejects_remote_peers_host_header_and_cross_site_mutations(
    admin_lounge,
):
    app = admin_lounge["app"]

    remote = TestClient(app, client=("203.0.113.10", 51000)).get("/admin")
    hostile_host = admin_lounge["client"].get(
        "/admin", headers={"Host": "attacker.example"}
    )
    hostile_origin = admin_lounge["client"].post(
        f"/admin/api/visitors/{admin_lounge['visitor_id']}/pause",
        headers={"Origin": "https://attacker.example"},
    )

    assert remote.status_code == 403
    assert hostile_host.status_code == 403
    assert hostile_origin.status_code == 403
    assert admin_lounge["visitors"].visitor(admin_lounge["visitor_id"]).status == "active"


def test_admin_html_is_never_cached(admin_lounge):
    dashboard = admin_lounge["client"].get("/admin")
    detail = admin_lounge["client"].get(
        f"/admin/visitors/{admin_lounge['visitor_id']}"
    )

    assert dashboard.headers["Cache-Control"] == "no-store"
    assert detail.headers["Cache-Control"] == "no-store"


def test_key_rotation_rolls_back_when_audit_cannot_be_persisted(
    admin_lounge, monkeypatch
):
    service = admin_lounge["app"].state.admin_service

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("audit unavailable")

    import sqlite3

    monkeypatch.setattr(service, "_audit", fail_audit)
    response = TestClient(
        admin_lounge["app"], raise_server_exceptions=False
    ).post(f"/admin/api/visitors/{admin_lounge['visitor_id']}/key/rotate")

    assert response.status_code == 500
    assert (
        KeyService(admin_lounge["visitors"], admin_lounge["settings"]).authenticate(
            admin_lounge["raw_key"]
        )
        == admin_lounge["visitor_id"]
    )


def test_status_change_rolls_back_when_audit_cannot_be_persisted(
    admin_lounge, monkeypatch
):
    service = admin_lounge["app"].state.admin_service

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("audit unavailable")

    import sqlite3

    monkeypatch.setattr(service, "_audit", fail_audit)
    response = TestClient(
        admin_lounge["app"], raise_server_exceptions=False
    ).post(f"/admin/api/visitors/{admin_lounge['visitor_id']}/pause")

    assert response.status_code == 500
    assert admin_lounge["visitors"].visitor(admin_lounge["visitor_id"]).status == "active"


def test_reception_settings_page_saves_and_restores_all_global_fields(admin_lounge):
    client = admin_lounge["client"]
    page = client.get("/admin/settings")
    assert page.status_code == 200
    assert "温和而稳重。" in page.text
    assert '/admin/static/admin.js?v=' in page.text
    assert client.get("/admin/static/admin.js").headers["cache-control"] == "no-store"
    assert 'href="/admin/settings"' in client.get("/admin").text

    payload = {
        "persona_text": "新的全局接待人设",
        "first_welcome": "欢迎回来前，{访客名字}",
        "returning_welcome": "又见面了，{访客名字}",
        "quota_exhausted": "额度已用完。",
        "unsafe_request": "这项请求不合适。",
        "input_too_long": "请缩短消息。",
        "lounge_closed": "会客室休息中。",
        "system_unavailable": "暂时无法回复。",
        "lounge_enabled": False,
        "idle_minutes": 30,
    }
    saved = client.put("/admin/api/settings", json=payload)

    assert saved.status_code == 200
    assert saved.json()["persona_text"] == "新的全局接待人设"
    assert client.get("/admin/settings").text.count("又见面了，{访客名字}") == 1

    restored = client.post("/admin/api/settings/restore-defaults")
    assert restored.status_code == 200
    assert restored.json()["persona_text"] == "温和而稳重。"


def test_dashboard_shows_each_visitor_masked_key_and_copy_action(admin_lounge):
    page = admin_lounge["client"].get("/admin")

    assert page.status_code == 200
    assert admin_lounge["raw_key"][-4:] in page.text
    assert f'data-copy-key="{admin_lounge["visitor_id"]}"' in page.text


def test_dashboard_reports_lightweight_lounge_runtime_metrics(admin_lounge):
    metrics = admin_lounge["app"].state.admin_service.dashboard()["metrics"]

    assert metrics["lounge_enabled"] is True
    assert metrics["active_generations"] == 0
    assert metrics["today_completed"] == 1
    assert metrics["today_failed"] == 0
    assert metrics["reported_input_tokens"] == 1000
    assert metrics["reported_output_tokens"] == 250
