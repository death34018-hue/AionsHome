"""Small, deterministic device-context store shared by activity and prompts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from math import isfinite
from typing import Any


FRESH_SECONDS = 30 * 60
KEEP_SECONDS = 8 * 60 * 60
PROMPT_MAX_CHARS = 800

PHONE_SLOT_KEYS = (
    "posture",
    "motion",
    "light",
    "proximity",
    "screen",
    "foreground_app",
)

NOISE_CATEGORIES = {"service", "transport", "progress", "promo", "recommendation"}
NOISE_WORDS = ("签到", "领券", "活动推荐", "热门推荐", "立即领取", "限时优惠")
NOTIFICATION_PRIORITY = {
    "call": 100,
    "alarm": 95,
    "reminder": 90,
    "event": 85,
    "message": 80,
    "delivery": 80,
    "finance": 75,
    "system": 60,
}

POSTURE_TEXT = {
    "portrait": "竖屏",
    "portrait_upside_down": "倒竖屏",
    "landscape_left": "左横屏",
    "landscape_right": "右横屏",
    "landscape": "横屏",
    "face_up": "平放正面朝上",
    "face_down": "扣放",
    "tilted": "倾斜",
}
MOTION_TEXT = {
    "still": "静止",
    "slight": "轻微晃动",
    "moving": "移动中",
    "strong": "明显晃动",
}
LIGHT_TEXT = {
    "dark": "环境黑暗",
    "dim": "环境较暗",
    "normal": "环境光线正常",
    "bright": "环境明亮",
}
SCREEN_TEXT = {"on": "亮屏", "off": "熄屏"}
FOREGROUND_APP_TEXT = {
    "com.bbk.launcher2": "vivo 桌面",
    "com.vivo.launcher": "vivo 桌面",
    "com.android.launcher": "安卓桌面",
    "com.android.launcher3": "安卓桌面",
    "com.huawei.android.launcher": "华为桌面",
    "com.miui.home": "小米桌面",
    "com.oppo.launcher": "OPPO 桌面",
    "com.sec.android.app.launcher": "三星桌面",
    "com.tencent.mm": "微信",
    "com.tencent.mobileqq": "QQ",
    "com.xingin.xhs": "小红书",
    "com.ss.android.ugc.aweme": "抖音",
    "tv.danmaku.bili": "哔哩哔哩",
    "com.openai.chatgpt": "ChatGPT",
}


def _number(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _clean_text(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _duration_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "不到1分钟"
    minutes = seconds // 60
    if minutes < 60:
        return f"约{minutes}分钟"
    hours, rest = divmod(minutes, 60)
    return f"约{hours}小时{rest}分钟" if rest else f"约{hours}小时"


def _age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "刚刚"
    return f"{seconds // 60}分钟前"


class DeviceContextStore:
    def __init__(
        self,
        fresh_seconds: int = FRESH_SECONDS,
        keep_seconds: int = KEEP_SECONDS,
    ):
        self.fresh_seconds = fresh_seconds
        self.keep_seconds = keep_seconds
        self.slots: dict[str, dict] = {}
        self.notifications: dict[str, dict] = {}
        self.events: list[dict] = []

    def _remember(self, event: dict, now: float) -> dict:
        item = dict(event)
        item["timestamp"] = _number(item.get("timestamp"), now)
        self.events.append(item)
        cutoff = now - self.keep_seconds
        self.events = [row for row in self.events if row.get("timestamp", 0) >= cutoff]
        return item

    def update_phone(self, payload: dict, received_at: float) -> list[dict]:
        changes = []
        if not isinstance(payload, dict):
            return changes
        for key in PHONE_SLOT_KEYS:
            incoming = payload.get(key)
            if not isinstance(incoming, dict):
                continue
            value = incoming.get("value")
            if value is None or value == "":
                continue
            observed_at = _number(incoming.get("observed_at"), received_at)
            old = self.slots.get(key)
            changed = old is None or old.get("value") != value
            slot = dict(incoming)
            slot["value"] = value
            slot["observed_at"] = observed_at
            slot["received_at"] = received_at
            slot["since"] = (
                _number(incoming.get("since"), received_at)
                if changed
                else _number(old.get("since"), received_at)
            )
            slot["confidence"] = _number(incoming.get("confidence"), 1.0)
            self.slots[key] = slot
            if changed:
                changes.append(
                    self._remember(
                        {
                            "kind": "phone_context",
                            "slot": key,
                            "data": deepcopy(slot),
                            "timestamp": received_at,
                        },
                        received_at,
                    )
                )
        return changes

    @staticmethod
    def _notification_noise(item: dict) -> tuple[bool, str]:
        title = _clean_text(item.get("title"))
        text = _clean_text(item.get("text"))
        category = str(item.get("category") or "").lower()
        if item.get("noise"):
            return True, "手机端标记为低价值"
        if item.get("group_summary"):
            return True, "通知组摘要"
        if not title and not text:
            return True, "没有标题或正文"
        if category in NOISE_CATEGORIES:
            return True, f"{category} 类常驻或推荐通知"
        combined = title + text
        if any(word in combined for word in NOISE_WORDS):
            return True, "推广或活动通知"
        return False, ""

    def upsert_notification(self, payload: dict, received_at: float) -> dict:
        key = _clean_text(payload.get("key"), 180)
        if not key:
            return {}
        old = self.notifications.get(key, {})
        item = dict(old)
        item.update(payload)
        item["key"] = key
        item["title"] = _clean_text(item.get("title"))
        item["text"] = _clean_text(item.get("text"))
        item["received_at"] = received_at
        item["last_updated_at"] = received_at
        item["posted_at"] = _number(item.get("posted_at"), received_at)
        item["removed"] = False
        item["noise"], item["filter_reason"] = self._notification_noise(item)
        self.notifications[key] = item
        return self._remember(
            {
                "kind": "notification",
                "notification": deepcopy(item),
                "timestamp": received_at,
            },
            received_at,
        )

    def remove_notification(self, key: str, received_at: float) -> dict | None:
        key = _clean_text(key, 180)
        old = self.notifications.pop(key, None)
        if old is None:
            return None
        return self._remember(
            {
                "kind": "notification_removed",
                "key": key,
                "timestamp": received_at,
            },
            received_at,
        )

    def hydrate(self, events: list[dict], now: float) -> None:
        self.slots.clear()
        self.notifications.clear()
        self.events.clear()
        cutoff = now - self.keep_seconds
        for event in sorted(events or [], key=lambda row: row.get("timestamp", 0)):
            timestamp = _number(event.get("timestamp"), 0)
            if timestamp < cutoff:
                continue
            kind = event.get("kind")
            if kind == "phone_context" and event.get("slot") in PHONE_SLOT_KEYS:
                self.slots[event["slot"]] = deepcopy(event.get("data") or {})
            elif kind == "notification":
                item = deepcopy(event.get("notification") or {})
                if item.get("key"):
                    self.notifications[item["key"]] = item
            elif kind == "notification_removed":
                self.notifications.pop(str(event.get("key") or ""), None)
            self.events.append(deepcopy(event))

    def _fresh_phone(self, now: float) -> dict[str, dict]:
        result = {}
        for key, slot in self.slots.items():
            if now - _number(slot.get("received_at"), 0) <= self.fresh_seconds:
                result[key] = deepcopy(slot)
        return result

    def _fresh_notifications(self, now: float) -> tuple[list[dict], list[dict]]:
        active, filtered = [], []
        for item in self.notifications.values():
            if now - _number(item.get("received_at"), 0) > self.fresh_seconds:
                continue
            target = filtered if item.get("noise") else active
            target.append(deepcopy(item))
        active.sort(
            key=lambda item: (
                NOTIFICATION_PRIORITY.get(str(item.get("category") or "").lower(), 50),
                _number(item.get("received_at"), 0),
            ),
            reverse=True,
        )
        filtered.sort(key=lambda item: _number(item.get("received_at"), 0), reverse=True)
        return active, filtered

    def snapshot(self, pc: dict, now: float) -> dict:
        phone = self._fresh_phone(now)
        pc = dict(pc or {})
        display = str(pc.get("display") or "unknown")
        idle_seconds = _number(pc.get("idle_seconds"), float("inf"))
        if display != "on":
            pc["input_state"] = "unavailable" if display == "off" else "unknown"
        elif idle_seconds <= 60:
            pc["input_state"] = "active"
        elif idle_seconds <= 300:
            pc["input_state"] = "recent"
        else:
            pc["input_state"] = "idle"

        primary = None
        evidence = []
        if pc.get("input_state") == "active":
            evidence.append(f"电脑最近一次键鼠输入在{int(idle_seconds)}秒前")
            if pc.get("app"):
                evidence.append(f"电脑前台为{_clean_text(pc['app'], 60)}")
            primary = {
                "state": "computer_active",
                "label": "正在电脑前操作",
                "confidence": 0.95,
                "since": now - idle_seconds,
                "evidence": evidence,
            }
        else:
            screen = (phone.get("screen") or {}).get("value")
            posture = (phone.get("posture") or {}).get("value")
            motion = (phone.get("motion") or {}).get("value")
            light = (phone.get("light") or {}).get("value")
            app = (phone.get("foreground_app") or {}).get("value")
            phone_active = screen == "on" and motion in {"slight", "moving", "strong"}
            possible_bed = (
                phone_active
                and posture in {"landscape_left", "landscape_right", "landscape"}
                and light in {"dark", "dim"}
                and pc.get("input_state") in {"idle", "unavailable", "unknown"}
            )
            if possible_bed:
                primary = {
                    "state": "possible_bed_phone",
                    "label": "可能躺着使用手机",
                    "confidence": 0.78,
                    "since": max(
                        _number((phone.get("posture") or {}).get("since"), now),
                        _number((phone.get("motion") or {}).get("since"), now),
                    ),
                    "evidence": ["手机横屏并有轻微移动", "环境较暗", "电脑没有近期输入"],
                }
            elif phone_active:
                label = f"正在使用手机（{_clean_text(app, 40)}）" if app else "正在使用手机"
                primary = {
                    "state": "phone_active",
                    "label": label,
                    "confidence": 0.87,
                    "since": _number((phone.get("motion") or {}).get("since"), now),
                    "evidence": ["手机亮屏且持续有轻微移动"],
                }
            elif pc.get("input_state") == "recent":
                primary = {
                    "state": "temporarily_away",
                    "label": "可能刚离开电脑",
                    "confidence": 0.65,
                    "since": now - idle_seconds,
                    "evidence": [f"电脑已{_duration_text(idle_seconds)}没有键鼠输入"],
                }

        notifications, filtered = self._fresh_notifications(now)
        return {
            "updated_at": now,
            "primary": primary,
            "pc": pc,
            "phone": phone,
            "notifications": notifications,
            "filtered_notifications": filtered,
        }

    @staticmethod
    def _notification_line(item: dict, now: float) -> str:
        app = _clean_text(item.get("app_name") or item.get("package_name"), 36)
        title = _clean_text(item.get("title"), 50)
        text = _clean_text(item.get("text"), 110)
        label = " · ".join(part for part in (app, title) if part)
        content = f"：{text}" if text else ""
        age = _age_text(now - _number(item.get("received_at"), now))
        return f"- {label or '通知'}{content}（{age}）"

    @staticmethod
    def _pc_observation_line(pc: dict) -> str:
        parts = []
        display = str(pc.get("display") or "unknown")
        if display == "off":
            parts.append("熄屏")

        idle_seconds = _number(pc.get("idle_seconds"), float("inf"))
        if isfinite(idle_seconds):
            idle_seconds = max(0, int(idle_seconds))
            age = (
                f"{idle_seconds} 秒前"
                if idle_seconds < 60
                else f"约{idle_seconds // 60}分钟前"
            )
            parts.append(f"{age}有键鼠输入")

        app = _clean_text(pc.get("app"), 60)
        if app:
            parts.append(f"前台为 {app}")
        return f"电脑：{'，'.join(parts)}。" if parts else ""

    @staticmethod
    def _phone_observation_line(phone: dict[str, dict]) -> str:
        def value(key: str) -> str:
            return str((phone.get(key) or {}).get("value") or "")

        parts = []
        for key, mapping in (
            ("screen", SCREEN_TEXT),
            ("posture", POSTURE_TEXT),
            ("motion", MOTION_TEXT),
            ("light", LIGHT_TEXT),
        ):
            text = mapping.get(value(key))
            if text:
                parts.append(text)

        if value("proximity") == "near":
            parts.append("顶部被遮挡或贴近物体")

        screen_slot = phone.get("screen") or {}
        app_slot = phone.get("foreground_app") or {}
        screen_started_at = _number(
            screen_slot.get("since"),
            _number(screen_slot.get("observed_at"), 0),
        )
        app_observed_at = _number(app_slot.get("observed_at"), 0)
        app = _clean_text(app_slot.get("value"), 60)
        app_is_current = (
            screen_slot.get("value") == "on"
            and app_observed_at >= screen_started_at
        )
        if app_is_current and app and app != "unknown":
            parts.append(f"前台为 {FOREGROUND_APP_TEXT.get(app, app)}")
        return f"手机：{'、'.join(parts)}。" if parts else ""

    def prompt(self, pc: dict, now: float, max_chars: int = PROMPT_MAX_CHARS) -> str:
        max_chars = max(120, min(PROMPT_MAX_CHARS, int(max_chars)))
        snap = self.snapshot(pc, now)
        device_lines = [
            line
            for line in (
                self._pc_observation_line(snap["pc"]),
                self._phone_observation_line(snap["phone"]),
            )
            if line
        ]
        if not device_lines and not snap["notifications"]:
            return ""
        lines = ["【设备当前状态】", *device_lines]
        updated_line = f"更新时间：{datetime.fromtimestamp(now).strftime('%H:%M')}"
        footer = "数据来自设备观测，请结合当前时间和对话上下文自行判断用户当前状态。"
        if snap["notifications"]:
            lines.append("近期有效信息：")
            for item in snap["notifications"]:
                candidate = self._notification_line(item, now)
                trial = "\n".join(lines + [candidate, updated_line, footer])
                if len(trial) > max_chars:
                    break
                lines.append(candidate)
        if len("\n".join(lines + [updated_line, footer])) <= max_chars:
            lines.extend([updated_line, footer])
        return "\n".join(lines)[:max_chars].rstrip()
