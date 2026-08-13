import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.security_access import create_security_access_router
from security_access import SecurityAccessMiddleware, SecurityAccessService
from test_security_access import make_scope


async def call_asgi(app, scope):
    messages = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if scope["type"] == "websocket":
            if not request_sent:
                request_sent = True
                return {"type": "websocket.connect"}
            return {"type": "websocket.disconnect", "code": 1000}
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


async def normal_http_app(scope, receive, send):
    assert scope["type"] == "http"
    await send(
        {
            "type": "http.response.start",
            "status": 201,
            "headers": [(b"x-existing", b"kept")],
        }
    )
    await send({"type": "http.response.body", "body": b"normal response"})


class SecurityAccessMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.service = SecurityAccessService(Path(self.temp_dir.name))

    async def _cleanup(self):
        await self.service.stop()
        self.temp_dir.cleanup()

    async def test_http_response_is_preserved_and_receives_device_cookie(self):
        app = SecurityAccessMiddleware(normal_http_app, self.service)

        messages = await call_asgi(app, make_scope("192.168.1.178"))

        start = messages[0]
        self.assertEqual(201, start["status"])
        self.assertIn((b"x-existing", b"kept"), start["headers"])
        self.assertTrue(any(name == b"set-cookie" for name, _ in start["headers"]))
        self.assertEqual(b"normal response", messages[1]["body"])

    async def test_audit_exception_does_not_change_response(self):
        class BrokenAudit:
            def observe(self, scope, headers):
                raise OSError("audit unavailable")

        app = SecurityAccessMiddleware(normal_http_app, BrokenAudit())

        with self.assertLogs("security_access", level="WARNING"):
            messages = await call_asgi(app, make_scope("192.168.1.178"))

        self.assertEqual(201, messages[0]["status"])
        self.assertEqual([(b"x-existing", b"kept")], messages[0]["headers"])
        self.assertEqual(b"normal response", messages[1]["body"])

    async def test_websocket_scope_is_observed_without_changing_handshake(self):
        observed = {}

        async def websocket_app(scope, receive, send):
            observed.update(scope["state"]["security_access"].__dict__)
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})

        app = SecurityAccessMiddleware(websocket_app, self.service)
        scope = make_scope(
            "100.64.0.42",
            "/ws",
            scope_type="websocket",
        )

        messages = await call_asgi(app, scope)

        self.assertEqual("websocket.accept", messages[0]["type"])
        self.assertEqual("websocket.close", messages[1]["type"])
        self.assertEqual("tailscale", observed["source"])

    async def test_worker_writes_jsonl_then_broadcasts_and_drains_on_stop(self):
        broadcasts = []

        async def broadcast(payload):
            broadcasts.append(payload)

        self.service.start(broadcast)
        observation = self.service.observe(make_scope("8.8.8.8"), {})
        self.service.submit(observation)
        await self.service.stop()

        log_files = list(Path(self.temp_dir.name).glob("*.jsonl"))
        self.assertEqual(1, len(log_files))
        events = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
        self.assertIn("unknown_device", [event["event"] for event in events])
        self.assertEqual("security_alert", broadcasts[0]["type"])
        self.assertEqual("warning", broadcasts[0]["data"]["level"])
        self.assertEqual(1, len(self.service.pending_alerts()))

    async def test_unusable_data_path_does_not_prevent_service_or_request(self):
        blocker = Path(self.temp_dir.name) / "blocked-data-dir"
        blocker.write_text("not a directory", encoding="utf-8")

        with self.assertLogs("security_access", level="WARNING"):
            service = SecurityAccessService(blocker)
        self.addAsyncCleanup(service.stop)
        app = SecurityAccessMiddleware(normal_http_app, service)

        messages = await call_asgi(app, make_scope("192.168.1.178"))

        self.assertEqual(201, messages[0]["status"])
        self.assertEqual(b"normal response", messages[1]["body"])

    async def test_disk_or_broadcast_failure_never_escapes_stop(self):
        async def broken_broadcast(payload):
            raise RuntimeError("socket unavailable")

        self.service.start(broken_broadcast)
        blocker = Path(self.temp_dir.name) / "not-a-directory"
        blocker.write_text("block", encoding="utf-8")
        self.service.data_dir = blocker
        observation = self.service.observe(make_scope("9.9.9.9"), {})
        self.service.submit(observation)

        with self.assertLogs("security_access", level="WARNING"):
            await self.service.stop()


class SecurityAccessRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.service = SecurityAccessService(Path(self.temp_dir.name))
        app = FastAPI()
        app.add_middleware(SecurityAccessMiddleware, service=self.service)
        app.include_router(create_security_access_router(self.service))

        @app.get("/probe")
        async def probe():
            return {"normal": True}

        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _persist_unknown_alert(self, ip: str = "8.8.8.8") -> dict:
        async def persist():
            async def ignore_broadcast(payload):
                return None

            self.service.start(ignore_broadcast)
            observation = self.service.observe(make_scope(ip), {})
            self.service.submit(observation)
            await self.service.stop()
            return observation.alerts[0]

        return asyncio.run(persist())

    def test_trust_endpoint_uses_current_device_and_sanitizes_label(self):
        probe = self.client.get("/probe")
        self.assertEqual(200, probe.status_code)

        response = self.client.post(
            "/api/security-access/devices/trust",
            json={"label": "iPad" + "x" * 100},
        )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertTrue(payload["trusted"])
        self.assertEqual(40, len(payload["label"]))
        self.assertEqual(12, len(payload["device"]))
        self.assertNotIn("device_id", payload)
        self.assertTrue(any(
            '"event":"device_trusted"' in path.read_text(encoding="utf-8")
            for path in Path(self.temp_dir.name).glob("*.jsonl")
        ))

    def test_trust_alert_source_endpoint_trusts_source_not_confirming_browser(self):
        source = self.service.observe(make_scope("192.168.1.178"), {})
        self.service._pending_alerts[source.alerts[0]["alert_id"]] = source.alerts[0]
        confirmer = self.client.get("/probe")

        response = self.client.post(
            f"/api/security-access/alerts/{source.alerts[0]['alert_id']}/trust-source",
            json={"label": "phone"},
        )
        revisited = self.service.observe(
            make_scope("192.168.1.99"),
            {"cookie": source.set_cookie.split(";", 1)[0]},
        )

        self.assertEqual(200, confirmer.status_code)
        self.assertEqual(200, response.status_code)
        self.assertEqual(source.device_fingerprint, response.json()["device"])
        self.assertTrue(revisited.trusted)
        self.assertEqual([], self.service.pending_alerts())

    def test_pending_and_acknowledgement_are_sanitized_and_idempotent(self):
        alert = self._persist_unknown_alert()
        self.client.get("/probe")
        self.client.post("/api/security-access/devices/trust", json={"label": "PC"})

        pending = self.client.get("/api/security-access/alerts/pending")
        first_ack = self.client.post(
            f"/api/security-access/alerts/{alert['alert_id']}/ack"
        )
        second_ack = self.client.post(
            f"/api/security-access/alerts/{alert['alert_id']}/ack"
        )

        self.assertEqual([alert["alert_id"]], [a["alert_id"] for a in pending.json()["alerts"]])
        self.assertNotIn("device_id", json.dumps(pending.json()))
        self.assertTrue(first_ack.json()["acknowledged"])
        self.assertFalse(second_ack.json()["acknowledged"])
        self.assertEqual([], self.client.get("/api/security-access/alerts/pending").json()["alerts"])

    def test_untrusted_client_cannot_read_another_ips_pending_alert(self):
        foreign_alert = self._persist_unknown_alert()

        response = self.client.get("/api/security-access/alerts/pending")
        alerts = response.json()["alerts"]

        self.assertNotIn(foreign_alert["alert_id"], [alert["alert_id"] for alert in alerts])
        self.assertTrue(alerts)
        self.assertTrue(all(alert["ip"] == "testclient" for alert in alerts))

    def test_trusting_device_clears_its_ip_alerts_but_keeps_foreign_evidence(self):
        own = self._persist_unknown_alert("testclient")
        foreign = self._persist_unknown_alert("8.8.8.8")
        self.client.get("/probe")

        trusted = self.client.post(
            "/api/security-access/devices/trust",
            json={"label": "phone"},
        )
        remaining = {alert["alert_id"] for alert in self.service.pending_alerts()}

        self.assertEqual(200, trusted.status_code)
        self.assertNotIn(own["alert_id"], remaining)
        self.assertIn(foreign["alert_id"], remaining)

    def test_missing_middleware_state_affects_only_security_endpoint(self):
        app = FastAPI()
        app.include_router(create_security_access_router(self.service))

        @app.get("/normal")
        async def normal():
            return {"ok": True}

        with TestClient(app) as client:
            normal = client.get("/normal")
            security = client.post("/api/security-access/devices/trust", json={})

        self.assertEqual(200, normal.status_code)
        self.assertEqual(503, security.status_code)


if __name__ == "__main__":
    unittest.main()
