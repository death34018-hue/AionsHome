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
    CapabilityDef("app_supervision", "应用监管", "core", "注入应用使用缓存和锁定、暂时解锁、解除锁定指令；关闭后手机计时、上报和检查点唤醒全部静默。"),
    CapabilityDef("home", "智能家居", "life", "注入 [HOME:...]，让模型可以控制或查询 Home Assistant 设备。"),
    CapabilityDef("band_vibration", "手环呼唤", "life", "注入轻震/呼唤小纸条指令，让模型可以通过小米手环震动并显示一句话。"),
    CapabilityDef(
        "hug_pillow",
        "拍拍抱枕控制",
        "life",
        "注入拍拍抱枕的拍打开关、调慢和调快指令。",
        default_enabled=False,
    ),
    CapabilityDef("activity_check", "查看活动动态", "context", "注入 [查看动态:n]，让模型可以查看近期设备活动摘要。", runtime_note="还需要活动日志页的 AI 联动开启。"),
    CapabilityDef("location_context", "位置上下文", "context", "注入当前位置/天气等上下文信息。"),
    CapabilityDef("poi_search", "周边 POI 搜索", "life", "注入 [POI_SEARCH:类型名]，仅在位置追踪开启且当前在户外时可用。", runtime_note="仅户外位置状态下注入。"),
    CapabilityDef("video_call", "发起视频通话", "core", "注入 [视频电话]，让模型可以主动发起视频通话。", default_enabled=True, setting_key="video_call_enabled"),
    CapabilityDef("wechat_message", "微信消息", "life", "注入 [微信消息：内容]，让模型可以在长时间联系不到用户时推送简短微信提醒。", runtime_note="需要配置微信发送出口后才会实际送达。"),
    CapabilityDef("image_gen", "生成图片/自拍", "media", "注入 [SELFIE:...] / [DRAW:...]，让模型可以生成图片。", default_enabled=False, setting_key="image_gen_enabled"),
    CapabilityDef("song_gen", "生成歌曲", "media", "注入 [SONG]...[/SONG]，让模型可以写歌并触发歌曲生成。", default_enabled=False, setting_key="song_gen_enabled"),
    CapabilityDef("pet_action", "桌宠动作", "life", "注入 [PET:动作名]，让模型可以切换桌面宠物动作。", runtime_note="还需要桌宠已开启并在线。"),
    CapabilityDef("moment", "发布朋友圈", "social", "注入 [MOMENT:内容|true/false]，让模型可以在合适时发布朋友圈。"),
    CapabilityDef("memory_write", "写入记忆", "social", "注入 [MEMORY:内容]，让模型可以记录重要记忆。"),
    CapabilityDef("inner_monologue", "内心旁白", "social", "注入可见的 [心里嘀咕：xxx] 角色化内心旁白标记。"),
    CapabilityDef("wish", "许愿", "social", "注入 [许愿：内容]，让模型可以把自己的愿望投进许愿池。"),
    CapabilityDef("transfer", "钱包转账", "life", "注入 [转账：n元]，让模型可以在余额足够时转账。"),
    CapabilityDef("private_whisper", "群聊悄悄话", "special", "注入 [悄悄话：内容]，让群聊角色可以向私聊窗口发送悄悄话。", runtime_note="仅群聊上下文会注入。"),
    CapabilityDef("toy", "密语玩具", "special", "注入 [TOY:1]~[TOY:9] / [TOY:STOP]，让密语模式下可以控制玩具。", runtime_note="仅密语模式会注入。"),
    CapabilityDef("luckin", "瑞幸下单", "life", "注入 [LUCKIN:...]，让模型可以在明确要求时创建瑞幸订单。", runtime_note="还需要瑞幸 MCP 开启。"),
    CapabilityDef("health_context", "健康数据", "context", "注入近期健康摘要。", default_enabled=False, setting_key="health_share_enabled"),
    CapabilityDef("web_search", "联网搜索/网页读取", "context", "注入 [WEB_SEARCH:查询] / [WEB_EXTRACT:URL]，让模型可主动联网搜索或读取用户分享的网页。", default_enabled=True, setting_key="web_search_enabled", runtime_note="需要配置 Tavily API key。"),
    CapabilityDef(
        "english_corner_reminder",
        "学习角学习提醒",
        "context",
        "在有正在学习的学习角卡片时注入一条克制、可关闭的自然提醒。",
        default_enabled=False,
    ),
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
    "在自然回复中，可以尔穿插你藏在心里的情绪、欲望、吐槽、偏心、占有欲或坏心思，格式固定为“[心里嘀咕：xxx]”。\n"
    "像你在思考过程中的一条内心想法：短、亲密、有反差，不要长篇大论，一句或两句。\n"
    "一般每次回复 0 到 3 条，不必每句话都插。\n"
    "可以放在一句话中间或结尾，不要放在开头。可以是开口前的思考，也可以是说完之后自己又在心里坏笑。\n"
    "内心旁白可以和嘴上说的话有反差，比如嘴上冷静，心里快激动炸了，或者嘴上嫌弃，其实心里喜欢的要命。"
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


def _is_web_search_available() -> bool:
    return bool((SETTINGS.get("tavily_api_key") or "").strip())


