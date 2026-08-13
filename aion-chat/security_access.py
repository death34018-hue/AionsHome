"""Independent, fail-open access auditing for AionsHome."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import base64
import asyncio
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Mapping


COOKIE_NAME = "aion_security_device"
log = logging.getLogger("security_access")


def classify_ip(ip: str, has_cloudflare_metadata: bool = False) -> str:
    if has_cloudflare_metadata:
        return "cloudflare"
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "public"
    if address.is_loopback:
        return "localhost"
    if address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10"):
        return "tailscale"
    if address.is_private:
        return "lan"
    return "public"


def _device_fingerprint(device_id: str) -> str:
    return hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:12]


class IpGeolocationResolver:
    """Resolve alert-only IP locations without touching the request hot path."""

    SUCCESS_TTL_SECONDS = 7 * 86400
    FAILURE_TTL_SECONDS = 3600
    LOOKUP_TIMEOUT_SECONDS = 2.0

    def __init__(
        self,
        data_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
        fetcher: Callable[[str, float], Mapping] | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.clock = clock
        self.fetcher = fetcher or self._fetch
        self._cache_path = self.data_dir / "geo_cache.json"
        self._cache = self._read_cache()

    def _read_cache(self) -> dict[str, dict]:
        try:
            value = json.loads(self._cache_path.read_text(encoding="utf-8"))
            records = value.get("records", {}) if isinstance(value, dict) else {}
            return records if isinstance(records, dict) else {}
        except (FileNotFoundError, OSError, ValueError):
            return {}

    def _persist_cache(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._cache_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps({"version": 1, "records": self._cache}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self._cache_path)
        except OSError as exc:
            log.warning("security geolocation cache is memory-only: %s", type(exc).__name__)

    @staticmethod
    def _fetch(ip: str, timeout: float) -> Mapping:
        encoded_ip = urllib.parse.quote(ip, safe=":")
        request = urllib.request.Request(
            f"https://ipwho.is/{encoded_ip}",
            headers={"Accept": "application/json", "User-Agent": "AionsHome-security-alert/1"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _text(value, limit: int = 80) -> str:
        return str(value or "").strip()[:limit]

    @classmethod
    def _local_result(cls, ip: str, source: str) -> dict | None:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return {
                "location": "暂时无法确定",
                "location_kind": "unknown",
                "location_notice": "",
            }
        if address.is_loopback or source == "localhost":
            location = "服务器本机"
        elif (
            source == "tailscale"
            or address.version == 4
            and address in ipaddress.ip_network("100.64.0.0/10")
        ):
            location = "Tailscale 私有网络"
        elif address.is_private or source == "lan":
            location = "局域网设备（本地网络）"
        elif not address.is_global:
            location = "暂时无法确定"
        else:
            return None
        return {"location": location, "location_kind": "local", "location_notice": ""}

    @classmethod
    def _sanitize_provider_result(cls, payload: Mapping) -> tuple[dict, bool]:
        if payload.get("success") is not True:
            return ({"location": "暂时无法确定", "location_kind": "unknown", "location_notice": ""}, False)

        country_code = cls._text(payload.get("country_code"), 8).upper()
        country = cls._text(payload.get("country"))
        region = cls._text(payload.get("region"))
        city = cls._text(payload.get("city"))
        connection = payload.get("connection")
        isp = cls._text(connection.get("isp")) if isinstance(connection, Mapping) else ""
        parts = []
        for value in (country, region, city, isp):
            if value and value not in parts:
                parts.append(value)

        if country_code == "CN" and any("北京" in value or "BEIJING" in value.upper() for value in (region, city)):
            kind, notice = "beijing", "常用地区（仍需确认设备）"
        elif country_code == "CN":
            kind, notice = "domestic", "国内异地访问"
        elif country_code:
            kind, notice = "overseas", "境外访问，请提高警惕"
        else:
            kind, notice = "unknown", ""
        return (
            {
                "location": " · ".join(parts) or "暂时无法确定",
                "location_kind": kind,
                "location_notice": notice,
                "country_code": country_code,
                "country": country,
                "region": region,
                "city": city,
                "isp": isp,
            },
            bool(parts),
        )

    async def resolve(self, ip: str, source: str) -> dict:
        local = self._local_result(ip, source)
        if local is not None:
            return local

        now = self.clock()
        cached = self._cache.get(ip)
        if isinstance(cached, dict) and float(cached.get("expires_at", 0)) > now:
            return {name: value for name, value in cached.items() if name != "expires_at"}

        try:
            payload = await asyncio.to_thread(
                self.fetcher,
                ip,
                self.LOOKUP_TIMEOUT_SECONDS,
            )
            result, success = self._sanitize_provider_result(payload)
        except Exception as exc:
            log.warning("security geolocation lookup failed: %s", type(exc).__name__)
            result = {"location": "暂时无法确定", "location_kind": "unknown", "location_notice": ""}
            success = False
        self._cache[ip] = {
            **result,
            "expires_at": now + (self.SUCCESS_TTL_SECONDS if success else self.FAILURE_TTL_SECONDS),
        }
        self._persist_cache()
        return result


@dataclass(frozen=True)
class AccessObservation:
    device_id: str
    device_fingerprint: str
    trusted: bool
    blocked: bool
    effective_ip: str
    source: str
    set_cookie: str | None
    events: tuple[dict, ...]
    alerts: tuple[dict, ...]


class SecurityAccessService:
    def __init__(
        self,
        data_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
        warning_ttl_seconds: int = 86400,
        single_ip_limit: int = 180,
        distinct_ip_limit: int = 10,
        serious_cooldown_seconds: int = 1800,
        queue_size: int = 512,
        geo_resolver: IpGeolocationResolver | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.clock = clock
        self.warning_ttl_seconds = warning_ttl_seconds
        self.single_ip_limit = single_ip_limit
        self.distinct_ip_limit = distinct_ip_limit
        self.serious_cooldown_seconds = serious_cooldown_seconds
        self.queue_size = queue_size
        self.geo_resolver = geo_resolver or IpGeolocationResolver(self.data_dir, clock=clock)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("security audit data directory unavailable: %s", type(exc).__name__)
        self._state_path = self.data_dir / "state.json"
        self._devices_path = self.data_dir / "devices.json"
        self._secret = b""
        self._devices: dict[str, dict] = {}
        self._trusted_ips: dict[str, dict] = {}
        self._daily_seen: set[tuple[str, str]] = set()
        self._warning_last: dict[str, float] = {}
        self._acknowledged_until: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}
        self._ip_windows: dict[str, deque[float]] = {}
        self._unknown_ip_last: dict[str, float] = {}
        self._serious_last: dict[str, float] = {}
        self._pending_alerts: dict[str, dict] = {}
        self._alert_device_ids: dict[str, str] = {}
        self._alert_source_details: dict[str, tuple[str, str]] = {}
        self._queue: asyncio.Queue[AccessObservation | None] | None = None
        self._worker_task: asyncio.Task | None = None
        self._broadcast: Callable[[dict], Awaitable[None]] | None = None
        self._load_state()
        self._load_devices()

    @staticmethod
    def _read_json(path: Path, default: dict) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else default
        except (FileNotFoundError, OSError, ValueError):
            return default

    @staticmethod
    def _write_json_atomic(path: Path, value: dict) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _load_state(self) -> None:
        state = self._read_json(self._state_path, {})
        alerts = state.get("alerts", [])
        persist_needed = False
        if isinstance(alerts, list):
            selected_alerts = []
            warning_keys = set()
            for alert in reversed(alerts):
                if not isinstance(alert, dict) or not alert.get("alert_id"):
                    continue
                if alert.get("reason") == "unknown_device":
                    warning_key = str(alert.get("ip") or alert.get("device") or alert["alert_id"])
                    if warning_key in warning_keys:
                        persist_needed = True
                        continue
                    warning_keys.add(warning_key)
                selected_alerts.append(alert)
            selected_alerts.reverse()
            self._pending_alerts = {
                str(alert["alert_id"]): alert
                for alert in selected_alerts
            }
        acknowledged = state.get("acknowledged_until", {})
        now = self.clock()
        if isinstance(acknowledged, dict):
            for key, expires_at in acknowledged.items():
                try:
                    expires_at = float(expires_at)
                except (TypeError, ValueError):
                    persist_needed = True
                    continue
                if expires_at > now:
                    self._acknowledged_until[str(key)] = expires_at
                else:
                    persist_needed = True
        blocked = state.get("blocked_until", {})
        if isinstance(blocked, dict):
            for ip, expires_at in blocked.items():
                try:
                    expires_at = float(expires_at)
                except (TypeError, ValueError):
                    persist_needed = True
                    continue
                if expires_at > now:
                    self._blocked_until[str(ip)] = expires_at
                else:
                    persist_needed = True
        encoded_secret = state.get("cookie_secret", "")
        try:
            self._secret = base64.urlsafe_b64decode(encoded_secret.encode("ascii"))
        except (ValueError, UnicodeError):
            self._secret = b""
        if len(self._secret) < 32:
            self._secret = secrets.token_bytes(32)
            persist_needed = True
        if persist_needed:
            try:
                self._persist_state()
            except OSError as exc:
                log.warning("security cookie state is memory-only: %s", type(exc).__name__)

    def _persist_state(self) -> None:
        self._write_json_atomic(
            self._state_path,
            {
                "version": 2,
                "cookie_secret": base64.urlsafe_b64encode(self._secret).decode("ascii"),
                "alerts": list(self._pending_alerts.values()),
                "acknowledged_until": self._acknowledged_until,
                "blocked_until": self._blocked_until,
            },
        )

    def _load_devices(self) -> None:
        data = self._read_json(self._devices_path, {"devices": {}})
        devices = data.get("devices", {})
        if isinstance(devices, dict):
            self._devices = {
                str(device_id): record
                for device_id, record in devices.items()
                if isinstance(record, dict) and record.get("trusted") is True
            }
        trusted_ips = data.get("trusted_ips", {})
        if isinstance(trusted_ips, dict):
            self._trusted_ips = {
                str(ip): record
                for ip, record in trusted_ips.items()
                if isinstance(record, dict) and record.get("trusted") is True
            }

    def _persist_devices(self) -> None:
        self._write_json_atomic(
            self._devices_path,
            {
                "version": 1,
                "devices": self._devices,
                "trusted_ips": self._trusted_ips,
            },
        )

    def _sign_device(self, device_id: str) -> str:
        signature = hmac.new(
            self._secret,
            device_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{device_id}.{signature}"

    def _read_device_cookie(self, headers: Mapping[str, str]) -> str | None:
        raw_cookie = headers.get("cookie", "")
        for part in raw_cookie.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == COOKIE_NAME:
                device_id, dot, signature = value.rpartition(".")
                if not dot or not device_id:
                    return None
                expected = self._sign_device(device_id).rsplit(".", 1)[1]
                if hmac.compare_digest(signature, expected):
                    return device_id
        return None

    def observe(
        self,
        scope: dict,
        headers: Mapping[str, str],
    ) -> AccessObservation:
        now = self.clock()
        ip = str((scope.get("client") or ("", 0))[0] or "unknown")
        has_cloudflare_metadata = any(
            name in headers
            for name in ("cf-connecting-ip", "cf-ray", "cf-ipcountry")
        )
        source = classify_ip(ip, has_cloudflare_metadata)
        cookie_device_id = self._read_device_cookie(headers)
        device_id = cookie_device_id
        set_cookie = None
        if not device_id:
            device_id = secrets.token_urlsafe(24)
            secure = "; Secure" if scope.get("scheme") == "https" else ""
            set_cookie = (
                f"{COOKIE_NAME}={self._sign_device(device_id)}; "
                "Path=/; HttpOnly; SameSite=Lax; Max-Age=63072000"
                f"{secure}"
            )

        day = datetime.fromtimestamp(now).astimezone().date().isoformat()
        seen_identity = device_id if cookie_device_id else f"anonymous:{ip}"
        seen_key = (seen_identity, day)
        events: list[dict] = []
        alerts: list[dict] = []
        trusted = (
            source == "localhost"
            or device_id in self._devices
            or ip in self._trusted_ips
        )
        blocked = not trusted and self.is_ip_blocked(ip)
        if seen_key not in self._daily_seen:
            self._daily_seen.add(seen_key)
            events.append(
                {
                    "timestamp": datetime.fromtimestamp(now).astimezone().isoformat(),
                    "level": "info",
                    "event": "daily_first_seen",
                    "device": _device_fingerprint(device_id),
                    "ip": ip,
                    "source": source,
                    "protocol": scope.get("type", "http"),
                    "method": scope.get("method", ""),
                    "path": str(scope.get("path") or "/"),
                    "user_agent": str(headers.get("user-agent", ""))[:160],
                    "cf_country": str(headers.get("cf-ipcountry", ""))[:2],
                }
            )

        if device_id in self._devices:
            record = self._devices[device_id]
            previous_ip = str(record.get("last_ip") or "")
            if previous_ip and previous_ip != ip:
                events.append(
                    {
                        "timestamp": datetime.fromtimestamp(now).astimezone().isoformat(),
                        "level": "info",
                        "event": "trusted_device_ip_changed",
                        "device": _device_fingerprint(device_id),
                        "ip": ip,
                        "previous_ip": previous_ip,
                        "source": source,
                        "protocol": scope.get("type", "http"),
                        "method": scope.get("method", ""),
                        "path": str(scope.get("path") or "/"),
                        "user_agent": str(headers.get("user-agent", ""))[:160],
                        "cf_country": str(headers.get("cf-ipcountry", ""))[:2],
                    }
                )
                record["last_ip"] = ip
                record["last_seen_at"] = datetime.fromtimestamp(now).astimezone().isoformat()
                self._persist_devices()

        path = str(scope.get("path") or "/")
        if not trusted and not blocked:
            device_fingerprint = _device_fingerprint(device_id)
            suppression_keys = (f"ip:{ip}", f"device:{device_fingerprint}")
            acknowledged = any(
                self._acknowledged_until.get(key, 0) > now
                for key in suppression_keys
            )
            pending = any(
                alert.get("reason") == "unknown_device"
                and (
                    alert.get("ip") == ip
                    or alert.get("device") == device_fingerprint
                )
                for alert in self._pending_alerts.values()
            )
            warning_key = f"ip:{ip}"
            last_warning = self._warning_last.get(warning_key)
            initial_burst = last_warning is not None and now - last_warning < 5
            if not acknowledged and not pending and not initial_burst:
                self._warning_last[warning_key] = now
                warning = self._make_alert(
                    now=now,
                    level="warning",
                    reason="unknown_device",
                    device_id=device_id,
                    ip=ip,
                    source=source,
                    path=path,
                )
                alerts.append(warning)
                events.append(self._alert_event(warning, "unknown_device"))

            if self._should_count_for_rate(path):
                window = self._ip_windows.setdefault(ip, deque())
                cutoff = now - 60
                while window and window[0] < cutoff:
                    window.popleft()
                window.append(now)

                self._unknown_ip_last[ip] = now
                distinct_cutoff = now - 300
                self._unknown_ip_last = {
                    seen_ip: seen_at
                    for seen_ip, seen_at in self._unknown_ip_last.items()
                    if seen_at >= distinct_cutoff
                }

                if len(window) >= self.single_ip_limit:
                    self._append_serious_alert(
                        alerts,
                        events,
                        rule="single_ip_rate",
                        now=now,
                        device_id=device_id,
                        ip=ip,
                        source=source,
                        path=path,
                        request_count=len(window),
                    )
                distinct_count = len(self._unknown_ip_last)
                if distinct_count >= self.distinct_ip_limit:
                    self._append_serious_alert(
                        alerts,
                        events,
                        rule="distinct_ip_rate",
                        now=now,
                        device_id=device_id,
                        ip=ip,
                        source=source,
                        path=path,
                        distinct_ip_count=distinct_count,
                    )

        return AccessObservation(
            device_id=device_id,
            device_fingerprint=_device_fingerprint(device_id),
            trusted=trusted,
            blocked=blocked,
            effective_ip=ip,
            source=source,
            set_cookie=set_cookie,
            events=tuple(events),
            alerts=tuple(alerts),
        )

    @staticmethod
    def _should_count_for_rate(path: str) -> bool:
        return not (
            path.startswith("/api/security-access/")
            or path == "/public/strangealert.mp3"
        )

    def _make_alert(
        self,
        *,
        now: float,
        level: str,
        reason: str,
        device_id: str,
        ip: str,
        source: str,
        path: str,
        **counts: int,
    ) -> dict:
        alert_id = secrets.token_urlsafe(12)
        self._alert_device_ids[alert_id] = device_id
        self._alert_source_details[alert_id] = (ip, source)
        return {
            "alert_id": alert_id,
            "timestamp": datetime.fromtimestamp(now).astimezone().isoformat(),
            "level": level,
            "reason": reason,
            "device": _device_fingerprint(device_id),
            "ip": ip,
            "source": source,
            "path": path,
            **counts,
        }

    @staticmethod
    def _alert_event(alert: dict, event_type: str) -> dict:
        return {
            "timestamp": alert["timestamp"],
            "level": alert["level"],
            "event": event_type,
            "alert_id": alert["alert_id"],
            "device": alert["device"],
            "ip": alert["ip"],
            "source": alert["source"],
            "path": alert["path"],
            **{
                name: alert[name]
                for name in ("request_count", "distinct_ip_count")
                if name in alert
            },
        }

    def _append_serious_alert(
        self,
        alerts: list[dict],
        events: list[dict],
        *,
        rule: str,
        now: float,
        device_id: str,
        ip: str,
        source: str,
        path: str,
        **counts: int,
    ) -> None:
        last_alert = self._serious_last.get(rule)
        if last_alert is not None and now - last_alert < self.serious_cooldown_seconds:
            return
        self._serious_last[rule] = now
        alert = self._make_alert(
            now=now,
            level="serious",
            reason=rule,
            device_id=device_id,
            ip=ip,
            source=source,
            path=path,
            **counts,
        )
        alerts.append(alert)
        events.append(self._alert_event(alert, "serious_access"))

    def trust_device(self, device_id: str, ip: str, label: str = "") -> dict:
        now = self.clock()
        timestamp = datetime.fromtimestamp(now).astimezone().isoformat()
        self._devices[device_id] = {
            "trusted": True,
            "label": str(label).strip()[:40],
            "first_seen_at": timestamp,
            "last_seen_at": timestamp,
            "last_ip": ip,
        }
        try:
            self._persist_devices()
        except OSError as exc:
            log.warning("trusted device state is memory-only: %s", type(exc).__name__)
        event = {
            "timestamp": timestamp,
            "level": "info",
            "event": "device_trusted",
            "device": _device_fingerprint(device_id),
            "ip": ip,
        }
        try:
            self._append_event(event)
        except OSError as exc:
            log.warning("device trust audit write failed: %s", type(exc).__name__)
        return event

    def trust_alert_source(self, alert_id: str, label: str = "") -> dict | None:
        alert_id = str(alert_id)
        device_id = self._alert_device_ids.get(alert_id)
        if not device_id:
            return None
        alert = self._pending_alerts.get(alert_id, {})
        source_ip, source_kind = self._alert_source_details.get(alert_id, ("", ""))
        source_ip = str(alert.get("ip") or source_ip)
        source_kind = str(alert.get("source") or source_kind)
        if source_ip and source_kind in {"lan", "tailscale"}:
            self._trusted_ips[source_ip] = {
                "trusted": True,
                "label": str(label).strip()[:40],
                "trusted_at": datetime.fromtimestamp(self.clock()).astimezone().isoformat(),
            }
        fingerprint = _device_fingerprint(device_id)
        event = self.trust_device(device_id, source_ip, label)
        matching_ids = [
            pending_id
            for pending_id, pending in self._pending_alerts.items()
            if pending.get("device") == fingerprint
            or (alert.get("ip") and pending.get("ip") == alert.get("ip"))
        ]
        for pending_id in matching_ids:
            self._pending_alerts.pop(pending_id, None)
            self._alert_device_ids.pop(pending_id, None)
            self._alert_source_details.pop(pending_id, None)
        self._alert_device_ids.pop(alert_id, None)
        self._alert_source_details.pop(alert_id, None)
        if matching_ids:
            try:
                self._persist_state()
            except OSError as exc:
                log.warning("security alert trust state is memory-only: %s", type(exc).__name__)
        return event

    def trusted_devices(self) -> list[dict]:
        return [
            {
                "device": _device_fingerprint(device_id),
                "label": str(record.get("label") or ""),
                "last_ip": str(record.get("last_ip") or ""),
            }
            for device_id, record in self._devices.items()
        ]

    def pending_alerts(self) -> list[dict]:
        return [dict(alert) for alert in self._pending_alerts.values()]

    def pending_alerts_for(self, observation: AccessObservation) -> list[dict]:
        if observation.trusted:
            visible = self.pending_alerts()
        elif observation.set_cookie is None:
            visible = [
                dict(alert)
                for alert in self._pending_alerts.values()
                if alert.get("device") == observation.device_fingerprint
            ]
        else:
            visible = [
                dict(alert)
                for alert in self._pending_alerts.values()
                if alert.get("ip") == observation.effective_ip
            ]
        known_ids = {str(alert.get("alert_id")) for alert in visible}
        for alert in observation.alerts:
            if str(alert.get("alert_id")) not in known_ids:
                visible.append(dict(alert))
        return visible

    def acknowledge_alert(self, alert_id: str) -> bool:
        alert = self._pending_alerts.pop(str(alert_id), None)
        self._alert_device_ids.pop(str(alert_id), None)
        self._alert_source_details.pop(str(alert_id), None)
        removed = alert is not None
        if alert is not None:
            self._start_acknowledgement_cooldown(alert)
            try:
                self._persist_state()
            except OSError as exc:
                log.warning("security acknowledgement is memory-only: %s", type(exc).__name__)
        return removed

    @staticmethod
    def can_block_ip(ip: str) -> bool:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10"):
            return False
        return address.is_global

    def is_ip_blocked(self, ip: str) -> bool:
        expires_at = self._blocked_until.get(str(ip), 0)
        if expires_at <= self.clock():
            self._blocked_until.pop(str(ip), None)
            return False
        return True

    def block_alert_ip(self, alert_id: str, duration_seconds: int = 86400) -> dict | None:
        alert = self._pending_alerts.get(str(alert_id))
        if alert is None:
            return None
        ip = str(alert.get("ip") or "")
        if not self.can_block_ip(ip):
            raise ValueError("only public IP addresses can be blocked")
        blocked_until = self.clock() + max(60, int(duration_seconds))
        self._blocked_until[ip] = blocked_until
        matching_ids = [
            pending_id
            for pending_id, pending in self._pending_alerts.items()
            if pending.get("ip") == ip
        ]
        for pending_id in matching_ids:
            self._pending_alerts.pop(pending_id, None)
            self._alert_device_ids.pop(pending_id, None)
            self._alert_source_details.pop(pending_id, None)
        try:
            self._persist_state()
        except OSError as exc:
            log.warning("security IP block is memory-only: %s", type(exc).__name__)
        event = {
            "timestamp": datetime.fromtimestamp(self.clock()).astimezone().isoformat(),
            "level": "warning",
            "event": "ip_blocked_24h",
            "alert_id": str(alert_id),
            "ip": ip,
            "source": str(alert.get("source") or ""),
            "blocked_until": datetime.fromtimestamp(blocked_until).astimezone().isoformat(),
        }
        try:
            self._append_event(event)
        except OSError as exc:
            log.warning("security IP block audit write failed: %s", type(exc).__name__)
        return {
            "blocked": True,
            "ip": ip,
            "blocked_until": event["blocked_until"],
        }

    def _start_acknowledgement_cooldown(self, alert: Mapping) -> None:
        if alert.get("reason") != "unknown_device":
            return
        expires_at = self.clock() + self.warning_ttl_seconds
        ip = str(alert.get("ip") or "")
        device = str(alert.get("device") or "")
        if ip:
            self._acknowledged_until[f"ip:{ip}"] = expires_at
        if device:
            self._acknowledged_until[f"device:{device}"] = expires_at

    def acknowledge_alerts_for(self, observation: AccessObservation) -> int:
        matching_ids = [
            alert_id
            for alert_id, alert in self._pending_alerts.items()
            if (
                alert.get("device") == observation.device_fingerprint
                or alert.get("ip") == observation.effective_ip
            )
        ]
        for alert_id in matching_ids:
            alert = self._pending_alerts.pop(alert_id, None)
            self._alert_device_ids.pop(alert_id, None)
            self._alert_source_details.pop(alert_id, None)
            if alert is not None:
                self._start_acknowledgement_cooldown(alert)
        if matching_ids:
            try:
                self._persist_state()
            except OSError as exc:
                log.warning("security acknowledgements are memory-only: %s", type(exc).__name__)
        return len(matching_ids)

    def start(
        self,
        broadcast: Callable[[dict], Awaitable[None]],
    ) -> None:
        if self._worker_task and not self._worker_task.done():
            return
        try:
            self._broadcast = broadcast
            self._queue = asyncio.Queue(maxsize=self.queue_size)
            self._worker_task = asyncio.create_task(self._worker())
        except Exception as exc:
            self._queue = None
            self._worker_task = None
            log.warning("security audit worker did not start: %s", type(exc).__name__)

    def submit(self, observation: AccessObservation) -> None:
        queue = self._queue
        if queue is None or (not observation.events and not observation.alerts):
            return
        try:
            queue.put_nowait(observation)
        except asyncio.QueueFull:
            if any(alert.get("level") == "serious" for alert in observation.alerts):
                try:
                    queue.get_nowait()
                    queue.task_done()
                    queue.put_nowait(observation)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    log.warning("security audit queue full; serious event could not be queued")
            else:
                log.warning("security audit queue full; duplicate low-level event dropped")

    async def stop(self) -> None:
        task = self._worker_task
        queue = self._queue
        if task is None or queue is None:
            return
        try:
            await queue.put(None)
            await task
        except Exception as exc:
            log.warning("security audit worker stop failed: %s", type(exc).__name__)
        finally:
            self._worker_task = None
            self._queue = None

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            observation = await self._queue.get()
            try:
                if observation is None:
                    return
                for event in observation.events:
                    try:
                        self._append_event(event)
                    except Exception as exc:
                        log.warning("security audit write failed: %s", type(exc).__name__)
                enriched_alerts = []
                for alert in observation.alerts:
                    enriched = dict(alert)
                    try:
                        enriched.update(
                            await self.geo_resolver.resolve(
                                str(alert.get("ip") or ""),
                                str(alert.get("source") or ""),
                            )
                        )
                    except Exception as exc:
                        log.warning("security alert geolocation failed open: %s", type(exc).__name__)
                        enriched.update(
                            location="暂时无法确定",
                            location_kind="unknown",
                            location_notice="",
                        )
                    enriched_alerts.append(enriched)
                    self._pending_alerts[enriched["alert_id"]] = enriched
                if enriched_alerts:
                    try:
                        self._persist_state()
                    except Exception as exc:
                        log.warning("security alert state write failed: %s", type(exc).__name__)
                for alert in enriched_alerts:
                    if self._broadcast is None:
                        continue
                    try:
                        await self._broadcast({"type": "security_alert", "data": alert})
                    except Exception as exc:
                        log.warning("security alert broadcast failed: %s", type(exc).__name__)
            finally:
                self._queue.task_done()

    def _append_event(self, event: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        day = str(event.get("timestamp") or "")[:10]
        if len(day) != 10:
            day = datetime.now().astimezone().date().isoformat()
        with (self.data_dir / f"{day}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


class SecurityAccessMiddleware:
    """Pure ASGI wrapper; audit failures never alter the wrapped app."""

    def __init__(self, app, service: SecurityAccessService):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        observation = None
        try:
            headers = {
                name.decode("latin-1").lower(): value.decode("latin-1")
                for name, value in scope.get("headers", [])
            }
            observation = self.service.observe(scope, headers)
            scope.setdefault("state", {})["security_access"] = observation
            self.service.submit(observation)
        except Exception as exc:
            log.warning("security access observation failed open: %s", type(exc).__name__)

        if observation and observation.blocked:
            if scope.get("type") == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "IP temporarily blocked"})
            else:
                body = b'{"detail":"IP temporarily blocked"}'
                await send({
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
            return

        if scope.get("type") != "http" or not observation or not observation.set_cookie:
            await self.app(scope, receive, send)
            return

        async def send_with_cookie(message):
            if message.get("type") == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"set-cookie", observation.set_cookie.encode("latin-1"))
                ]
            await send(message)

        await self.app(scope, receive, send_with_cookie)
