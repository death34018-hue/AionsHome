from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

import visitor_lounge.admin_app as admin_app_module
import visitor_lounge.visitor_app as visitor_app_module
from visitor_lounge.admin_app import create_admin_app
from visitor_lounge.container import Container
from visitor_lounge.models import GenerationChunk, GenerationRequest
from visitor_lounge.quota import QuotaService
from visitor_lounge.repository import MessageRepository, VisitorRepository
from visitor_lounge.settings import Settings
from visitor_lounge.visitor_app import create_visitor_app


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


class NoCallAdapter:
    async def generate(self, prompt: str) -> AsyncIterator[GenerationChunk]:
        raise AssertionError(f"smoke test unexpectedly generated: {prompt[:20]}")
        yield GenerationChunk(kind="completed")


class RecordingRequestAdapter:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.closed = False

    async def generate(self, prompt: str) -> AsyncIterator[GenerationChunk]:
        self.prompts.append(prompt)
        try:
            yield GenerationChunk(kind="text", text="第一段")
            yield GenerationChunk(kind="text", text="第二段")
            yield GenerationChunk(
                kind="usage", usage={"input_tokens": 12, "output_tokens": 3}
            )
            yield GenerationChunk(kind="completed")
        finally:
            self.closed = True


def _settings(tmp_path, database) -> Settings:
    root = tmp_path / "AionsHome-Visitor-Lounge"
    (root / "config").mkdir(parents=True)
    (root / "config" / "persona.md").write_text("温和接待访客。", encoding="utf-8")
    (root / "templates").mkdir()
    (root / "static").mkdir()
    return Settings(
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
    )


def test_startup_recovers_stale_jobs_and_refunds_reservations(
    tmp_path, database
) -> None:
    settings = _settings(tmp_path, database)
    database.initialize()
    visitors = VisitorRepository(database)
    visitor_id = visitors.create_unclaimed_visitor()
    quota = QuotaService(database)
    reservation = quota.reserve(visitor_id, "stale-request", NOW)
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO model_calls (id, visitor_id, job_id, created_at)
            VALUES ('stale-no-output-call', ?, ?, ?)
            """,
            (visitor_id, reservation.job_id, NOW.isoformat()),
        )
    container = Container(settings=settings, database=database, clock=lambda: NOW)

    recovered = container.startup_recovery()

    assert recovered == 1
    assert quota.state(visitor_id).reserved == 0
    with database.connection() as connection:
        row = connection.execute(
            """
            SELECT status, response_message_id, refunded_at, refund_reason
            FROM generation_jobs WHERE id = ?
            """,
            (reservation.job_id,),
        ).fetchone()
        completed_at = connection.execute(
            "SELECT completed_at FROM model_calls WHERE job_id = ?",
            (reservation.job_id,),
        ).fetchone()[0]
        host_messages = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE visitor_id = ? AND sender = 'host'",
            (visitor_id,),
        ).fetchone()[0]
    assert row == (
        "interrupted",
        None,
        NOW.isoformat(),
        "startup_recovery",
    )
    assert completed_at == NOW.isoformat()
    assert host_messages == 0


def test_initialize_upgrades_old_job_status_constraint_for_recovery(database) -> None:
    with database.connection() as connection:
        connection.executescript(
            """
            CREATE TABLE visitors (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                display_name TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE quota_windows (
                id TEXT PRIMARY KEY,
                visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
                limit_count INTEGER NOT NULL,
                used_count INTEGER NOT NULL DEFAULT 0,
                reserved_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                ends_at TEXT NOT NULL
            );
            CREATE TABLE generation_jobs (
                id TEXT PRIMARY KEY,
                visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
                message_id TEXT,
                response_message_id TEXT,
                quota_window_id TEXT REFERENCES quota_windows(id) ON DELETE SET NULL,
                request_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK(kind IN ('chat', 'summary')),
                status TEXT NOT NULL CHECK(status IN (
                    'queued', 'running', 'completed', 'failed', 'cancelled'
                )),
                visible_text TEXT NOT NULL DEFAULT '',
                action TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                confirmed_at TEXT,
                refunded_at TEXT,
                refund_reason TEXT
            );
            INSERT INTO visitors (id, created_at) VALUES ('legacy-visitor', 'now');
            INSERT INTO quota_windows
                (id, visitor_id, limit_count, used_count, reserved_count,
                 started_at, ends_at)
            VALUES (
                'legacy-window', 'legacy-visitor', 10, 0, 1,
                '2020-01-01T00:00:00+00:00', '2020-01-01T12:00:00+00:00'
            );
            INSERT INTO generation_jobs
                (id, visitor_id, quota_window_id, request_id, kind, status, created_at)
            VALUES (
                'legacy-job', 'legacy-visitor', 'legacy-window',
                'legacy-request', 'chat', 'queued', 'now'
            );
            """
        )

    database.initialize()
    recovered = database.recover_stale_jobs(NOW)

    assert recovered == 1
    with database.connection() as connection:
        assert connection.execute(
            "SELECT status FROM generation_jobs WHERE id = 'legacy-job'"
        ).fetchone() == ("interrupted",)
        connection.execute(
            "UPDATE generation_jobs SET action = 'closing' WHERE id = 'legacy-job'"
        )
        assert connection.execute(
            "SELECT action FROM generation_jobs WHERE id = 'legacy-job'"
        ).fetchone() == ("closing",)


def test_startup_recovery_persists_partial_reply_once_without_refunding_usage(
    tmp_path, database
) -> None:
    settings = _settings(tmp_path, database)
    database.initialize()
    visitors = VisitorRepository(database)
    messages = MessageRepository(database)
    visitor_id = visitors.create_unclaimed_visitor()
    quota = QuotaService(database)
    reservation = quota.reserve_message(
        visitor_id, "partial-request", "访客原消息", NOW
    )
    quota.confirm(reservation.request_id)
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE generation_jobs
            SET status = 'running', visible_text = '已经生成的部分回复', started_at = ?
            WHERE id = ?
            """,
            (NOW.isoformat(), reservation.job_id),
        )
        connection.execute(
            """
            INSERT INTO model_calls (id, visitor_id, job_id, created_at)
            VALUES ('partial-call', ?, ?, ?)
            """,
            (visitor_id, reservation.job_id, NOW.isoformat()),
        )
    container = Container(settings=settings, database=database, clock=lambda: NOW)

    assert container.startup_recovery() == 1
    assert container.startup_recovery() == 0

    history = messages.recent(visitor_id, limit=10)
    assert [(message.sender, message.content) for message in history] == [
        ("visitor", "访客原消息"),
        ("host", "已经生成的部分回复"),
    ]
    assert quota.state(visitor_id).used == 1
    assert quota.state(visitor_id).reserved == 0
    with database.connection() as connection:
        job = connection.execute(
            """
            SELECT status, response_message_id, refunded_at
            FROM generation_jobs WHERE id = ?
            """,
            (reservation.job_id,),
        ).fetchone()
        model_call = connection.execute(
            "SELECT completed_at FROM model_calls WHERE job_id = ?",
            (reservation.job_id,),
        ).fetchone()
        host_count = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE visitor_id = ? AND sender = 'host'",
            (visitor_id,),
        ).fetchone()[0]
    assert job[0] == "interrupted"
    assert job[1] == history[-1].id
    assert job[2] is None
    assert model_call == (NOW.isoformat(),)
    assert host_count == 1