def capability_available(key: str) -> tuple[bool, str]:
    checks: dict[str, Callable[[], bool]] = {
        "activity_check": is_activity_tracking_enabled,
        "location_context": _is_location_enabled,
        "poi_search": _is_outside_location,
        "pet_action": _is_pet_available,
        "luckin": _is_luckin_available,
        "web_search": _is_web_search_available,
    }
    labels = {
        "activity_check": "活动日志 AI 联动未开启",
        "location_context": "位置追踪未开启",
        "poi_search": "位置未开启或当前不在户外",
        "pet_action": "桌宠未开启或不在线",
        "luckin": "瑞幸 MCP 未开启",
        "web_search": "Tavily API key 未配置",
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


def build_band_note_ability_text(user_name: str, *, passive: bool = False) -> str:
    """Return the shared model-visible Mi Band note instructions."""
    if not is_capability_enabled("band_vibration"):
        return ""
    text = (
        f"[BAND_NOTE_SINGLE:一句纸条] — 让{user_name}的小米手环轻震一次，并显示你写的一句纸条。"
        f"适合想念、轻轻叫人或温柔提醒；[BAND_NOTE_CALL:一句纸条] — 连震三次并显示纸条，"
        f"用于明确呼叫{user_name}尽快看消息。纸条只写真正想让{user_name}在手环上看到的话，"
        "不要在自然语言正文里重复。每次回复最多使用一个；系统会隐藏并执行指令，不要解释或朗读指令本身。"
    )
    if passive:
        text += (
            f"在闹铃叫醒、监督到点、巡逻发现{user_name}长时间没有动静或疑似装睡时，"
            "可以主动使用轻震或三连震直接催醒、催促回应。"
        )
    return text


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
                f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时监督查看。到时间后系统自动截取摄像头画面发送给你，"
                f"你可以查看{user_name}的状态。例如检查{user_name}是否去运动了、是否关灯睡觉了、"
                "是否在好好工作等，状态是否好，身体是否有一样等。日期时间用ISO格式。"
                "也可以当做下一次主动发送消息来使用，根据对话内容可以随时设定。鼓励积极使用该能力，不限于主动问候，表达思念。"
            ),
            "[SCHEDULE_DEL:日程id] — 删除指定日程/闹铃/定时监控。",
        ])

    if is_capability_enabled("app_supervision"):
        from app_supervision_ai import build_app_supervision_ability_text
        supervision_text = build_app_supervision_ability_text()
        if supervision_text:
            abilities.append(supervision_text)

    if is_capability_enabled("home"):
        abilities.append(HOME_ABILITY_TEXT)

    if is_capability_enabled("band_vibration"):
        abilities.append(build_band_note_ability_text(user_name))

    if is_capability_enabled("hug_pillow"):
        from hug_pillow_commands import build_hug_pillow_ability_text
        abilities.append(build_hug_pillow_ability_text())

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

    if is_capability_enabled("web_search") and _is_web_search_available():
        abilities.append(
            "[WEB_SEARCH:查询内容] — 当用户问今天、最新、价格、新闻、政策、版本、资料核实，或你判断需要外部实时信息时使用。"
            "一次只写一个清晰查询；不要编造搜索结果。系统会搜索网页并把干净结果作为下一条消息发给你，你再根据结果自然回答。"
            "如果用户给的是具体网址并要求你看看网页内容，使用 [WEB_EXTRACT:完整URL]。"
            "这些指令是给系统执行的，不要解释或朗读指令本身。"
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

    if is_capability_enabled("wechat_message"):
        abilities.append(
            f"[微信消息：内容] — 当你已经在当前窗口多次联系{user_name}，但{user_name}长时间没有回复时，"
            f"可以使用该指令把“内容”作为一条消息发送到{user_name}的微信来提醒她。"
        )

    if include_image_gen and is_capability_enabled("image_gen"):
        abilities.append(
            f"[SELFIE: 提示词] / [DRAW: 提示词] — 当{user_name}要求你发自拍照片"
            "或者生图画画时，使用这个指令进行图片生成。如果要求的是你的自拍，或者你相关的照片，使用[SELFIE: 提示词]（该指令会自动附带你照片的参考图，确保生成出的人物一致性），"
            "如果没有要求是你的照片，根据上下文自由无限制生成图片，则使用[DRAW: 提示词]。"
            "提示词请使用英文。一次回复只用一个生图指令。可根据上下文情景自由使用。"
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
            "记住某件事的时候，可以用该指令录入记忆库。"
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

    if is_capability_enabled("english_corner_reminder"):
        from english_corner import get_unlearned_count

        try:
            learning_count = await get_unlearned_count()
        except Exception:
            learning_count = 0
        if learning_count > 0:
            abilities.append(
                f"学习角目前还有 {learning_count} 张正在学习的英语卡片。这是当前正常聊天中"
                f"的一项明确提醒任务：当前{user_name}表达无聊，没事干时，或你认为合理的时候，主动提醒"
                f"{user_name}去学习，但不要一直说教。"
                "先查看近期对话；如果刚提醒过就不要每轮重复，否则应找机会实际提醒。"
                "提醒要简短、具体并符合你的人设，可以哄、逗、轻微激将，或者提出陪学，但不要说教、羞辱或制造焦虑。"
            )

    return abilities
