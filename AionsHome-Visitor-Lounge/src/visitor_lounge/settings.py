"""Configuration boundary for the standalone Visitor Lounge."""

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet


MAX_GENERATIONS = 2
MAX_WAITING = 3


@dataclass(frozen=True)
class Settings:
    root: Path
    database_path: Path
    visitor_host: str
    visitor_port: int
    admin_host: str
    admin_port: int
    max_generations: int
    max_generations_hard_limit: int
    max_waiting: int
    queue_timeout_seconds: int
    generation_timeout_seconds: int
    key_pepper: bytes
    master_key: bytes
    session_secret: bytes
    codex_workdir: Path
    host_display_name: str = "接待人"
    reserved_visitor_names: tuple[str, ...] = ()
    standby_display_name: str = "未来接待者"
    recording_disclosure: str = "对话将被记录，以便保持交流连续。"
    recording_disclosure_version: str = "1"
    input_token_price_per_million: float | None = None
    output_token_price_per_million: float | None = None
    timezone_name: str = "Asia/Shanghai"

    def validate(self) -> "Settings":
        try:
            Fernet(self.master_key)
        except (TypeError, ValueError):
            raise ValueError("invalid visitor lounge master key") from None
        try:
            ZoneInfo(self.timezone_name)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("invalid admin timezone") from None
        if self.max_generations_hard_limit != MAX_GENERATIONS or not (
            1 <= self.max_generations <= MAX_GENERATIONS
        ):
            raise ValueError("max_generations must be between 1 and 2")
        if not 0 <= self.max_waiting <= MAX_WAITING:
            raise ValueError("max_waiting must be between 0 and 3")
        if not self.standby_display_name.strip() or len(self.standby_display_name) > 80:
            raise ValueError("standby_display_name must contain 1 to 80 characters")
        return self

    @classmethod
    def load(cls, root: Path) -> "Settings":
        raw = tomllib.loads((root / "config/visitor-lounge.toml").read_text("utf-8"))
        concurrency = int(raw["queue"]["max_generations"])
        identity = raw.get("identity", {})
        visitor_ui = raw.get("visitor_ui", {})
        disclosure = raw.get("recording_disclosure", {})
        pricing = raw.get("pricing", {})
        settings = cls(
            root=root,
            database_path=root / "data/visitor-lounge.sqlite3",
            visitor_host="127.0.0.1",
            visitor_port=8001,
            admin_host="127.0.0.1",
            admin_port=8002,
            max_generations=concurrency,
            max_generations_hard_limit=MAX_GENERATIONS,
            max_waiting=min(int(raw["queue"]["max_waiting"]), MAX_WAITING),
            queue_timeout_seconds=120,
            generation_timeout_seconds=120,
            key_pepper=os.environ["VISITOR_LOUNGE_KEY_PEPPER"].encode(),
            master_key=os.environ["VISITOR_LOUNGE_MASTER_KEY"].encode(),
            session_secret=os.environ["VISITOR_LOUNGE_SESSION_SECRET"].encode(),
            codex_workdir=root / ".runtime/codex-workdir",
            host_display_name=str(identity.get("host_display_name") or "接待人"),
            reserved_visitor_names=tuple(
                str(name) for name in identity.get("reserved_visitor_names", ())
            ),
            standby_display_name=str(
                visitor_ui.get("standby_display_name") or "未来接待者"
            ),
            recording_disclosure=str(
                disclosure.get("text") or "对话将被记录，以便保持交流连续。"
            ),
            recording_disclosure_version=str(disclosure.get("version") or "1"),
            input_token_price_per_million=(
                float(pricing["input_per_million"])
                if "input_per_million" in pricing
                else None
            ),
            output_token_price_per_million=(
                float(pricing["output_per_million"])
                if "output_per_million" in pricing
                else None
            ),
            timezone_name=str(raw.get("admin", {}).get("timezone") or "Asia/Shanghai"),
        )
        return settings.validate()