def test_two_apps_expose_only_their_own_routes(tmp_path, database) -> None:
    settings = _settings(tmp_path, database)
    visitor_app = create_visitor_app(
        Container(settings, database, codex_adapter=NoCallAdapter(), clock=lambda: NOW)
    )
    admin_app = create_admin_app(Container(settings, database, clock=lambda: NOW))

    with TestClient(visitor_app) as visitor_client, TestClient(admin_app) as admin_client:
        assert visitor_client.get("/healthz").status_code == 200
        assert visitor_client.get("/admin").status_code == 404
        assert admin_client.get("/healthz").status_code == 200
        assert admin_client.get("/admin").status_code == 200
        assert admin_client.post("/api/messages", json={}).status_code == 404


def test_admin_container_builds_no_generation_dependencies(tmp_path, database) -> None:
    settings = _settings(tmp_path, database)

    container = Container.build_admin(settings)

    assert container.codex_adapter is None
    assert container.visitor_service is None
    assert container.background is None


async def _collect(stream) -> list[GenerationChunk]:
    return [chunk async for chunk in stream]


def test_visitor_container_uses_request_scoped_adapter_and_shared_slot(
    tmp_path, database
) -> None:
    settings = _settings(tmp_path, database)
    instances: list[RecordingRequestAdapter] = []

    def adapter_factory(settings, *, model):
        assert settings is settings_under_test
        assert model == "test-model"
        adapter = RecordingRequestAdapter()
        instances.append(adapter)
        return adapter

    settings_under_test = settings
    container = Container.build_visitor(
        settings,
        model="test-model",
        codex_adapter_factory=adapter_factory,
    )

    assert container.visitor_service is not None
    assert container.background is not None
    assert container.background.scheduler is container.visitor_service.scheduler

    import asyncio

    request = GenerationRequest(
        job_id="job-1",
        request_id="request-1",
        visitor_id="visitor-1",
        message_id="message-1",
        prompt="chat prompt",
    )
    chunks = asyncio.run(_collect(container.codex_adapter.generate(request)))
    summary_text, usage = asyncio.run(
        container.background.summary_generator(object(), "summary prompt")
    )

    assert [chunk.text for chunk in chunks if chunk.kind == "text"] == [
        "第一段",
        "第二段",
    ]
    assert summary_text == "第一段第二段"
    assert usage == {"input_tokens": 12, "output_tokens": 3}
    assert [instance.prompts for instance in instances] == [
        ["chat prompt"],
        ["summary prompt"],
    ]
    assert all(instance.closed for instance in instances)


def test_visitor_lifespan_recovers_then_starts_and_stops_owned_dependencies(
    tmp_path, database
) -> None:
    settings = _settings(tmp_path, database)
    events: list[str] = []

    class Service:
        async def start(self):
            events.append("service.start")

        async def shutdown(self):
            events.append("service.shutdown")

    class Background:
        async def start(self):
            events.append("background.start")

        async def stop(self):
            events.append("background.stop")

    container = Container(
        settings,
        database,
        codex_adapter=NoCallAdapter(),
        visitor_service=Service(),
        background=Background(),
    )
    original_recovery = container.startup_recovery

    def recover():
        events.append("recovery")
        return original_recovery()

    container.startup_recovery = recover  # type: ignore[method-assign]
    app = create_visitor_app(container)

    with TestClient(app):
        assert events == ["recovery", "service.start", "background.start"]

    assert events == [
        "recovery",
        "service.start",
        "background.start",
        "background.stop",
        "service.shutdown",
    ]


def test_uvicorn_factories_keep_generation_out_of_admin(
    tmp_path, database, monkeypatch
) -> None:
    settings = _settings(tmp_path, database)
    visitor_container = Container.build_visitor(
        settings,
        codex_adapter_factory=lambda settings, *, model: NoCallAdapter(),
    )
    admin_container = Container.build_admin(settings)
    monkeypatch.setattr(
        Settings, "load", classmethod(lambda _cls, _root: settings)
    )
    monkeypatch.setattr(
        Container,
        "build_visitor",
        classmethod(lambda _cls, _settings: visitor_container),
    )
    monkeypatch.setattr(
        Container,
        "build_admin",
        classmethod(lambda _cls, _settings: admin_container),
    )

    visitor_app = visitor_app_module.build_visitor_app()
    admin_app = admin_app_module.build_admin_app()

    assert visitor_app.state.visitor_service is visitor_container.visitor_service
    assert visitor_app.state.background is visitor_container.background
    assert admin_app.state.admin_service.container is admin_container
    assert admin_container.visitor_service is None
    assert admin_container.background is None


SCRIPT_NAMES = (
    "runtime-common.ps1",
    "supervisor.ps1",
    "start.ps1",
    "stop.ps1",
    "status.ps1",
    "diagnose.ps1",
)


def _powershell() -> str:
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    return executable


