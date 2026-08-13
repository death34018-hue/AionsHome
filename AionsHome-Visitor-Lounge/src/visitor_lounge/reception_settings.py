"""Live, Lounge-owned reception configuration shared by both local apps."""

from dataclasses import dataclass, replace
from datetime import datetime
from string import Formatter

from .database import Database, utc_now


FIRST_WELCOME = (
    "欢迎，{访客名字}。这里是会客室，我是{接待人名字}，由我来接待你。"
    "有什么想和我聊聊的吗？"
)
RETURNING_WELCOME = "再次见到你很开心，{访客名字}。今天想和我聊些什么？"


class InvalidReceptionSettings(ValueError):
    """Raised when an administrator submits an unusable reception setting."""


@dataclass(frozen=True)
class ReceptionSettings:
    persona_text: str
    first_welcome: str = FIRST_WELCOME
    returning_welcome: str = RETURNING_WELCOME
    quota_exhausted: str = "本轮会面额度已经用完了，我们先聊到这里吧。额度恢复后，欢迎你再来。"
    unsafe_request: str = (
        "这项请求不适合在会客室继续，我会先结束本次会面。"
        "如果你认为这是误判，可以联系邀请你来的人。"
    )
    credential_detected: str = (
        "为了保护你的信息安全，请不要在会客室发送密码、密钥或访问令牌。"
        "你可以删去敏感内容后再继续聊。"
    )
    input_too_long: str = "这条消息太长了，请缩短到 500 字以内再发送。"
    lounge_closed: str = "会客室现在暂时休息，请稍后再来。"
    system_unavailable: str = "我现在暂时无法回复，请稍后再试。你的本次额度不会被扣除。"
    hourly_quota_limit: int = 15
    lounge_enabled: bool = True
    idle_minutes: int = 30
    updated_at: datetime | None = None


_TEXT_FIELDS = (
    "persona_text",
    "first_welcome",
    "returning_welcome",
    "quota_exhausted",
    "unsafe_request",
    "credential_detected",
    "input_too_long",
    "lounge_closed",
    "system_unavailable",
)
_TEMPLATE_FIELDS = _TEXT_FIELDS[1:]


class ReceptionSettingsRepository:
    def __init__(self, database: Database, root) -> None:
        self.database = database
        self.root = root

    def defaults(self) -> ReceptionSettings:
        persona = (self.root / "config" / "persona.md").read_text("utf-8").strip()
        return self._validate(ReceptionSettings(persona_text=persona))

    def get(self) -> ReceptionSettings:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT persona_text, first_welcome, returning_welcome, "
                "quota_exhausted, unsafe_request, credential_detected, "
                "input_too_long, lounge_closed, "
                "system_unavailable, hourly_quota_limit, lounge_enabled, "
                "idle_minutes, updated_at "
                "FROM reception_settings WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            return self.defaults()
        return ReceptionSettings(
            persona_text=str(row[0]),
            first_welcome=str(row[1]),
            returning_welcome=str(row[2]),
            quota_exhausted=str(row[3]),
            unsafe_request=str(row[4]),
            credential_detected=str(row[5] or ReceptionSettings.credential_detected),
            input_too_long=str(row[6]),
            lounge_closed=str(row[7]),
            system_unavailable=str(row[8]),
            hourly_quota_limit=int(row[9]),
            lounge_enabled=bool(row[10]),
            idle_minutes=int(row[11]),
            updated_at=datetime.fromisoformat(str(row[12])),
        )

    def save(self, candidate: ReceptionSettings) -> ReceptionSettings:
        saved = replace(self._validate(candidate), updated_at=utc_now())
        values = [getattr(saved, name) for name in _TEXT_FIELDS]
        with self.database.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO reception_settings
                    (singleton_id, persona_text, first_welcome, returning_welcome,
                     quota_exhausted, unsafe_request, credential_detected,
                     input_too_long, lounge_closed, system_unavailable, lounge_enabled,
                     hourly_quota_limit, idle_minutes, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    persona_text=excluded.persona_text,
                    first_welcome=excluded.first_welcome,
                    returning_welcome=excluded.returning_welcome,
                    quota_exhausted=excluded.quota_exhausted,
                    unsafe_request=excluded.unsafe_request,
                    credential_detected=excluded.credential_detected,
                    input_too_long=excluded.input_too_long,
                    lounge_closed=excluded.lounge_closed,
                    system_unavailable=excluded.system_unavailable,
                    hourly_quota_limit=excluded.hourly_quota_limit,
                    lounge_enabled=excluded.lounge_enabled,
                    idle_minutes=excluded.idle_minutes,
                    updated_at=excluded.updated_at
                """,
                (
                    *values,
                    int(saved.lounge_enabled),
                    saved.hourly_quota_limit,
                    saved.idle_minutes,
                    saved.updated_at.isoformat(),
                ),
            )
        return saved

    def restore_defaults(self) -> ReceptionSettings:
        return self.save(self.defaults())

    @staticmethod
    def _validate(candidate: ReceptionSettings) -> ReceptionSettings:
        cleaned = {}
        for name in _TEXT_FIELDS:
            value = getattr(candidate, name).strip()
            limit = 12_000 if name == "persona_text" else 1_000
            if not value or len(value) > limit:
                raise InvalidReceptionSettings(f"invalid {name}")
            cleaned[name] = value
        for name in _TEMPLATE_FIELDS:
            try:
                placeholders = {
                    field_name
                    for _, field_name, _, _ in Formatter().parse(cleaned[name])
                    if field_name is not None
                }
            except ValueError as exc:
                raise InvalidReceptionSettings(f"invalid {name}") from exc
            allowed_placeholders = {"访客名字"}
            if name == "first_welcome":
                allowed_placeholders.add("接待人名字")
            if placeholders - allowed_placeholders:
                raise InvalidReceptionSettings(f"invalid placeholder in {name}")
        if candidate.idle_minutes != 30:
            raise InvalidReceptionSettings("idle_minutes must be 30")
        if not 1 <= candidate.hourly_quota_limit <= 500:
            raise InvalidReceptionSettings(
                "quota window limit must be between 1 and 500"
            )
        return replace(candidate, **cleaned)
