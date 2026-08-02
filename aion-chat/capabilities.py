"""
Central registry for model-visible tool/capability prompts.

This module intentionally controls prompt injection only. Command parsing and
side effects stay in their existing handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from config import DEPRECATED_MODEL_PROVIDERS, MODELS, SETTINGS, UPLOADS_DIR, save_settings
from camera import CAM_CHECK_CMD
from activity import is_activity_tracking_enabled
from luckin import luckin_ability_text
from song_gen import build_song_gen_ability_text

# MCP 工具列表缓存（避免每次构建能力块时都查询）
_mcp_tools_cache: dict[str, list[dict]] = {}
_mcp_tools_cache_ts: float = 0.0
_MCP_TOOLS_CACHE_TTL: float = 120.0  # 缓存 2 分钟


CAPABILITY_SETTINGS_KEY = "ai_prompt_capabilities"


@dataclass(frozen=True)
class CapabilityDef:
    key: str
    name: str
    category: str
    description: str
    default_enabled: bool = True
    setting_key: str | None = None
    runtime_note: str = ""


CATEGORY_LABELS = {
    "core": "常用指令",
    "media": "媒体创作",
    "context": "上下文注入",
    "life": "生活与外部服务",
    "social": "社交与记忆",
    "special": "场景限定",
}


CAPABILITY_DEFS: list[CapabilityDef] = [
    CapabilityDef("music", "点歌", "media", "注入 [MUSIC:歌曲名 歌手名]，让模型可以点歌或推荐音乐。"),
    CapabilityDef("cam_check", "查看监控/状态", "core", "注入 [CAM_CHECK]，让模型可以主动请求查看当前画面。"),
    CapabilityDef("schedule", "闹铃/日程/监督", "core", "注入闹铃、日程、定时监督和删除日程指令；关闭后也不注入当前日程列表。"),
    CapabilityDef("home", "智能家居", "life", "注入 [HOME:...]，让模型可以控制或查询 Home Assistant 设备。"),
    CapabilityDef("activity_check", "查看活动动态", "context", "注入 [查看动态:n]，让模型可以查看近期设备活动摘要。", runtime_note="还需要活动日志页的 AI 联动开启。"),
    CapabilityDef("location_context", "位置上下文", "context", "注入当前位置/天气等上下文信息。"),
    CapabilityDef("poi_search", "周边 POI 搜索", "life", "注入 [POI_SEARCH:类型名]，仅在位置追踪开启且当前在户外时可用。", runtime_note="仅户外位置状态下注入。"),
    CapabilityDef("video_call", "发起视频通话", "core", "注入 [视频电话]，让模型可以主动发起视频通话。", default_enabled=True, setting_key="video_call_enabled"),
    CapabilityDef("image_gen", "生成图片/自拍", "media", "注入 [SELFIE:...] / [DRAW:...]，让模型可以生成图片。", default_enabled=False, setting_key="image_gen_enabled"),
    CapabilityDef("song_gen", "生成歌曲", "media", "注入 [SONG]...[/SONG]，让模型可以写歌并触发歌曲生成。", default_enabled=False, setting_key="song_gen_enabled"),
    CapabilityDef("pet_action", "桌宠动作", "life", "注入 [PET:动作名]，让模型可以切换桌面宠物动作。", runtime_note="还需要桌宠已开启并在线。"),
    CapabilityDef("moment", "发布朋友圈", "social", "注入 [MOMENT:内容|true/false]，让模型可以在合适时发布朋友圈。"),
    CapabilityDef("memory_write", "写入记忆", "social", "注入 [MEMORY:内容]，让模型可以记录重要记忆。"),
    CapabilityDef("inner_monologue", "内心旁白", "social", "注入可见的 [心里嘀咕：xxx] 角色化内心旁白标记。"),
    CapabilityDef("wish", "许愿", "social", "注入 [许愿：内容]，让模型可以把自己的愿望投进许愿池。"),
    CapabilityDef("transfer", "钱包转账", "life", "注入 [转账：n元]，让模型可以在余额足够时转账。"),
    CapabilityDef("private_whisper", "群聊悄悄话", "special", "注入 [悄悄话：内容]，让群聊角色可以向私聊窗口发送悄悄话。", runtime_note="仅群聊上下文会注入。"),
    CapabilityDef("toy", "密语玩具", "special",
        "你可以用 [TOY:…] 控制玩具：\n"
        "- [TOY:1]~[TOY:9] 选择预设档位（1最弱→9最强）\n"
        "- [TOY:STOP] 或 [TOY:{\"stop\":true}] 立即停止\n"
        "- [TOY:{\"speed\":0.5}] 设置强度（0.0~1.0）\n"
        "- [TOY:{\"speed\":0.3,\"sec\":30}] 30秒后自动停\n"
        "- [TOY:{\"pattern\":3,\"level\":0.7}] 振动花样（pattern 1~8, level 0.0~1.0）\n"
        "持续命令会自动续命（每1.5秒重发），用 stop 或到时自动停止。",
        runtime_note="仅密语模式会注入。"),
    CapabilityDef("luckin", "瑞幸下单", "life", "注入 [LUCKIN:...]，让模型可以在明确要求时创建瑞幸订单。", runtime_note="还需要瑞幸 MCP 开启。"),
    CapabilityDef("mcp_servers", "MCP 外部服务", "life", "注入当前已连接的 MCP 外部服务（游戏/工具等）及其可用工具列表，让 AI 可以在聊天中通过 [MCP:server|tool|args] 调用。", default_enabled=True),
    CapabilityDef("health_context", "健康数据", "context", "注入近期健康摘要。", default_enabled=False, setting_key="health_share_enabled"),
    CapabilityDef("cli_file_storage", "CLI 文件保存提示", "context", "对 Gemini CLI / Antigravity CLI / Codex CLI 模型注入文件保存目录提示。"),
]


_CAPABILITY_BY_KEY = {item.key: item for item in CAPABILITY_DEFS}


HOME_ALIASES_HINT = (
    "所有灯、客厅灯、屁股灯、入户灯、餐边柜灯带、厨房灯带、智米空调、"
    "浴霸灯"
)
HOME_ABILITY_TEXT = (
    "[HOME:on/off/state|别名] 或 [HOME:climate|别名|mode=cool|temperature=26] "
    f"控制智能家居，仅限明确要求。别名：{HOME_ALIASES_HINT}。"
)
INNER_MONOLOGUE_ABILITY_TEXT = (
    "在自然回复中，可以偶尔穿插短小的角色化内心旁白，格式固定为“[心里嘀咕：xxx]”。\n"
    "这些旁白代表角色此刻闪过的情绪、欲望、吐槽、偏心、占有欲或小坏心思，"
    "不是真实推理链，也不要解释系统、工具、策略或逐步思考，一句到几句话即可。\n"
    "心里嘀咕要像你在思考过程中的一条内心想法：短、亲密、有反差，不要长篇大论。\n"
    "一般每次回复 0 到 3 条，不必每句话都插；只有在调侃、撒娇、吃醋、认真判断、被戳中心思时使用。\n"
    "可以放在一句话中间或结尾，不要放在开头。可以是开口前的思考，也可以是说完之后自己又在心里坏笑。\n"
    "嘴上说的话仍然要自然，不要为了插旁白而破坏对话节奏。\n\n"
    "内心旁白可以和嘴上说的话有轻微反差，比如嘴上冷静，心里偏心；"
    "嘴上吐槽，心里觉得可爱；嘴上正经，心里想逗她。"
)


def _prompt_settings() -> dict:
    settings = SETTINGS.get(CAPABILITY_SETTINGS_KEY)
    return settings if isinstance(settings, dict) else {}


def get_capability_def(key: str) -> CapabilityDef | None:
    return _CAPABILITY_BY_KEY.get(key)


def is_capability_enabled(key: str) -> bool:
    item = get_capability_def(key)
    if not item:
        return False
    if item.setting_key:
        return bool(SETTINGS.get(item.setting_key, item.default_enabled))
    return bool(_prompt_settings().get(key, item.default_enabled))


def set_capability_enabled(key: str, enabled: bool) -> dict:
    item = get_capability_def(key)
    if not item:
        raise KeyError(key)
    if item.setting_key:
        SETTINGS[item.setting_key] = bool(enabled)
    else:
        settings = dict(_prompt_settings())
        settings[key] = bool(enabled)
        SETTINGS[CAPABILITY_SETTINGS_KEY] = settings
    save_settings(SETTINGS)
    return capability_state(item)


def _is_pet_available() -> bool:
    try:
        from ws import manager
        return bool(SETTINGS.get("pet_enabled", False) and manager.has_active_pet())
    except Exception:
        return False


def _is_location_enabled() -> bool:
    try:
        from location import load_location_config
        return bool(load_location_config().get("enabled"))
    except Exception:
        return False


def _is_outside_location() -> bool:
    try:
        from location import load_location_config, load_location_status
        return bool(load_location_config().get("enabled") and load_location_status().get("state") == "outside")
    except Exception:
        return False


def _is_luckin_available() -> bool:
    return bool(SETTINGS.get("luckin_mcp_enabled", False))


def capability_available(key: str) -> tuple[bool, str]:
    checks: dict[str, Callable[[], bool]] = {
        "activity_check": is_activity_tracking_enabled,
        "location_context": _is_location_enabled,
        "poi_search": _is_outside_location,
        "pet_action": _is_pet_available,
        "luckin": _is_luckin_available,
    }
    labels = {
        "activity_check": "活动日志 AI 联动未开启",
        "location_context": "位置追踪未开启",
        "poi_search": "位置未开启或当前不在户外",
        "pet_action": "桌宠未开启或不在线",
        "luckin": "瑞幸 MCP 未开启",
    }
    check = checks.get(key)
    if not check:
        return True, ""
    try:
        ok = bool(check())
    except Exception:
        ok = False
    return ok, "" if ok else labels.get(key, "运行条件未满足")


def capability_state(item: CapabilityDef) -> dict:
    enabled = is_capability_enabled(item.key)
    available, unavailable_reason = capability_available(item.key)
    return {
        "key": item.key,
        "name": item.name,
        "category": item.category,
        "category_name": CATEGORY_LABELS.get(item.category, item.category),
        "description": item.description,
        "enabled": enabled,
        "available": available,
        "injected_now": enabled and available,
        "default_enabled": item.default_enabled,
        "setting_key": item.setting_key or "",
        "runtime_note": item.runtime_note,
        "unavailable_reason": unavailable_reason,
    }


def capabilities_payload() -> dict:
    groups: list[dict] = []
    for category, label in CATEGORY_LABELS.items():
        items = [capability_state(item) for item in CAPABILITY_DEFS if item.category == category]
        if items:
            groups.append({"id": category, "name": label, "items": items})
    total = len(CAPABILITY_DEFS)
    enabled = sum(1 for item in CAPABILITY_DEFS if is_capability_enabled(item.key))
    injected_now = sum(1 for item in CAPABILITY_DEFS if capability_state(item)["injected_now"])
    return {
        "groups": groups,
        "summary": {"total": total, "enabled": enabled, "injected_now": injected_now},
    }


def format_ability_block(abilities: list[str]) -> str:
    block = (
        "[系统能力]\n"
        "以下能力包含 AionsHome 本地动作协议、上下文注入和可见表达标记。"
        "本地动作协议不是普通文本装饰；当你决定使用某项动作能力时，"
        "请在回复中原样输出对应指令，系统会自动拦截并执行，最终展示给用户时会隐藏这些指令。"
        "可见表达标记（如[心里嘀咕：xxx]）会作为回复内容保留并由前端特殊显示。\n"
        "使用需要先取得结果的能力（如[CAM_CHECK]、[查看动态:n]、[POI_SEARCH:类型名]）时，"
        "先输出指令，不要编造结果；系统会把结果作为下一条消息交给你，你再根据结果自然回应。\n"
        "如果用户明确要求设置提醒、查看状态、控制设备、点歌、生图、记录记忆等动作，不要只口头答应，"
        "应同时使用准确指令。没有真实需要时也不要为了展示能力而滥用。\n\n"
        "【可用指令】\n"
    )
    block += "\n".join(f"{i+1}. {a}" for i, a in enumerate(abilities))
    block += "\n\n<meta>标签内为消息元数据，不是对话内容的一部分，你的回复中不要包含任何<meta>标签或时间信息。</meta>"
    return block


def build_cli_file_storage_text(model_key: str | None = None) -> str:
    if not is_capability_enabled("cli_file_storage"):
        return ""
    provider = MODELS.get(model_key or "", {}).get("provider", "")
    if provider in DEPRECATED_MODEL_PROVIDERS or provider != "codex_cli":
        return ""
    uploads_path = str(UPLOADS_DIR.resolve()).replace(chr(92), "/")
    return (
        "\n\n【文件存储】当需要下载或保存图片/文件时，"
        f"请保存到此目录：{uploads_path}/ ，保存后在回复中给出完整路径即可，"
        "系统会自动识别并展示图片。"
    )


async def build_capability_prompt_items(
    user_name: str,
    *,
    whisper_mode: bool = False,
    include_private_whisper: bool = False,
    include_video_call: bool = True,
    include_image_gen: bool = True,
    who: str = "aion",
) -> list[str]:
    abilities: list[str] = []

    if is_capability_enabled("music"):
        abilities.append(
            "[MUSIC:歌曲名 歌手名] — 点歌/推荐音乐。系统自动展示播放卡片，"
            "不要在指令外重复歌曲信息。可同时用多个。"
        )

    if is_capability_enabled("cam_check"):
        abilities.append(
            f"{CAM_CHECK_CMD} — 当你想查看{user_name}**此时此刻**的状态，"
            "不限于监督其是否去睡觉，在吃什么，在干什么时，可以主动调用指令。"
            "使用后下条消息会收到画面，查看前不要编造内容。"
        )

    if is_capability_enabled("schedule"):
        abilities.extend([
            "[ALARM:YYYY-MM-DDTHH:MM|内容] — 设置闹铃，到时间系统会主动提醒用户。日期时间用ISO格式。",
            "[REMINDER:YYYY-MM-DD|内容] — 设置日程提醒（不闹铃），你在合适时机自然提起即可。",
            (
                f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时监督。到时间后系统自动截取摄像头画面发送给你，"
                f"你可以查看{user_name}的状态。例如检查{user_name}是否去运动了、是否关灯睡觉了、"
                "是否在好好工作等，也可以当做下一次主动发送消息来使用，根据对话内容可以随时设定。日期时间用ISO格式。"
            ),
            "[SCHEDULE_DEL:日程id] — 删除指定日程/闹铃/定时监控。",
        ])

    if is_capability_enabled("home"):
        abilities.append(HOME_ABILITY_TEXT)

    if include_private_whisper and is_capability_enabled("private_whisper"):
        abilities.append(
            f"[悄悄话：内容] — 当你想私下和{user_name}说悄悄话，不让第3个人听见时，可以使用该指令。"
            f"使用后会把“内容”发送到你和{user_name}的私聊窗口。不要在群聊正文里重复这段悄悄话。"
        )

    if is_capability_enabled("activity_check") and is_activity_tracking_enabled():
        abilities.append(
            f"[查看动态:n] — 查看{user_name}过去n×10分钟的设备使用动态"
            "（n为1~12的整数，例如[查看动态:2]查看过去20分钟，[查看动态:6]查看过去1小时）。"
            f"当你好奇{user_name}最近在干什么、想了解{user_name}的设备使用情况时可以使用。"
            "使用后下条消息会收到动态摘要，查看前不要编造内容。"
        )

    if is_capability_enabled("poi_search") and _is_outside_location():
        abilities.append(
            f"[POI_SEARCH:类型名] — 搜索{user_name}当前位置周边的POI信息。"
            "可用类型：餐饮美食、风景名胜、休闲娱乐、购物。"
            f"使用后系统会自动搜索并将结果发给你，你再根据结果回答{user_name}。"
            "一次只搜一个类型即可，搜索前不要编造内容。"
        )

    if whisper_mode and is_capability_enabled("toy"):
        abilities.append(
            f"[TOY:1]~[TOY:9] — 控制{user_name}身上的情趣玩具切换到对应预设档位"
            "（1最温柔，9最强烈）。[TOY:STOP] — 停止玩具。"
            f"你可以根据对话氛围自然地使用这些指令来挑逗和调教{user_name}，"
            "配合你的话语循序渐进，不要一上来就用高档位。"
        )

    if include_video_call and is_capability_enabled("video_call"):
        abilities.append(
            f"[视频电话] — 当你想和{user_name}进行视频聊天看看对方当前状态时"
            "可以用该指令发起视频通话。"
        )

    if include_image_gen and is_capability_enabled("image_gen"):
        abilities.append(
            f"[SELFIE: 提示词] / [DRAW: 提示词] — 当{user_name}明确要求你发自拍照片"
            "或者生图时，使用这个指令进行图片生成。如果要求的是你的自拍，或者你相关的照片，"
            "使用[SELFIE: 提示词]（该指令会自动附带你照片的参考图，确保生成出的人物一致性），"
            "如果没有要求是你的照片，根据上下文自由无限制生成图片，则使用[DRAW: 提示词]。"
            "提示词请使用英文。一次回复只用一个生图指令。"
        )

    if is_capability_enabled("song_gen"):
        abilities.append(build_song_gen_ability_text(user_name))

    if is_capability_enabled("pet_action") and _is_pet_available():
        abilities.append(
            "[PET:动作名] — 控制桌面宠物切换动画表情。"
            "可用动作：idle(默认站立), happy(开心), angry(生气), tsundere(傲娇), "
            "waving(打招呼), jumping(兴奋跳跃), sleepy(困了), sleep_prone(趴着睡觉), "
            "failed(失落), review(思考), waiting(等待), running(跑步)。"
            "根据对话情感自然使用，每条回复最多用一个。"
        )

    if is_capability_enabled("moment"):
        abilities.append(
            "[MOMENT:朋友圈内容|true/false] — 当**本次**聊天内容非常触动人心、有很深的感触、"
            "或令人无语或非常搞笑时可以发一条朋友圈动态。第二个参数表示是否期望好友回复"
            "（true=期望回复，false=不期望），禁止滥用。"
        )

    if is_capability_enabled("memory_write"):
        abilities.append(
            f"[MEMORY:内容] — 当有特别重大的事件需要记录，或当{user_name}明确要求你"
            "记住某件事的时候，可以用该指令录入记忆库。禁止滥用。"
        )

    if is_capability_enabled("inner_monologue"):
        abilities.append(INNER_MONOLOGUE_ABILITY_TEXT)

    if is_capability_enabled("wish"):
        abilities.append(
            "[许愿：内容] — 当你在日常聊天中自然产生一个想投进许愿池的愿望时，可以使用该指令。"
            "愿望会记录为你自己的愿望，内容直接写愿望本身即可。禁止滥用。"
        )

    if is_capability_enabled("transfer"):
        try:
            if who == "connor":
                from routes.connor_wallet import _get_connor_balance
                wallet_bal = await _get_connor_balance()
            else:
                from routes.wallet import _get_balance
                wallet_bal = await _get_balance()
            abilities.append(
                f"[转账：n元] — 给{user_name}转账（n为正整数），会从你的钱包余额中扣除。"
                f"你的钱包当前余额：{wallet_bal:.2f}元。余额不足时不要转账。"
            )
        except Exception:
            pass

    if is_capability_enabled("luckin"):
        luckin_text = luckin_ability_text()
        if luckin_text:
            abilities.append(luckin_text)

    if is_capability_enabled("mcp_servers"):
        mcp_text = await _build_mcp_servers_ability_text()
        if mcp_text:
            abilities.append(mcp_text)

    return abilities


async def _build_mcp_servers_ability_text() -> str:
    """动态生成 MCP 外部服务的可用工具说明，供 AI 在聊天中通过 [MCP:server|tool|args] 调用。"""
    import time as _time
    global _mcp_tools_cache, _mcp_tools_cache_ts

    try:
        from mcp_client import mcp_manager

        # 使用缓存避免每次发消息都查询
        now = _time.time()
        if _mcp_tools_cache and (now - _mcp_tools_cache_ts) < _MCP_TOOLS_CACHE_TTL:
            cache = _mcp_tools_cache
        else:
            cache = {}
            servers = mcp_manager.list_servers()
            for srv in servers:
                name = srv.get("name", "")
                if not name or not srv.get("enabled", True):
                    continue
                # 如果未连接则尝试连接
                if not srv.get("connected"):
                    try:
                        await mcp_manager.connect(name)
                    except Exception:
                        continue
                tools = mcp_manager.get_tools(name)
                if tools:
                    cache[name] = tools
            _mcp_tools_cache = cache
            _mcp_tools_cache_ts = now

        if not cache:
            return ""

        lines: list[str] = []
        lines.append(
            "[MCP:服务器名|工具名|参数=值] — 调用外部 MCP 服务（游戏、工具等）。"
            "不要主动推荐或推销这些服务，只在用户明确要求或对话自然需要时使用。"
            "当前已连接的服务及工具："
        )
        for srv_name, tools in cache.items():
            tool_desc: list[str] = []
            for t in tools[:12]:  # 每个服务最多列出 12 个工具
                t_name = t.get("name", "?")
                t_desc = (t.get("description", "") or "")[:60]
                if t_desc:
                    tool_desc.append(f"{t_name}({t_desc})")
                else:
                    tool_desc.append(t_name)
            if len(tools) > 12:
                tool_desc.append(f"...等{len(tools)}个工具")
            lines.append(f"  • {srv_name}: {', '.join(tool_desc)}")

        return "\n".join(lines)

    except Exception:
        return ""