def _fixture_script_project(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "scripts"
    project = tmp_path / "AionsHome-Visitor-Lounge"
    shutil.copytree(source, project / "scripts")
    return project


def _write_fixture_environment(project: Path) -> tuple[str, str, str]:
    (project / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
    python = project / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python.write_text("fixture")
    (project / "config").mkdir()
    (project / "config" / "visitor-lounge.toml").write_text("[visitor]\n")
    secrets = (
        "fixture-pepper-DO-NOT-PRINT",
        "fixture-master-DO-NOT-PRINT",
        "fixture-session-DO-NOT-PRINT",
    )
    (project / ".env").write_text(
        "\n".join(
            (
                f"VISITOR_LOUNGE_KEY_PEPPER={secrets[0]}",
                f"VISITOR_LOUNGE_MASTER_KEY={secrets[1]}",
                f"VISITOR_LOUNGE_SESSION_SECRET={secrets[2]}",
            )
        ),
        encoding="utf-8",
    )
    return secrets


def _create_fixture_venv(
    project: Path,
    *,
    uvicorn_source: str = "import time\ntime.sleep(60)\n",
    system_site_packages: bool = False,
) -> Path:
    arguments = [sys.executable, "-m", "venv"]
    if system_site_packages:
        arguments.append("--system-site-packages")
    arguments.append(str(project / ".venv"))
    subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    (project / "uvicorn.py").write_text(uvicorn_source, encoding="utf-8")
    return project / ".venv" / "Scripts" / "python.exe"


FIXTURE_PYTHON_HEALTH_SERVER = r"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys

port = int(sys.argv[sys.argv.index("--port") + 1])
service = "visitor" if "visitor_app" in sys.argv[1] else "admin"
if os.environ.get("FIXTURE_HEALTH_MODE") == "wrong_service":
    service = "admin" if service == "visitor" else "visitor"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "service": service}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


DIAGNOSE_HEALTH_SERVER = r"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading

mode = os.environ["FIXTURE_HEALTH_MODE"]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        service = "visitor" if self.server.server_port == 8001 else "admin"
        if mode == "wrong_service":
            service = "admin" if service == "visitor" else "visitor"
        body = json.dumps({"status": "ok", "service": service}).encode("utf-8")
        content_type = "application/json"
        if mode == "wrong_content_type":
            content_type = "text/plain"
        elif mode == "malformed":
            body = b"{"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

servers = [
    ThreadingHTTPServer(("127.0.0.1", 8001), Handler),
    ThreadingHTTPServer(("127.0.0.1", 8002), Handler),
]
for server in servers:
    threading.Thread(target=server.serve_forever, daemon=True).start()
Path(sys.argv[1]).write_text("ready", encoding="ascii")
threading.Event().wait()
"""


def _create_fixture_health_executable(project: Path) -> Path:
    scripts = project / ".venv" / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    source = project / "fixture_health_server.cs"
    source.write_text(
        r"""
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;

public static class FixtureHealthServer {
    public static void Main(string[] args) {
        int port = 0;
        for (int index = 0; index < args.Length - 1; index++) {
            if (args[index] == "--port") port = Int32.Parse(args[index + 1]);
        }
        string expectedService = args[2].Contains("visitor_app") ? "visitor" : "admin";
        string mode = Environment.GetEnvironmentVariable("FIXTURE_HEALTH_MODE") ?? "valid";
        TcpListener listener = new TcpListener(IPAddress.Parse("127.0.0.1"), port);
        listener.Start();
        while (true) {
            using (TcpClient client = listener.AcceptTcpClient())
            using (NetworkStream stream = client.GetStream()) {
                byte[] request = new byte[4096];
                stream.Read(request, 0, request.Length);
                string service = expectedService;
                if (mode == "wrong_service") {
                    service = expectedService == "visitor" ? "admin" : "visitor";
                }
                string body = "{\"status\":\"ok\",\"service\":\"" + service + "\"}";
                string contentType = mode == "wrong_content_type"
                    ? "text/plain" : "application/json";
                if (mode == "malformed") body = "{";
                byte[] bodyBytes = Encoding.UTF8.GetBytes(body);
                string headers = "HTTP/1.1 200 OK\r\nContent-Type: " + contentType
                    + "\r\nContent-Length: " + bodyBytes.Length
                    + "\r\nConnection: close\r\n\r\n";
                byte[] headerBytes = Encoding.ASCII.GetBytes(headers);
                stream.Write(headerBytes, 0, headerBytes.Length);
                stream.Write(bodyBytes, 0, bodyBytes.Length);
            }
        }
    }
}
""",
        encoding="utf-8",
    )
    executable = scripts / "python.exe"
    escaped_source = str(source).replace("'", "''")
    escaped_executable = str(executable).replace("'", "''")
    compiled = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-Command",
            (
                f"Add-Type -Path '{escaped_source}' "
                f"-OutputAssembly '{escaped_executable}' "
                "-OutputType ConsoleApplication"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr
    return executable


def _diagnose_fixture_project(
    tmp_path: Path,
    *,
    with_venv: bool = True,
    config_text: str | None = None,
    master_key: str | None = None,
) -> tuple[Path, dict[str, str], tuple[str, str, str]]:
    aionshome = tmp_path / "AionsHome"
    project = _fixture_script_project(
        aionshome / ".worktrees" / "visitor-lounge"
    )
    source_root = Path(__file__).resolve().parents[1]
    shutil.copytree(source_root / "src", project / "src")
    (project / "config").mkdir()
    (project / "config" / "visitor-lounge.toml").write_text(
        config_text
        if config_text is not None
        else (source_root / "config" / "visitor-lounge.toml").read_text("utf-8"),
        encoding="utf-8",
    )
    shutil.copy2(
        source_root / "config" / "codex_base.md",
        project / "config" / "codex_base.md",
    )
    local_codex = (
        aionshome
        / "Connor-Codex/node_modules/@openai/codex/bin/codex.js"
    )
    local_codex.parent.mkdir(parents=True)
    local_codex.write_text("// fixture", encoding="utf-8")
    aionshome_python = aionshome / ".venv/Scripts/python.exe"
    aionshome_python.parent.mkdir(parents=True)
    os.link(sys.executable, aionshome_python)
    chat_home = aionshome / "fixture-chat-home"
    chat_home.mkdir()
    (chat_home / "auth.json").write_text("{}", encoding="utf-8")
    provider = aionshome / "aion-chat/ai_providers.py"
    provider.parent.mkdir(parents=True)
    provider.write_text(
        "\n".join(
            (
                f"_CODEX_SCRIPT = {str(local_codex)!r}",
                "def _build_codex_chat_command(node, script, workspace, model):",
                "    return [node, script, '-c', 'model_instructions_file=\"owner.md\"', '-c', 'developer_instructions=\"owner\"', '-c', 'features.shell_tool=false', 'app-server', '--stdio']",
                "def _build_codex_chat_environment():",
                f"    return {{'PATH': '', 'CODEX_HOME': {str(chat_home)!r}, 'HOME': {str(aionshome)!r}, 'USERPROFILE': {str(aionshome)!r}}}",
            )
        ),
        encoding="utf-8",
    )
    if with_venv:
        _create_fixture_venv(project, system_site_packages=True)
    secrets = (
        "diagnose-pepper-DO-NOT-PRINT",
        master_key or Fernet.generate_key().decode(),
        "diagnose-session-DO-NOT-PRINT",
    )
    (project / ".env").write_text(
        "\n".join(
            (
                f"VISITOR_LOUNGE_KEY_PEPPER={secrets[0]}",
                f"VISITOR_LOUNGE_MASTER_KEY={secrets[1]}",
                f"VISITOR_LOUNGE_SESSION_SECRET={secrets[2]}",
            )
        ),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "diagnose-fake-bin"
    fake_bin.mkdir()
    (fake_bin / "codex.cmd").write_text("@echo off\nexit /b 0\n", encoding="ascii")
    environment = os.environ.copy()
    environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
    environment["PYTHONPATH"] = str(project / "src")
    return project, environment, secrets


def _start_diagnose_health_server(tmp_path: Path, mode: str) -> subprocess.Popen[str]:
    script = tmp_path / "diagnose_health_server.py"
    ready = tmp_path / "diagnose-health-ready.txt"
    script.write_text(DIAGNOSE_HEALTH_SERVER, encoding="utf-8")
    environment = os.environ.copy()
    environment["FIXTURE_HEALTH_MODE"] = mode
    process = subprocess.Popen(
        [sys.executable, script, ready],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if not ready.exists():
        stdout, stderr = process.communicate(timeout=10)
        raise RuntimeError(f"diagnose health fixture failed: {stdout} {stderr}")
    return process


def _write_fixture_supervisor(project: Path) -> None:
    (project / "scripts" / "supervisor.ps1").write_text(
        r"""
param([string]$ProjectRoot, [string]$LoungeIdentity)
$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$runtime = Join-Path $ProjectRoot '.runtime'
[IO.File]::WriteAllText((Join-Path $runtime 'fixture-supervisor.pid'), [string]$PID)
function Start-Fixture([string]$Factory, [int]$Port) {
    $arguments = @(
        '-m', 'uvicorn', $Factory, '--factory', '--host', '127.0.0.1',
        '--port', [string]$Port, '--app-dir', ('"' + $ProjectRoot + '"')
    )
    return Start-Process -FilePath $python -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
}
$visitor = Start-Fixture 'visitor_lounge.visitor_app:build_visitor_app' 8001
$admin = Start-Fixture 'visitor_lounge.admin_app:build_admin_app' 8002
[IO.File]::WriteAllText((Join-Path $runtime 'visitor.pid'), [string]$visitor.Id)
[IO.File]::WriteAllText((Join-Path $runtime 'admin.pid'), [string]$admin.Id)
[IO.File]::WriteAllText((Join-Path $runtime 'fixture-visitor.pid'), [string]$visitor.Id)
[IO.File]::WriteAllText((Join-Path $runtime 'fixture-admin.pid'), [string]$admin.Id)
Start-Sleep -Seconds 60
""",
        encoding="utf-8",
    )


def _start_fixture_uvicorn(
    project: Path,
    executable: Path,
    *,
    factory: str = "visitor_lounge.visitor_app:build_visitor_app",
    host: str = "127.0.0.1",
    port: int = 8001,
    app_dir: Path | None = None,
    extra_args: tuple[str, ...] = (),
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            executable,
            "-m",
            "uvicorn",
            factory,
            "--factory",
            "--host",
            host,
            "--port",
            str(port),
            "--app-dir",
            app_dir or project,
            *extra_args,
        ],
        cwd=project,
        text=True,
    )


@pytest.mark.parametrize("script_name", SCRIPT_NAMES)
def test_powershell_scripts_parse(script_name: str) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / script_name
    escaped = str(script).replace("'", "''")
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-Command",
            (
                "$ErrorActionPreference='Stop'; "
                f"[void][ScriptBlock]::Create([IO.File]::ReadAllText('{escaped}'))"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_start_validation_creates_only_the_lounge_runtime_path(tmp_path) -> None:
    project = _fixture_script_project(tmp_path)
    _write_fixture_environment(project)

    validated = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts/start.ps1", "-ValidateOnly"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert validated.returncode == 0, validated.stderr
    assert not (project / ".codex-home").exists()


def _start_fixture_process(
    project: Path, *, marker: str, wrong_executable: bool = False
) -> subprocess.Popen[str]:
    del project
    if wrong_executable:
        escaped_marker = marker.replace("'", "''")
        command = [
            _powershell(),
            "-NoProfile",
            "-Command",
            f"$null='{escaped_marker}'; Start-Sleep -Seconds 60",
        ]
    else:
        command = [sys.executable, "-c", "import time; time.sleep(60)", marker]
    return subprocess.Popen(command, text=True)


def test_stop_rejects_unverified_pid_and_stops_only_verified_fixture_process(
    tmp_path,
) -> None:
    project = _fixture_script_project(tmp_path)
    venv_python = _create_fixture_venv(project)
    runtime = project / ".runtime"
    runtime.mkdir()
    stop_script = project / "scripts" / "stop.ps1"
    wrong = _start_fixture_process(
        project,
        marker=f"visitor_lounge.visitor_app:build_visitor_app {project} --port 9999",
    )
    try:
        (runtime / "visitor.pid").write_text(str(wrong.pid), encoding="ascii")
        refused = subprocess.run(
            [_powershell(), "-NoProfile", "-File", stop_script],
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert refused.returncode != 0
        assert wrong.poll() is None
        assert (runtime / "visitor.pid").exists()
    finally:
        if wrong.poll() is None:
            wrong.terminate()
            wrong.wait(timeout=10)
        (runtime / "visitor.pid").unlink(missing_ok=True)

    verified = _start_fixture_uvicorn(project, venv_python)
    try:
        (runtime / "visitor.pid").write_text(str(verified.pid), encoding="ascii")
        stopped = subprocess.run(
            [_powershell(), "-NoProfile", "-File", stop_script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        deadline = time.monotonic() + 5
        while verified.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)

        assert stopped.returncode == 0, stopped.stderr
        assert verified.poll() is not None
        assert not (runtime / "visitor.pid").exists()
    finally:
        if verified.poll() is None:
            verified.terminate()
            verified.wait(timeout=10)


def test_pid_reuse_wrong_project_and_stale_records_are_never_broadly_killed(
    tmp_path,
) -> None:
    project = _fixture_script_project(tmp_path)
    _write_fixture_environment(project)
    runtime = project / ".runtime"
    runtime.mkdir()
    stop_script = project / "scripts" / "stop.ps1"
    status_script = project / "scripts" / "status.ps1"
    wrong_executable = _start_fixture_process(
        project,
        marker=f"visitor_lounge.visitor_app:build_visitor_app {project} --port 8001",
        wrong_executable=True,
    )
    try:
        (runtime / "visitor.pid").write_text(
            str(wrong_executable.pid), encoding="ascii"
        )
        refused_executable = subprocess.run(
            [_powershell(), "-NoProfile", "-File", stop_script],
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert refused_executable.returncode != 0
        assert wrong_executable.poll() is None
    finally:
        if wrong_executable.poll() is None:
            wrong_executable.terminate()
            wrong_executable.wait(timeout=10)

    wrong_project = _start_fixture_process(
        project,
        marker=(
            "visitor_lounge.visitor_app:build_visitor_app "
            f"{tmp_path / 'different-project'} --port 8001"
        ),
    )
    try:
        (runtime / "visitor.pid").write_text(str(wrong_project.pid), encoding="ascii")
        status = subprocess.run(
            [_powershell(), "-NoProfile", "-File", status_script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        refused = subprocess.run(
            [_powershell(), "-NoProfile", "-File", stop_script],
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert status.returncode == 2
        assert refused.returncode != 0
        assert wrong_project.poll() is None
    finally:
        if wrong_project.poll() is None:
            wrong_project.terminate()
            wrong_project.wait(timeout=10)

    stale_pid = wrong_project.pid
    (runtime / "visitor.pid").write_text(str(stale_pid), encoding="ascii")
    stale = subprocess.run(
        [_powershell(), "-NoProfile", "-File", stop_script],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert stale.returncode == 0, stale.stderr
    assert not (runtime / "visitor.pid").exists()


def test_strict_pid_identity_rejects_wrong_runtime_tokens_and_script(tmp_path) -> None:
    project = _fixture_script_project(tmp_path)
    venv_python = _create_fixture_venv(project)
    runtime = project / ".runtime"
    runtime.mkdir()
    stop_script = project / "scripts" / "stop.ps1"
    invalid_processes = [
        _start_fixture_uvicorn(project, Path(sys.executable)),
        _start_fixture_uvicorn(
            project,
            venv_python,
            factory="visitor_lounge.visitor_app:build_visitor_app_evil",
        ),
        _start_fixture_uvicorn(project, venv_python, host="0.0.0.0"),
        _start_fixture_uvicorn(project, venv_python, app_dir=project / "src"),
        _start_fixture_uvicorn(
            project, venv_python, extra_args=("--port=9999",)
        ),
        _start_fixture_uvicorn(
            project, venv_python, extra_args=("--host=0.0.0.0",)
        ),
        _start_fixture_uvicorn(
            project,
            venv_python,
            extra_args=(f"--app-dir={project / 'src'}",),
        ),
        _start_fixture_uvicorn(
            project, venv_python, extra_args=("--port", "8001")
        ),
        _start_fixture_uvicorn(project, venv_python, extra_args=("--reload",)),
    ]
    try:
        for process in invalid_processes:
            (runtime / "visitor.pid").write_text(str(process.pid), encoding="ascii")
            refused = subprocess.run(
                [_powershell(), "-NoProfile", "-File", stop_script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert refused.returncode != 0
            assert process.poll() is None
    finally:
        for process in invalid_processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        (runtime / "visitor.pid").unlink(missing_ok=True)

    wrong_supervisor = project / "scripts" / "not-the-supervisor.ps1"
    wrong_supervisor.write_text(
        "param([string]$ProjectRoot,[string]$LoungeIdentity)\n"
        "Start-Sleep -Seconds 60\n",
        encoding="utf-8",
    )
    supervisor = subprocess.Popen(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            wrong_supervisor,
            "-ProjectRoot",
            project,
            "-LoungeIdentity",
            "visitor_lounge",
        ],
        text=True,
    )
    try:
        (runtime / "supervisor.pid").write_text(str(supervisor.pid), encoding="ascii")
        refused = subprocess.run(
            [_powershell(), "-NoProfile", "-File", stop_script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert refused.returncode != 0
        assert supervisor.poll() is None
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=10)
        (runtime / "supervisor.pid").unlink(missing_ok=True)


def test_strict_pid_identity_stops_exact_project_venv_process(tmp_path) -> None:
    project = _fixture_script_project(tmp_path)
    venv_python = _create_fixture_venv(project)
    runtime = project / ".runtime"
    runtime.mkdir()
    process = _start_fixture_uvicorn(project, venv_python)
    try:
        (runtime / "visitor.pid").write_text(str(process.pid), encoding="ascii")
        stopped = subprocess.run(
            [_powershell(), "-NoProfile", "-File", project / "scripts" / "stop.ps1"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert stopped.returncode == 0, stopped.stderr
        process.wait(timeout=10)
        assert not (runtime / "visitor.pid").exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_stop_refuses_base_worker_without_live_direct_canonical_launcher(
    tmp_path,
) -> None:
    project = _fixture_script_project(tmp_path)
    _create_fixture_venv(project)
    runtime = project / ".runtime"
    runtime.mkdir()
    worker = _start_fixture_uvicorn(project, Path(sys.executable))
    stale_launcher = subprocess.Popen(
        [_powershell(), "-NoProfile", "-Command", "Start-Sleep -Seconds 60"],
        text=True,
    )
    stale_launcher.terminate()
    stale_launcher.wait(timeout=10)
    assert worker.poll() is None
    (runtime / "visitor.pid").write_text(str(worker.pid), encoding="ascii")
    (runtime / "visitor.launcher.pid").write_text(
        str(stale_launcher.pid), encoding="ascii"
    )

    stopped = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts" / "stop.ps1"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    try:
        assert stopped.returncode != 0
        assert worker.poll() is None
        assert (runtime / "visitor.pid").exists()
        assert not (runtime / "visitor.launcher.pid").exists()
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=10)
        (runtime / "visitor.pid").unlink(missing_ok=True)


def test_partial_start_rolls_back_its_only_verified_fixture_child(tmp_path) -> None:
    project = _fixture_script_project(tmp_path)
    _create_fixture_venv(project)
    _write_fixture_environment(project)
    supervisor = project / "scripts" / "supervisor.ps1"
    supervisor.write_text(
        r"""
param([string]$ProjectRoot, [string]$LoungeIdentity)
$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$runtime = Join-Path $ProjectRoot '.runtime'
$arguments = @(
    '-m', 'uvicorn', 'visitor_lounge.visitor_app:build_visitor_app',
    '--factory', '--host', '127.0.0.1', '--port', '8001',
    '--app-dir', ('"' + $ProjectRoot + '"')
)
$child = Start-Process -FilePath $python -ArgumentList $arguments `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
[IO.File]::WriteAllText((Join-Path $runtime 'visitor.pid'), [string]$child.Id)
[IO.File]::WriteAllText((Join-Path $runtime 'fixture-child.pid'), [string]$child.Id)
exit 7
""",
        encoding="utf-8",
    )

    started = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts/start.ps1"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    child_pid_file = project / ".runtime" / "fixture-child.pid"
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    probe = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-Command",
            f"if (Get-Process -Id {child_pid} -ErrorAction SilentlyContinue) {{ exit 1 }}",
        ],
        timeout=10,
    )
    try:
        assert started.returncode != 0
        assert probe.returncode == 0
        assert not (project / ".runtime" / "visitor.pid").exists()
        assert not (project / ".runtime" / "supervisor.pid").exists()
    finally:
        if probe.returncode != 0:
            subprocess.run(
                [
                    _powershell(),
                    "-NoProfile",
                    "-Command",
                    f"Stop-Process -Id {child_pid} -Force -ErrorAction SilentlyContinue",
                ],
                timeout=10,
            )


def _fixture_pid_is_alive(pid: int) -> bool:
    probe = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-Command",
            f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return probe.returncode == 0


def _fixture_listener_owner(port: int) -> int:
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-Command",
            (
                "$owner = Get-NetTCPConnection -State Listen "
                f"-LocalAddress '127.0.0.1' -LocalPort {port} "
                "-ErrorAction Stop | Select-Object -ExpandProperty OwningProcess; "
                "Write-Output $owner"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    owners = [int(line) for line in result.stdout.splitlines() if line.strip()]
    assert len(owners) == 1
    return owners[0]


def _fixture_project_service_pids(project: Path) -> list[int]:
    environment = os.environ.copy()
    environment["FIXTURE_PROJECT_ROOT"] = str(project)
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-Command",
            (
                "$needle=[IO.Path]::GetFullPath($env:FIXTURE_PROJECT_ROOT); "
                "Get-CimInstance Win32_Process | Where-Object { "
                "$_.ProcessId -ne $PID -and $_.CommandLine -and "
                "$_.CommandLine.IndexOf($needle, "
                "[StringComparison]::OrdinalIgnoreCase) -ge 0 -and "
                "$_.CommandLine.IndexOf('visitor_lounge.', "
                "[StringComparison]::Ordinal) -ge 0 } | "
                "Select-Object -ExpandProperty ProcessId"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    return [int(line) for line in result.stdout.splitlines() if line.strip()]


def _start_unrelated_health_server(tmp_path: Path) -> subprocess.Popen[str]:
    ready = tmp_path / "unrelated-health-ready.txt"
    server_script = tmp_path / "unrelated_health_server.py"
    server_script.write_text(
        r"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        service = "visitor" if self.server.server_port == 8001 else "admin"
        body = json.dumps({"status": "ok", "service": service}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

servers = [
    ThreadingHTTPServer(("127.0.0.1", 8001), Handler),
    ThreadingHTTPServer(("127.0.0.1", 8002), Handler),
]
for server in servers:
    threading.Thread(target=server.serve_forever, daemon=True).start()
Path(sys.argv[1]).write_text("ready", encoding="ascii")
threading.Event().wait()
""",
        encoding="utf-8",
    )
    process = subprocess.Popen([sys.executable, server_script, ready], text=True)
    deadline = time.monotonic() + 5
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if not ready.exists():
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        raise RuntimeError("unrelated fixture failed to own both health ports")
    return process


def test_start_requires_both_health_endpoints_before_ready_and_rolls_back(
    tmp_path,
) -> None:
    project = _fixture_script_project(tmp_path)
    _create_fixture_venv(project)
    _write_fixture_environment(project)
    _write_fixture_supervisor(project)
    environment = dict(os.environ)
    environment["VISITOR_LOUNGE_STARTUP_TIMEOUT_SECONDS"] = "1"
    started_at = time.monotonic()
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts" / "start.ps1"],
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
    )
    elapsed = time.monotonic() - started_at
    runtime = project / ".runtime"
    fixture_pid_files = [
        runtime / "fixture-visitor.pid",
        runtime / "fixture-admin.pid",
    ]
    assert all(path.exists() for path in fixture_pid_files)
    fixture_pids = [int(path.read_text(encoding="ascii")) for path in fixture_pid_files]
    supervisor_pid_file = runtime / "fixture-supervisor.pid"
    assert supervisor_pid_file.exists()
    supervisor_pid = int(supervisor_pid_file.read_text(encoding="ascii"))
    try:
        assert result.returncode != 0
        assert elapsed < 8
        assert not any(_fixture_pid_is_alive(pid) for pid in fixture_pids)
        assert not _fixture_pid_is_alive(supervisor_pid)
        for role in ("visitor", "admin", "supervisor"):
            assert not (runtime / f"{role}.pid").exists()
    finally:
        for pid in [*fixture_pids, supervisor_pid]:
            if _fixture_pid_is_alive(pid):
                subprocess.run(
                    [
                        _powershell(),
                        "-NoProfile",
                        "-Command",
                        f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue",
                    ],
                    timeout=10,
                )


def test_start_rejects_health_owned_by_unrelated_process(tmp_path) -> None:
    project = _fixture_script_project(tmp_path)
    _create_fixture_venv(project)
    _write_fixture_environment(project)
    _write_fixture_supervisor(project)
    unrelated = _start_unrelated_health_server(tmp_path)
    environment = dict(os.environ)
    environment["VISITOR_LOUNGE_STARTUP_TIMEOUT_SECONDS"] = "1"
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts" / "start.ps1"],
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
    )
    runtime = project / ".runtime"
    managed_pids = [
        int((runtime / f"fixture-{role}.pid").read_text(encoding="ascii"))
        for role in ("visitor", "admin", "supervisor")
    ]
    try:
        assert result.returncode != 0
        assert unrelated.poll() is None
        assert not any(_fixture_pid_is_alive(pid) for pid in managed_pids)
        for role in ("visitor", "admin", "supervisor"):
            assert not (runtime / f"{role}.pid").exists()
    finally:
        subprocess.run(
            [_powershell(), "-NoProfile", "-File", project / "scripts" / "stop.ps1"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if unrelated.poll() is None:
            unrelated.terminate()
            unrelated.wait(timeout=10)


def test_start_requires_owned_typed_health_and_accepts_canonical_fixture(
    tmp_path,
) -> None:
    project = _fixture_script_project(tmp_path)
    _create_fixture_health_executable(project)
    _write_fixture_environment(project)
    _write_fixture_supervisor(project)
    cases = (
        ("wrong_service", False),
        ("wrong_content_type", False),
        ("malformed", False),
        ("valid", True),
    )
    for mode, expected_success in cases:
        environment = dict(os.environ)
        environment["FIXTURE_HEALTH_MODE"] = mode
        environment["VISITOR_LOUNGE_STARTUP_TIMEOUT_SECONDS"] = (
            "5" if expected_success else "1"
        )
        result = subprocess.run(
            [_powershell(), "-NoProfile", "-File", project / "scripts" / "start.ps1"],
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
        )
        try:
            assert (result.returncode == 0) is expected_success, (
                mode,
                result.stdout,
                result.stderr,
            )
        finally:
            stopped = subprocess.run(
                [
                    _powershell(),
                    "-NoProfile",
                    "-File",
                    project / "scripts" / "stop.ps1",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert stopped.returncode == 0, stopped.stderr


def test_production_supervisor_records_real_venv_listener_workers(tmp_path) -> None:
    project = _fixture_script_project(tmp_path)
    _create_fixture_venv(project, uvicorn_source=FIXTURE_PYTHON_HEALTH_SERVER)
    _write_fixture_environment(project)
    failed_environment = dict(os.environ)
    failed_environment["FIXTURE_HEALTH_MODE"] = "wrong_service"
    failed_environment["VISITOR_LOUNGE_STARTUP_TIMEOUT_SECONDS"] = "1"
    failed = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts" / "start.ps1"],
        capture_output=True,
        text=True,
        timeout=20,
        env=failed_environment,
    )
    runtime = project / ".runtime"
    assert failed.returncode != 0
    assert _fixture_project_service_pids(project) == []
    for record in (
        "visitor.pid",
        "visitor.launcher.pid",
        "admin.pid",
        "admin.launcher.pid",
        "supervisor.pid",
    ):
        assert not (runtime / record).exists()

    environment = dict(os.environ)
    environment["VISITOR_LOUNGE_STARTUP_TIMEOUT_SECONDS"] = "5"
    starter = subprocess.Popen(
        [_powershell(), "-NoProfile", "-File", project / "scripts" / "start.ps1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    deadline = time.monotonic() + 5
    while not (runtime / "admin.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    status_snapshot = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts" / "status.ps1"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    stdout, stderr = starter.communicate(timeout=20)
    started = subprocess.CompletedProcess(starter.args, starter.returncode, stdout, stderr)
    launcher_pids: list[int] = []
    try:
        assert status_snapshot.returncode == 0, status_snapshot.stdout
        assert started.returncode == 0, started.stderr
        visitor_pid = int((runtime / "visitor.pid").read_text(encoding="ascii"))
        admin_pid = int((runtime / "admin.pid").read_text(encoding="ascii"))
        launcher_pids = [
            int((runtime / f"{role}.launcher.pid").read_text(encoding="ascii"))
            for role in ("visitor", "admin")
        ]
        assert visitor_pid == _fixture_listener_owner(8001)
        assert admin_pid == _fixture_listener_owner(8002)
        assert visitor_pid != launcher_pids[0]
        assert admin_pid != launcher_pids[1]
    finally:
        stopped = subprocess.run(
            [_powershell(), "-NoProfile", "-File", project / "scripts" / "stop.ps1"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert stopped.returncode == 0, stopped.stderr
    assert not any(_fixture_pid_is_alive(pid) for pid in launcher_pids)
    assert _fixture_project_service_pids(project) == []
    for role in ("visitor", "admin"):
        assert not (runtime / f"{role}.launcher.pid").exists()


def test_start_rollback_refuses_unverified_child_without_broad_kill(tmp_path) -> None:
    project = _fixture_script_project(tmp_path)
    _write_fixture_environment(project)
    supervisor = project / "scripts" / "supervisor.ps1"
    python = str(Path(sys.executable)).replace("'", "''")
    supervisor.write_text(
        f"""
param([string]$ProjectRoot, [string]$LoungeIdentity)
$runtime = Join-Path $ProjectRoot '.runtime'
[IO.File]::WriteAllText((Join-Path $runtime 'fixture-supervisor.pid'), [string]$PID)
$child = Start-Process -FilePath '{python}' -ArgumentList @(
    '-c', '"import time; time.sleep(60)"', '"unverified-visitor-fixture"'
) -WindowStyle Hidden -PassThru
[IO.File]::WriteAllText((Join-Path $runtime 'visitor.pid'), [string]$child.Id)
[IO.File]::WriteAllText((Join-Path $runtime 'fixture-child.pid'), [string]$child.Id)
Start-Sleep -Seconds 60
""",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["VISITOR_LOUNGE_STARTUP_TIMEOUT_SECONDS"] = "1"
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts" / "start.ps1"],
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
    )
    runtime = project / ".runtime"
    child_pid = int((runtime / "fixture-child.pid").read_text(encoding="ascii"))
    supervisor_pid = int(
        (runtime / "fixture-supervisor.pid").read_text(encoding="ascii")
    )
    try:
        assert result.returncode != 0
        assert _fixture_pid_is_alive(child_pid)
        assert not _fixture_pid_is_alive(supervisor_pid)
        assert (runtime / "visitor.pid").exists()
        assert not (runtime / "supervisor.pid").exists()
    finally:
        if _fixture_pid_is_alive(child_pid):
            subprocess.run(
                [
                    _powershell(),
                    "-NoProfile",
                    "-Command",
                    f"Stop-Process -Id {child_pid} -Force -ErrorAction SilentlyContinue",
                ],
                timeout=10,
            )


def test_status_and_diagnose_never_print_auth_values(tmp_path) -> None:
    project, diagnose_environment, secrets = _diagnose_fixture_project(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_auth = "fixture-global-auth-DO-NOT-PRINT"
    (fake_bin / "codex.cmd").write_text(
        "@echo off\n"
        f"echo {fake_auth}\n"
        f"echo {fake_auth} 1>&2\n"
        "exit /b 0\n",
        encoding="ascii",
    )
    diagnose_environment["PATH"] = (
        str(fake_bin) + os.pathsep + diagnose_environment.get("PATH", "")
    )

    status = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts/status.ps1"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    diagnose = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts/diagnose.ps1"],
        capture_output=True,
        text=True,
        timeout=20,
        env=diagnose_environment,
    )
    combined = status.stdout + status.stderr + diagnose.stdout + diagnose.stderr

    assert status.returncode == 0
    assert fake_auth not in combined
    for secret in secrets:
        assert secret not in combined


def test_diagnose_reports_unavailable_without_shared_aionshome_runtime(tmp_path) -> None:
    project = _fixture_script_project(tmp_path)
    _write_fixture_environment(project)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    diagnosed = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts/diagnose.ps1"],
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )
    combined = diagnosed.stdout + diagnosed.stderr

    assert diagnosed.returncode != 0
    assert "Codex runtime: shared AionsHome profile unavailable" in diagnosed.stdout
    assert "Codex runtime: shared AionsHome profile available" not in diagnosed.stdout
    assert "failed to run" not in combined


@pytest.mark.parametrize(
    ("with_venv", "config_text"),
    [
        (False, None),
        (True, "[visitor\nhost = '127.0.0.1'\n"),
    ],
)
def test_diagnose_fails_closed_when_configuration_cannot_be_loaded(
    tmp_path: Path, with_venv: bool, config_text: str | None
) -> None:
    project, environment, secrets = _diagnose_fixture_project(
        tmp_path, with_venv=with_venv, config_text=config_text
    )

    diagnosed = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts/diagnose.ps1"],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    combined = diagnosed.stdout + diagnosed.stderr

    assert diagnosed.returncode != 0
    assert "configuration: invalid" in diagnosed.stdout
    assert "configuration: valid" not in diagnosed.stdout
    assert all(secret not in combined for secret in secrets)
    assert "Traceback" not in combined


@pytest.mark.parametrize(
    ("case", "config_replacement", "master_key"),
    [
        ("invalid_fernet", None, "diagnose-invalid-fernet-DO-NOT-PRINT"),
        (
            "invalid_timezone",
            ('timezone = "Asia/Shanghai"', 'timezone = "Invalid/NoSuchZone"'),
            None,
        ),
        ("zero_generations", ("max_generations = 1", "max_generations = 0"), None),
        ("excess_generations", ("max_generations = 1", "max_generations = 3"), None),
        ("negative_waiting", ("max_waiting = 3", "max_waiting = -1"), None),
    ],
)
def test_diagnose_rejects_invalid_runtime_settings_without_details(
    tmp_path: Path,
    case: str,
    config_replacement: tuple[str, str] | None,
    master_key: str | None,
) -> None:
    source_root = Path(__file__).resolve().parents[1]
    config_text = (source_root / "config" / "visitor-lounge.toml").read_text("utf-8")
    if config_replacement is not None:
        original, replacement = config_replacement
        assert original in config_text
        config_text = config_text.replace(original, replacement, 1)
    project, environment, secrets = _diagnose_fixture_project(
        tmp_path, config_text=config_text, master_key=master_key
    )

    diagnosed = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts/diagnose.ps1"],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    combined = diagnosed.stdout + diagnosed.stderr

    assert diagnosed.returncode != 0, case
    assert "configuration: invalid" in diagnosed.stdout
    assert "configuration: valid" not in diagnosed.stdout
    assert "Traceback" not in combined
    assert "ValueError" not in combined
    assert all(secret not in combined for secret in secrets)


@pytest.mark.parametrize(
    "mode", ["wrong_service", "wrong_content_type", "malformed"]
)
def test_diagnose_rejects_untyped_or_malformed_health_responses(
    tmp_path: Path, mode: str
) -> None:
    project, environment, secrets = _diagnose_fixture_project(tmp_path)
    server = _start_diagnose_health_server(tmp_path, mode)
    try:
        diagnosed = subprocess.run(
            [_powershell(), "-NoProfile", "-File", project / "scripts/diagnose.ps1"],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    finally:
        server.terminate()
        server.wait(timeout=10)
    combined = diagnosed.stdout + diagnosed.stderr

    assert diagnosed.returncode != 0
    assert diagnosed.stdout.count(": unhealthy") == 2
    assert ": healthy" not in diagnosed.stdout
    assert all(secret not in combined for secret in secrets)


def test_diagnose_succeeds_when_every_required_check_passes(tmp_path: Path) -> None:
    project, environment, secrets = _diagnose_fixture_project(tmp_path)
    database = project / "data" / "visitor-lounge.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE fixture (value INTEGER)")
    server = _start_diagnose_health_server(tmp_path, "valid")
    try:
        diagnosed = subprocess.run(
            [_powershell(), "-NoProfile", "-File", project / "scripts/diagnose.ps1"],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    finally:
        server.terminate()
        server.wait(timeout=10)
    combined = diagnosed.stdout + diagnosed.stderr

    assert diagnosed.returncode == 0, combined
    assert "configuration: valid" in diagnosed.stdout
    assert "database: integrity ok" in diagnosed.stdout
    assert "Codex runtime: shared AionsHome profile available" in diagnosed.stdout
    assert diagnosed.stdout.count(": healthy") == 2
    assert "invalid" not in diagnosed.stdout
    assert "unhealthy" not in diagnosed.stdout
    assert all(secret not in combined for secret in secrets)


@pytest.mark.parametrize(
    ("module", "factory_name", "module_filename"),
    [
        (visitor_app_module, "build_visitor_app", "visitor_app.py"),
        (admin_app_module, "build_admin_app", "admin_app.py"),
    ],
)
def test_app_factories_reject_invalid_loaded_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
    factory_name: str,
    module_filename: str,
) -> None:
    source_root = Path(__file__).resolve().parents[1]
    root = tmp_path / "factory-project"
    shutil.copytree(source_root / "config", root / "config")
    shutil.copytree(source_root / "static", root / "static")
    shutil.copytree(source_root / "templates", root / "templates")
    config = (source_root / "config" / "visitor-lounge.toml").read_text("utf-8")
    (root / "config" / "visitor-lounge.toml").write_text(
        config.replace(
            'timezone = "Asia/Shanghai"', 'timezone = "Invalid/NoSuchZone"', 1
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VISITOR_LOUNGE_KEY_PEPPER", "factory-pepper")
    monkeypatch.setenv("VISITOR_LOUNGE_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VISITOR_LOUNGE_SESSION_SECRET", "factory-session")
    monkeypatch.setattr(
        module,
        "__file__",
        str(root / "src" / "visitor_lounge" / module_filename),
    )

    with pytest.raises(ValueError, match="^invalid admin timezone$"):
        getattr(module, factory_name)()


def _obsolete_codex_home_junction_test(tmp_path) -> None:
    project = _fixture_script_project(tmp_path)
    secrets = _write_fixture_environment(project)
    outside_home = tmp_path / "outside-global-codex-home"
    outside_home.mkdir()
    outside_auth = "outside-auth-material-DO-NOT-PRINT"
    (outside_home / "auth.json").write_text(outside_auth, encoding="utf-8")
    junction = project / ".codex-home"
    escaped_junction = str(junction).replace("'", "''")
    escaped_target = str(outside_home).replace("'", "''")
    subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-Command",
            (
                f"New-Item -ItemType Junction -Path '{escaped_junction}' "
                f"-Target '{escaped_target}' | Out-Null"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    fake_bin = tmp_path / "junction-fake-bin"
    fake_bin.mkdir()
    capture = tmp_path / "junction-codex-ran.txt"
    (fake_bin / "codex.cmd").write_text(
        "@echo off\n"
        "> \"%VISITOR_LOUNGE_CODEX_CAPTURE%\" echo invoked\n"
        "exit /b 0\n",
        encoding="ascii",
    )
    environment = os.environ.copy()
    environment["PATH"] = (
        str(fake_bin) + os.pathsep + environment.get("PATH", "")
    )
    environment["CODEX_HOME"] = str(outside_home)
    environment["VISITOR_LOUNGE_CODEX_CAPTURE"] = str(capture)

    prepared = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            project / "scripts" / "init-codex.ps1",
            "-PrepareOnly",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    )
    diagnosed = subprocess.run(
        [_powershell(), "-NoProfile", "-File", project / "scripts" / "diagnose.ps1"],
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )
    combined = prepared.stdout + prepared.stderr + diagnosed.stdout + diagnosed.stderr

    assert prepared.returncode != 0
    assert diagnosed.returncode != 0
    assert not capture.exists()
    assert outside_auth not in combined
    for secret in secrets:
        assert secret not in combined
