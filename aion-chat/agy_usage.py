"""Read AGY quotas and cache each CLI's latest successful report for Home."""

import asyncio
import json
import logging
from datetime import datetime, timezone

from antigravity_cli import CHAT_WORKSPACE, _process_options, find_binary
from generation_control import terminate_process_tree


class UsageError(Exception):
    pass


def parse_report(report):
    try:
        command = report["command"]
        if report["status"] != "SUCCESS" or command["name"] != "usage":
            raise ValueError
        groups = []
        for group in command["data"]["groups"]:
            buckets = []
            for bucket in group["buckets"]:
                fraction = bucket["remaining_fraction"]
                if type(fraction) not in (int, float) or not 0 <= fraction <= 1:
                    raise ValueError
                buckets.append({
                    "window": bucket.get("window") or bucket["name"],
                    "remaining": fraction * 100,
                    "reset_at": bucket.get("reset_time") or None,
                })
            if buckets:
                groups.append({"name": group["name"], "buckets": buckets})
        if groups:
            return groups
    except (KeyError, ValueError, TypeError, AttributeError):
        pass
    raise UsageError("AGY 未返回可识别的额度数据")


async def query_usage():
    binary = find_binary()
    if not binary:
        raise UsageError("小家服务器未找到 AGY")
    CHAT_WORKSPACE.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        binary, "-p", "/usage", "--output-format", "json", "--print-timeout", "30s",
        "--settings", '{"disableSlashCommands":false}',
        cwd=CHAT_WORKSPACE, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **_process_options(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
    except TimeoutError:
        raise UsageError("AGY 查询超时，请检查网络和登录状态") from None
    finally:
        if process.returncode is None:
            await terminate_process_tree(process)
    if process.returncode:
        if b"authentication required" in (stderr + stdout).lower():
            raise UsageError("请在小家服务器上登录 AGY")
        raise UsageError("AGY 查询失败，请检查网络和登录状态")
    try:
        return parse_report(json.loads(stdout))
    except (ValueError, UnicodeError):
        raise UsageError("AGY 未返回可识别的额度数据") from None


class UsageMonitor:
    def __init__(self, query, source):
        self.query = query
        self.source = source
        self.snapshot = {"groups": [], "updated_at": None, "error": None}
        self._in_flight = None

    async def refresh(self):
        if self._in_flight is None or self._in_flight.done():
            self._in_flight = asyncio.create_task(self._read_quota())
        await asyncio.shield(self._in_flight)

    async def _read_quota(self):
        try:
            groups = await self.query()
        except UsageError as exc:
            self.snapshot = {**self.snapshot, "error": str(exc)}
        except Exception:
            logging.getLogger(__name__).exception("%s quota query failed", self.source)
            self.snapshot = {**self.snapshot, "error": f"小家暂时无法读取 {self.source} 额度"}
        else:
            self.snapshot = {
                "groups": groups, "updated_at": datetime.now(timezone.utc).isoformat(), "error": None,
            }

monitor = UsageMonitor(query_usage, "AGY")
