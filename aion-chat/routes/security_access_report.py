"""Local-only, read-only summary for security access audit logs."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse


def _is_local_report_request(request: Request) -> bool:
    client_ip = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(client_ip).is_loopback:
            return False
    except ValueError:
        return False
    host = str(request.headers.get("host") or "").lower()
    if host.startswith("["):
        hostname = host.split("]", 1)[0] + "]"
    else:
        hostname = host.split(":", 1)[0]
    return hostname in {"127.0.0.1", "localhost", "[::1]"}


def _read_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, OSError, ValueError):
        return default


def _fingerprint(device_id: str) -> str:
    return hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:12]


def _local_location(ip: str, source: str) -> str:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "暂时无法确定"
    if address.is_loopback:
        return "服务器本机"
    if source == "tailscale" or (
        address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10")
    ):
        return "Tailscale 私有网络"
    if address.is_private:
        return "局域网设备（本地网络）"
    return "暂时无法确定"


def build_security_access_report(data_dir: Path) -> dict:
    data_dir = Path(data_dir)
    devices = _read_json(data_dir / "devices.json", {"devices": {}}).get("devices", {})
    trusted_devices = set()
    trusted_ips = set()
    if isinstance(devices, dict):
        for device_id, record in devices.items():
            if not isinstance(record, dict) or record.get("trusted") is not True:
                continue
            trusted_devices.add(_fingerprint(str(device_id)))
            if record.get("last_ip"):
                trusted_ips.add(str(record["last_ip"]))

    state = _read_json(data_dir / "state.json", {})
    blocked_ips = set()
    blocked = state.get("blocked_until", {})
    if isinstance(blocked, dict):
        blocked_ips = {str(ip) for ip in blocked}

    geo_records = _read_json(data_dir / "geo_cache.json", {"records": {}}).get("records", {})
    if not isinstance(geo_records, dict):
        geo_records = {}

    by_ip: dict[str, dict] = {}
    for log_path in sorted(data_dir.glob("*.jsonl"))[-30:]:
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            ip = str(event.get("ip") or "").strip()
            if not ip:
                continue
            timestamp = str(event.get("timestamp") or "")
            item = by_ip.setdefault(
                ip,
                {
                    "ip": ip,
                    "first_seen": timestamp,
                    "last_seen": timestamp,
                    "sources": set(),
                    "devices": set(),
                    "unknown": False,
                    "serious": False,
                    "events": 0,
                },
            )
            if timestamp and (not item["first_seen"] or timestamp < item["first_seen"]):
                item["first_seen"] = timestamp
            if timestamp > item["last_seen"]:
                item["last_seen"] = timestamp
            if event.get("source"):
                item["sources"].add(str(event["source"]))
            if event.get("device"):
                item["devices"].add(str(event["device"]))
            item["events"] += 1
            item["unknown"] = item["unknown"] or event.get("event") == "unknown_device"
            item["serious"] = item["serious"] or (
                event.get("event") == "serious_access" or event.get("level") == "serious"
            )

    source_names = {
        "localhost": "服务器本机",
        "lan": "局域网",
        "tailscale": "Tailscale",
        "cloudflare": "Cloudflare",
        "public": "公网",
    }
    rows = []
    for ip, item in by_ip.items():
        source_values = sorted(item["sources"])
        source = source_values[0] if len(source_values) == 1 else ", ".join(source_values)
        is_trusted = ip in trusted_ips or bool(item["devices"] & trusted_devices)
        if item["serious"]:
            status, status_kind = "严重异常", "serious"
        elif ip in blocked_ips:
            status, status_kind = "已临时封锁", "blocked"
        elif item["unknown"] and not is_trusted:
            status, status_kind = "尚未确认", "unknown"
        else:
            status, status_kind = "正常", "normal"
        geo = geo_records.get(ip, {})
        location = str(geo.get("location") or "") if isinstance(geo, dict) else ""
        rows.append(
            {
                "ip": ip,
                "location": location or _local_location(ip, source),
                "source": "、".join(source_names.get(value, value) for value in source_values) or "未知",
                "first_seen": item["first_seen"],
                "last_seen": item["last_seen"],
                "events": item["events"],
                "status": status,
                "status_kind": status_kind,
            }
        )
    rows.sort(key=lambda item: item["last_seen"], reverse=True)

    serious_rows = [item for item in rows if item["status_kind"] == "serious"]
    unknown_rows = [item for item in rows if item["status_kind"] == "unknown"]
    if serious_rows:
        summary = f"发现异常：{len(serious_rows)} 个 IP 触发严重频率告警，请优先检查。"
        summary_kind = "serious"
    elif unknown_rows:
        shown = "、".join(item["ip"] for item in unknown_rows[:3])
        suffix = " 等" if len(unknown_rows) > 3 else ""
        summary = f"发现异常：{len(unknown_rows)} 个 IP 尚未确认（{shown}{suffix}）。"
        summary_kind = "unknown"
    else:
        summary = "一切正常，未发现异常访问。"
        summary_kind = "normal"
    return {"summary": summary, "summary_kind": summary_kind, "ips": rows}


def create_security_access_report_router(data_dir: Path, page_path: Path) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    def require_local(request: Request) -> None:
        if not _is_local_report_request(request):
            raise HTTPException(status_code=404, detail="Not Found")

    @router.get("/security-access-report")
    async def report_page(request: Request):
        require_local(request)
        return FileResponse(page_path, headers={"Cache-Control": "no-store"})

    @router.get("/api/security-access-report")
    async def report_data(request: Request):
        require_local(request)
        return JSONResponse(build_security_access_report(data_dir), headers={"Cache-Control": "no-store"})

    return router
