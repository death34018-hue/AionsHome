import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from security_access import IpGeolocationResolver, SecurityAccessService, classify_ip


def make_scope(
    ip: str,
    path: str = "/api/chat",
    *,
    query: bytes = b"",
    scheme: str = "http",
    scope_type: str = "http",
) -> dict:
    return {
        "type": scope_type,
        "scheme": scheme,
        "client": (ip, 54321),
        "method": "GET",
        "path": path,
        "query_string": query,
    }


def cookie_header(set_cookie: str) -> str:
    return set_cookie.split(";", 1)[0]


def event_types(observation) -> list[str]:
    return [event["event"] for event in observation.events]


class FakeClock:
    def __init__(self, value: float = 1_800_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class SecurityAccessTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.service = SecurityAccessService(Path(self.temp_dir.name))


class SecurityAccessClassificationTests(SecurityAccessTestCase):
    def test_classifies_source_networks(self):
        self.assertEqual("localhost", classify_ip("127.0.0.1"))
        self.assertEqual("localhost", classify_ip("::1"))
        self.assertEqual("lan", classify_ip("192.168.1.178"))
        self.assertEqual("tailscale", classify_ip("100.64.0.42"))
        self.assertEqual("public", classify_ip("8.8.8.8"))
        self.assertEqual("cloudflare", classify_ip("8.8.8.8", True))

    def test_event_never_contains_secrets_or_query_string(self):
        observation = self.service.observe(
            make_scope("8.8.8.8", "/api/chat", query=b"token=QUERY_SENTINEL"),
            {
                "cookie": "session=COOKIE_SENTINEL",
                "authorization": "Bearer AUTH_SENTINEL",
                "user-agent": "safe-agent",
            },
        )

        payload = json.dumps(observation.events, ensure_ascii=False)

        self.assertNotIn("SENTINEL", payload)
        self.assertNotIn("token=", payload)
        self.assertIn('"path": "/api/chat"', payload)

    def test_user_agent_is_truncated_and_cloudflare_is_metadata_only(self):
        observation = self.service.observe(
            make_scope("8.8.8.8"),
            {
                "user-agent": "x" * 300,
                "cf-connecting-ip": "1.2.3.4",
                "cf-ipcountry": "CN",
            },
        )

        event = observation.events[0]
        self.assertEqual("8.8.8.8", event["ip"])
        self.assertEqual("cloudflare", event["source"])
        self.assertEqual("CN", event["cf_country"])
        self.assertEqual(160, len(event["user_agent"]))


class IpGeolocationResolverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.clock = FakeClock()

    async def test_private_networks_are_described_without_external_lookup(self):
        calls = []

        def fetch(ip, timeout):
            calls.append((ip, timeout))
            raise AssertionError("private IP must not be sent to provider")

        resolver = IpGeolocationResolver(
            Path(self.temp_dir.name),
            clock=self.clock,
            fetcher=fetch,
        )

        cases = [
            ("192.168.1.178", "lan", "局域网设备（本地网络）"),
            ("127.0.0.1", "localhost", "服务器本机"),
            ("100.64.0.42", "tailscale", "Tailscale 私有网络"),
        ]
        for ip, source, expected in cases:
            result = await resolver.resolve(ip, source)
            self.assertEqual(expected, result["location"])

        self.assertEqual([], calls)

    async def test_public_location_is_sanitized_and_cached_for_seven_days(self):
        calls = []

        def fetch(ip, timeout):
            calls.append((ip, timeout))
            return {
                "success": True,
                "country_code": "CN",
                "country": "中国",
                "region": "北京",
                "city": "北京",
                "latitude": 39.9,
                "longitude": 116.4,
                "postal": "100000",
                "connection": {"isp": "China Unicom", "domain": "example.test"},
            }

        resolver = IpGeolocationResolver(
            Path(self.temp_dir.name),
            clock=self.clock,
            fetcher=fetch,
        )

        first = await resolver.resolve("8.8.8.8", "cloudflare")
        self.clock.advance(6 * 86400)
        second = await resolver.resolve("8.8.8.8", "cloudflare")

        self.assertEqual("中国 · 北京 · China Unicom", first["location"])
        self.assertEqual("beijing", first["location_kind"])
        self.assertEqual("常用地区（仍需确认设备）", first["location_notice"])
        self.assertEqual(first, second)
        self.assertEqual([("8.8.8.8", 2.0)], calls)
        cache_text = (Path(self.temp_dir.name) / "geo_cache.json").read_text("utf-8")
        self.assertNotIn("latitude", cache_text)
        self.assertNotIn("longitude", cache_text)
        self.assertNotIn("postal", cache_text)
        self.assertNotIn("domain", cache_text)

    async def test_worker_broadcasts_enriched_alert_without_blocking_observe(self):
        def fetch(ip, timeout):
            return {
                "success": True,
                "country_code": "US",
                "country": "United States",
                "region": "California",
                "city": "Mountain View",
                "connection": {"isp": "Google LLC"},
            }

        data_dir = Path(self.temp_dir.name)
        resolver = IpGeolocationResolver(data_dir, clock=self.clock, fetcher=fetch)
        service = SecurityAccessService(
            data_dir,
            clock=self.clock,
            geo_resolver=resolver,
        )
        broadcasts = []

        async def broadcast(payload):
            broadcasts.append(payload)

        service.start(broadcast)
        observation = service.observe(make_scope("8.8.8.8"), {})
        self.assertNotIn("location", observation.alerts[0])
        service.submit(observation)
        await service.stop()

        alert = broadcasts[0]["data"]
        self.assertEqual("United States · California · Mountain View · Google LLC", alert["location"])
        self.assertEqual("overseas", alert["location_kind"])
        self.assertEqual("境外访问，请提高警惕", alert["location_notice"])


class SecurityAccessDeviceTests(SecurityAccessTestCase):
    def test_trusting_alert_source_trusts_the_device_that_triggered_it(self):
        source = self.service.observe(make_scope("192.168.1.178"), {})
        alert = source.alerts[0]

        trust_event = self.service.trust_alert_source(alert["alert_id"], "phone")
        revisited = self.service.observe(
            make_scope("192.168.1.99"),
            {"cookie": cookie_header(source.set_cookie)},
        )
        legacy_background_revisit = self.service.observe(make_scope("192.168.1.178"), {})

        self.assertEqual(source.device_fingerprint, trust_event["device"])
        self.assertTrue(revisited.trusted)
        self.assertEqual([], list(revisited.alerts))
        self.assertTrue(legacy_background_revisit.trusted)
        self.assertEqual([], list(legacy_background_revisit.alerts))
        restarted = SecurityAccessService(Path(self.temp_dir.name))
        self.assertTrue(restarted.observe(make_scope("192.168.1.178"), {}).trusted)

    def test_trusted_device_ip_change_logs_without_alert(self):
        first = self.service.observe(make_scope("192.168.1.178"), {})
        trust_event = self.service.trust_device(
            first.device_id,
            first.effective_ip,
            "phone",
        )

        changed = self.service.observe(
            make_scope("192.168.1.99"),
            {"cookie": cookie_header(first.set_cookie)},
        )

        self.assertEqual("device_trusted", trust_event["event"])
        self.assertTrue(changed.trusted)
        self.assertEqual([], list(changed.alerts))
        self.assertIn("trusted_device_ip_changed", event_types(changed))

    def test_invalid_signature_gets_a_new_untrusted_identity(self):
        first = self.service.observe(make_scope("192.168.1.178"), {})
        forged = cookie_header(first.set_cookie) + "forged"

        second = self.service.observe(
            make_scope("192.168.1.178"),
            {"cookie": forged},
        )

        self.assertNotEqual(first.device_id, second.device_id)
        self.assertFalse(second.trusted)
        self.assertIsNotNone(second.set_cookie)
        self.assertNotIn(forged, json.dumps(second.events))

    def test_loopback_is_trusted_without_registration(self):
        observation = self.service.observe(make_scope("127.0.0.1"), {})

        self.assertTrue(observation.trusted)
        self.assertEqual([], list(observation.alerts))

    def test_trust_registry_and_cookie_secret_survive_restart(self):
        first = self.service.observe(make_scope("192.168.1.178"), {})
        cookie = cookie_header(first.set_cookie)
        self.service.trust_device(first.device_id, first.effective_ip, "phone")

        restarted = SecurityAccessService(Path(self.temp_dir.name))
        observation = restarted.observe(
            make_scope("192.168.1.99"),
            {"cookie": cookie},
        )

        self.assertTrue(observation.trusted)
        self.assertEqual("phone", restarted.trusted_devices()[0]["label"])
        self.assertNotIn(first.device_id, json.dumps(restarted.trusted_devices()))


class SecurityAccessRateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.clock = FakeClock()
        self.service = SecurityAccessService(
            Path(self.temp_dir.name),
            clock=self.clock,
        )

    def test_unknown_ip_does_not_duplicate_during_initial_request_burst(self):
        first = self.service.observe(make_scope("8.8.8.8"), {})
        repeated = self.service.observe(make_scope("8.8.8.8"), {})

        self.assertEqual(["warning"], [a["level"] for a in first.alerts])
        self.assertEqual([], list(repeated.alerts))

    def test_anonymous_daily_log_is_deduplicated_by_ip(self):
        first = self.service.observe(make_scope("8.8.8.8"), {})
        repeated = self.service.observe(make_scope("8.8.8.8"), {})

        self.assertIn("daily_first_seen", event_types(first))
        self.assertEqual([], event_types(repeated))

    def test_single_unknown_ip_escalates_at_180_requests(self):
        serious = []
        for _ in range(180):
            observation = self.service.observe(make_scope("8.8.4.4"), {})
            serious.extend(a for a in observation.alerts if a["level"] == "serious")

        self.assertEqual(1, len(serious))
        self.assertEqual("single_ip_rate", serious[0]["reason"])
        self.assertEqual(180, serious[0]["request_count"])

    def test_ten_distinct_unknown_ips_escalate_once(self):
        serious = []
        for suffix in range(1, 11):
            observation = self.service.observe(
                make_scope(f"203.0.113.{suffix}"),
                {},
            )
            serious.extend(a for a in observation.alerts if a["level"] == "serious")

        self.assertEqual(1, len(serious))
        self.assertEqual("distinct_ip_rate", serious[0]["reason"])
        self.assertEqual(10, serious[0]["distinct_ip_count"])

    def test_serious_alert_obeys_cooldown_and_trusted_device_is_not_counted(self):
        service = SecurityAccessService(
            Path(self.temp_dir.name) / "cooldown",
            clock=self.clock,
            single_ip_limit=3,
            serious_cooldown_seconds=30,
        )
        first = service.observe(make_scope("192.168.1.178"), {})
        service.trust_device(first.device_id, first.effective_ip, "phone")
        cookie = {"cookie": cookie_header(first.set_cookie)}
        for _ in range(10):
            trusted = service.observe(make_scope("192.168.1.178"), cookie)
        self.assertFalse(trusted.alerts)

        alerts = []
        for _ in range(6):
            current = service.observe(make_scope("8.8.8.8"), {})
            alerts.extend(a for a in current.alerts if a["level"] == "serious")
        self.assertEqual(1, len(alerts))

        self.clock.advance(61)
        for _ in range(3):
            current = service.observe(make_scope("8.8.8.8"), {})
            alerts.extend(a for a in current.alerts if a["level"] == "serious")
        self.assertEqual(2, len(alerts))

    def test_alert_control_requests_do_not_feed_rate_window(self):
        service = SecurityAccessService(
            Path(self.temp_dir.name) / "excluded",
            clock=self.clock,
            single_ip_limit=2,
        )
        for _ in range(5):
            result = service.observe(
                make_scope("8.8.8.8", "/api/security-access/alerts/pending"),
                {},
            )

        self.assertFalse(any(a["level"] == "serious" for a in result.alerts))


class SecurityAccessAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.clock = FakeClock()
        self.data_dir = Path(self.temp_dir.name)
        self.service = SecurityAccessService(self.data_dir, clock=self.clock)

    async def _persist_warning(self, ip="192.168.1.178"):
        async def ignore_broadcast(payload):
            return None

        self.service.start(ignore_broadcast)
        observation = self.service.observe(make_scope(ip), {})
        self.service.submit(observation)
        await self.service.stop()
        return observation.alerts[0]

    async def test_unacknowledged_warning_reappears_after_reopen_but_ack_starts_24_hour_cooldown(self):
        alert = await self._persist_warning()
        self.clock.advance(86401)

        while_pending = self.service.observe(make_scope("192.168.1.178"), {})
        self.assertEqual([], list(while_pending.alerts))
        self.assertEqual([alert["alert_id"]], [item["alert_id"] for item in self.service.pending_alerts()])

        self.assertTrue(self.service.acknowledge_alert(alert["alert_id"]))
        during_cooldown = self.service.observe(make_scope("192.168.1.178"), {})
        self.assertEqual([], list(during_cooldown.alerts))

        self.clock.advance(86401)
        after_cooldown = self.service.observe(make_scope("192.168.1.178"), {})
        self.assertEqual(1, len(after_cooldown.alerts))

    async def test_acknowledgement_cooldown_survives_restart(self):
        alert = await self._persist_warning()
        self.assertTrue(self.service.acknowledge_alert(alert["alert_id"]))

        restarted = SecurityAccessService(self.data_dir, clock=self.clock)
        observation = restarted.observe(make_scope("192.168.1.178"), {})

        self.assertEqual([], list(observation.alerts))

    async def test_restart_collapses_duplicate_unacknowledged_warnings_by_ip(self):
        alert = await self._persist_warning()
        state_path = self.data_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        duplicate = {**state["alerts"][0], "alert_id": "duplicate-alert", "device": "other-device"}
        state["alerts"].append(duplicate)
        state_path.write_text(json.dumps(state), encoding="utf-8")

        restarted = SecurityAccessService(self.data_dir, clock=self.clock)

        pending = restarted.pending_alerts()
        self.assertEqual(1, len(pending))
        self.assertEqual(alert["ip"], pending[0]["ip"])


if __name__ == "__main__":
    unittest.main()
