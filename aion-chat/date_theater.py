import json
import re
import time
from pathlib import Path

from config import DATA_DIR, DEFAULT_MODEL, PUBLIC_DIR, load_worldbook


DATE_ASSET_DIR_NAME = "去约会小剧场素材"
DATE_ASSET_DIR = PUBLIC_DIR / DATE_ASSET_DIR_NAME
DATE_ASSET_PUBLIC_PREFIX = f"/public/{DATE_ASSET_DIR_NAME}"
DATE_CONFIG_PATH = DATA_DIR / "date_theater_config.json"
DATE_THEATER_TTS_CACHE_DIR = DATA_DIR / "date_theater_tts_cache"
DATE_THEATER_TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
DATE_THEATER_TTS_SEGMENT_DELETE_DELAY_SECONDS = 2 * 60 * 60

DEFAULT_BACKGROUND_ID = "背景-客厅"
DEFAULT_STATE_ID = "平静"

DATE_STAGE_TAG_PATTERN = re.compile(
    r"\[(DATE_(?:BACKGROUND|BG|STATE|ACTION)\s*:\s*[^\]]+|DATE_END_READY)\]",
    re.IGNORECASE,
)
DATE_BACKGROUND_PATTERN = re.compile(r"\[DATE_(?:BACKGROUND|BG)\s*:\s*([^\]]+)\]", re.IGNORECASE)
DATE_STATE_PATTERN = re.compile(r"\[DATE_(?:STATE|ACTION)\s*:\s*([^\]]+)\]", re.IGNORECASE)
DATE_END_READY_PATTERN = re.compile(r"\[DATE_END_READY\]", re.IGNORECASE)
MUSIC_TAG_PATTERN = re.compile(r"\[MUSIC\s*:\s*([^\]]+)\]", re.IGNORECASE)


def _default_persona_text() -> str:
    return "这是一个独立的约会人设：温柔、克制、会自然推进暧昧氛围，但不使用数值化的心情、亲密度或游戏状态。"


def _clean_id(value: str, fallback: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


def normalize_persona_presets(cfg: dict) -> dict:
    cfg = dict(cfg or {})
    legacy_persona = str(cfg.get("persona") or "").strip() or _default_persona_text()
    fallback_name = str(cfg.get("partner_name") or "").strip() or "AI"
    raw_presets = cfg.get("persona_presets")
    presets: list[dict] = []
    seen: set[str] = set()
    if isinstance(raw_presets, list):
        for idx, item in enumerate(raw_presets, start=1):
            if not isinstance(item, dict):
                continue
            pid = _clean_id(item.get("id") or item.get("name"), f"preset_{idx}")
            while pid in seen:
                pid = f"{pid}_{idx}"
            name = str(item.get("name") or pid).strip() or pid
            persona = str(item.get("persona") or "").strip()
            if not persona:
                continue
            seen.add(pid)
            presets.append({"id": pid, "name": name[:40], "persona": persona})
    if not presets:
        presets = [{"id": "default", "name": fallback_name[:40], "persona": legacy_persona}]
    active_id = str(cfg.get("active_persona_id") or "").strip()
    if active_id not in {item["id"] for item in presets}:
        active_id = presets[0]["id"]
    active = next((item for item in presets if item["id"] == active_id), presets[0])
    cfg["persona_presets"] = presets
    cfg["active_persona_id"] = active["id"]
    cfg["persona"] = active["persona"]
    cfg["partner_name"] = str(active.get("name") or fallback_name or "AI").strip() or "AI"
    return cfg


def resolve_date_model(*, requested: str = "", cfg: dict | None = None, chatroom_cfg: dict | None = None, model_keys: list[str] | tuple[str, ...] | set[str] | None = None) -> str:
    keys = [str(k or "").strip() for k in (model_keys or []) if str(k or "").strip()]
    valid = set(keys)
    cfg = cfg or {}
    chatroom_cfg = chatroom_cfg or {}
    candidates = [requested]
    if cfg.get("model_locked"):
        candidates.append(cfg.get("model"))
    candidates.extend([
        chatroom_cfg.get("aion_model"),
        cfg.get("model"),
        DEFAULT_MODEL,
    ])
    for item in candidates:
        model = str(item or "").strip()
        if model and (not valid or model in valid):
            return model
    return keys[0] if keys else DEFAULT_MODEL


def _default_config() -> dict:
    wb = load_worldbook()
    persona = _default_persona_text()
    partner_name = (wb.get("ai_name") or "AI").strip() or "AI"
    return {
        "partner_name": partner_name,
        "persona": persona,
        "persona_presets": [{"id": "default", "name": partner_name, "persona": persona}],
        "active_persona_id": "default",
        "model": "",
        "model_locked": False,
    }


def load_date_config() -> dict:
    cfg = _default_config()
    if DATE_CONFIG_PATH.exists():
        try:
            data = json.loads(DATE_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if v is not None})
        except Exception:
            pass
    cfg["partner_name"] = str(cfg.get("partner_name") or "AI").strip() or "AI"
    cfg = normalize_persona_presets(cfg)
    cfg["model"] = str(cfg.get("model") or "").strip()
    return cfg


def save_date_config(data: dict) -> dict:
    cfg = load_date_config()
    if "model" in data and data["model"] is not None:
        cfg["model_locked"] = True
    for key in ("partner_name", "persona", "model", "active_persona_id", "persona_presets", "model_locked"):
        if key in data and data[key] is not None:
            if key == "persona_presets":
                cfg[key] = data[key]
            elif key == "model_locked":
                cfg[key] = bool(data[key])
            else:
                cfg[key] = str(data[key]).strip()
    if not cfg.get("partner_name"):
        cfg["partner_name"] = "AI"
    cfg = normalize_persona_presets(cfg)
    DATE_CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def _asset_url(path: Path, root: Path, public_prefix: str) -> str:
    rel = path.relative_to(root).as_posix()
    return f"{public_prefix.rstrip('/')}/{rel}"


def _sort_with_default(items: list[dict], default_id: str) -> list[dict]:
    return sorted(items, key=lambda item: (0 if item["id"] == default_id else 1, item["id"]))


def scan_date_assets(root: Path | str | None = None, public_prefix: str | None = None) -> dict:
    root = Path(root) if root is not None else DATE_ASSET_DIR
    public_prefix = public_prefix or DATE_ASSET_PUBLIC_PREFIX

    backgrounds: list[dict] = []
    if root.exists():
        for path in root.glob("背景-*.png"):
            backgrounds.append({
                "id": path.stem,
                "name": path.stem.replace("背景-", "", 1),
                "url": _asset_url(path, root, public_prefix),
                "mime": "image/png",
            })

    states: list[dict] = []
    transparent_dir = root / "透明视频"
    if transparent_dir.exists():
        for path in transparent_dir.glob("*.webm"):
            states.append({
                "id": path.stem,
                "name": path.stem,
                "url": _asset_url(path, root, public_prefix),
                "mime": "video/webm",
                "kind": "transparent",
                "loop": True,
            })
    if root.exists():
        for path in root.glob("*.mp4"):
            states.append({
                "id": path.stem,
                "name": path.stem,
                "url": _asset_url(path, root, public_prefix),
                "mime": "video/mp4",
                "kind": "scene",
                "loop": True,
            })

    backgrounds = _sort_with_default(backgrounds, DEFAULT_BACKGROUND_ID)
    states = _sort_with_default(states, DEFAULT_STATE_ID)

    return {
        "root": str(root),
        "public_prefix": public_prefix,
        "backgrounds": backgrounds,
        "states": states,
        "default_background": DEFAULT_BACKGROUND_ID if any(x["id"] == DEFAULT_BACKGROUND_ID for x in backgrounds) else (backgrounds[0]["id"] if backgrounds else ""),
        "default_state": DEFAULT_STATE_ID if any(x["id"] == DEFAULT_STATE_ID for x in states) else (states[0]["id"] if states else ""),
    }


def _ids(items: list[dict]) -> list[str]:
    return [str(item.get("id") or "").strip() for item in items if str(item.get("id") or "").strip()]


def estimate_tokens(text: str) -> int:
    text = str(text or "")
    if not text.strip():
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = max(0, len(text) - cjk)
    return max(1, int(cjk * 0.75 + other / 4 + 0.5))


def build_prompt_usage_report(*, purpose: str, parts: list[dict], usage_meta: dict | None = None) -> dict:
    usage_meta = dict(usage_meta or {})
    clean_parts = []
    for part in parts:
        label = str(part.get("label") or "").strip() or "未命名片段"
        content = str(part.get("content") or "")
        clean_parts.append({
            "label": label,
            "chars": len(content),
            "estimated_tokens": estimate_tokens(content),
        })
    estimated_prompt_tokens = sum(item["estimated_tokens"] for item in clean_parts)
    return {
        "purpose": purpose,
        "parts": clean_parts,
        "estimated_prompt_tokens": estimated_prompt_tokens,
        "actual_prompt_tokens": usage_meta.get("prompt_tokens"),
        "actual_completion_tokens": usage_meta.get("completion_tokens"),
        "actual_total_tokens": usage_meta.get("total_tokens"),
    }


def build_outline_prompt(*, partner_name: str, user_name: str, persona: str, user_prompt: str, assets: dict) -> str:
    backgrounds = "、".join(_ids(assets.get("backgrounds") or [])) or "无"
    states = "、".join(_ids(assets.get("states") or [])) or "无"
    partner_name = partner_name or "AI"
    user_name = user_name or "用户"
    persona = persona or "自然、专注、会推进一场有起承转合的约会。"
    user_prompt = str(user_prompt or "").strip()
    return (
        f"你是{partner_name}，稍后会和{user_name}开启一场全新的独立约会。\n"
        f"独立人设：{persona}\n\n"
        "这场约会不能读取、引用或延续任何普通聊天内容。\n"
        "现在不要直接开始正式约会，只根据用户给出的约会提示生成一份可预览的大纲。\n"
        "用户满意后才会点击开始约会。大纲需要包含过程推进和明确结束契机，避免无限进行。\n"
        "不要输出心情值、亲密度、好感度等游戏数值系统。\n\n"
        f"用户给出的约会提示：\n{user_prompt or '用户还没有补充具体提示。'}\n\n"
        f"可用背景：{backgrounds}\n"
        f"可用动作或场景状态：{states}\n"
        "默认从背景-客厅和平静开始。可为开场选择一个背景和一个状态。\n\n"
        "只输出 JSON，不要 Markdown。格式："
        '{"title":"短标题","outline":"约会大纲，说明开端、推进、转折和收束","opening":"正式开始约会后的第一句开场白","ending_trigger":"结束契机","background":"背景-客厅","state":"平静"}'
    )


def build_start_prompt(*, partner_name: str, user_name: str, persona: str, assets: dict) -> str:
    backgrounds = "、".join(_ids(assets.get("backgrounds") or [])) or "无"
    states = "、".join(_ids(assets.get("states") or [])) or "无"
    partner_name = partner_name or "AI"
    user_name = user_name or "用户"
    persona = persona or "自然、专注、会推进一场有起承转合的约会。"
    return (
        f"你是{partner_name}，正在和{user_name}开启一场全新的独立约会。\n"
        f"独立人设：{persona}\n\n"
        "这场约会不能读取、引用或延续任何普通聊天内容。\n"
        "请生成一个约会标题、一个自然开场白、一个约会过程目标，以及一个明确的结束契机，避免无限进行。\n"
        "不要输出心情值、亲密度、好感度等游戏数值系统。\n\n"
        f"可用背景：{backgrounds}\n"
        f"可用动作或场景状态：{states}\n"
        "默认从背景-客厅和平静开始。可为开场选择一个背景和一个状态。\n\n"
        "只输出 JSON，不要 Markdown。格式："
        '{"title":"短标题","opening":"开场白","ending_trigger":"结束契机","background":"背景-客厅","state":"平静"}'
    )


def build_stage_instruction(assets: dict) -> str:
    backgrounds = "、".join(_ids(assets.get("backgrounds") or [])) or "无"
    states = "、".join(_ids(assets.get("states") or [])) or "无"
    return (
        "你可以用舞台标签控制画面，但标签只放在回复末尾，不要解释标签。\n"
        f"可切换背景：{backgrounds}\n"
        f"可切换动作或场景状态：{states}\n"
        "切换背景用 [DATE_BACKGROUND:背景-客厅]；切换动作或场景状态用 [DATE_STATE:平静]。\n"
        "完整 MP4 也是可选状态之一，和透明动作一样用 [DATE_STATE:状态名]。\n"
        "想点歌时用 [MUSIC:歌曲关键词]。当约会到达结束契机时追加 [DATE_END_READY]。\n"
        "不要输出心情值、亲密度、好感度或任何游戏数值。"
    )


def build_reply_prompt(*, session: dict, partner_name: str, user_name: str, persona: str, assets: dict) -> str:
    ending_trigger = session.get("ending_trigger") or "在一个自然的情绪收束点结束。"
    title = session.get("title") or "这场约会"
    outline = str(session.get("outline") or "").strip()
    return (
        f"你是{partner_name}，正在和{user_name}进行独立约会《{title}》。\n"
        f"独立人设：{persona or '自然、专注、会推进约会。'}\n"
        + (f"这场约会的大纲：{outline}\n" if outline else "")
        + f"这场约会的结束契机：{ending_trigger}\n"
        "你只能参考下面这场约会内的对话，不要引入普通聊天或小剧场上下文。\n"
        "保持自然聊天，但要让约会有推进、转折和收束。\n\n"
        + build_stage_instruction(assets)
    )


def build_end_prompt(*, session: dict, messages: list[dict], partner_name: str, user_name: str) -> str:
    lines = []
    for msg in messages:
        role = msg.get("role")
        label = user_name if role == "user" else partner_name if role == "assistant" else "系统"
        content = str(msg.get("content") or "").strip()
        if content:
            lines.append(f"{label}: {content}")
    transcript = "\n".join(lines[-40:])
    return (
        f"请结束并总结这场独立约会《{session.get('title') or '未命名约会'}》。\n"
        "只生成一个标题和一个简短摘要，摘要一到三句话即可。不要列关键瞬间、动作、场景或数值状态。\n"
        f"约会记录：\n{transcript}\n\n"
        '只输出 JSON：{"title":"标题","summary":"摘要"}'
    )


def _clean_control_text(text: str) -> str:
    text = DATE_STAGE_TAG_PATTERN.sub("", text)
    text = MUSIC_TAG_PATTERN.sub("", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def extract_stage_commands(text: str) -> tuple[str, dict]:
    text = text or ""
    backgrounds = [m.strip() for m in DATE_BACKGROUND_PATTERN.findall(text) if m.strip()]
    states = [m.strip() for m in DATE_STATE_PATTERN.findall(text) if m.strip()]
    music = [m.strip() for m in MUSIC_TAG_PATTERN.findall(text) if m.strip()]
    commands = {
        "background": backgrounds[-1] if backgrounds else None,
        "state": states[-1] if states else None,
        "music": music,
        "end_ready": bool(DATE_END_READY_PATTERN.search(text)),
    }
    return _clean_control_text(text), commands


def _extract_json_object(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _valid_or_default(value: str, valid_ids: set[str], default_id: str) -> str:
    value = str(value or "").strip()
    return value if value in valid_ids else default_id


def parse_start_payload(text: str, assets: dict) -> dict:
    payload = parse_outline_payload(text, assets)
    return {
        "title": payload["title"],
        "opening": payload["opening"],
        "ending_trigger": payload["ending_trigger"],
        "background": payload["background"],
        "state": payload["state"],
        "raw": payload["raw"],
    }


def parse_outline_payload(text: str, assets: dict) -> dict:
    obj = _extract_json_object(text) or {}
    bg_ids = set(_ids(assets.get("backgrounds") or []))
    state_ids = set(_ids(assets.get("states") or []))
    default_bg = assets.get("default_background") or DEFAULT_BACKGROUND_ID
    default_state = assets.get("default_state") or DEFAULT_STATE_ID
    title = str(obj.get("title") or "").strip() or "夜色约会"
    outline = str(obj.get("outline") or obj.get("plan") or "").strip()
    opening = str(obj.get("opening") or "").strip() or "今晚先从这里开始，好吗？"
    ending_trigger = str(obj.get("ending_trigger") or "").strip() or "在彼此把今晚最想说的话说完时自然结束。"
    if not outline:
        outline = _clean_control_text(text) or "这场约会会从一个自然开端推进，在情绪抵达收束点时结束。"
    return {
        "title": title[:40],
        "outline": outline,
        "opening": opening,
        "ending_trigger": ending_trigger,
        "background": _valid_or_default(obj.get("background"), bg_ids, default_bg),
        "state": _valid_or_default(obj.get("state"), state_ids, default_state),
        "raw": text or "",
    }


def parse_end_payload(text: str, fallback_title: str, fallback_summary: str) -> dict:
    obj = _extract_json_object(text) or {}
    title = str(obj.get("title") or fallback_title or "约会").strip()
    summary = str(obj.get("summary") or fallback_summary or "").strip()
    return {"title": title[:40] or "约会", "summary": summary}


def build_sync_message(title: str, summary: str) -> str:
    title = (title or "约会").strip()
    summary = (summary or "").strip()
    return f"【刚刚完成了约会：{title}】" + (f"\n{summary}" if summary else "")


def build_sync_card_attachment(title: str, summary: str) -> dict:
    title = (title or "约会").strip() or "约会"
    summary = (summary or "").strip()
    return {"type": "date_summary", "title": title, "summary": summary}


def _row_value(row, key: str, index: int | None = None):
    if row is None:
        return None
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if index is not None:
        return row[index]
    return None


def _sync_target_key(target: dict) -> str:
    return f"{target.get('type') or 'private'}:{target.get('id') or ''}"


def _float_value(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _display_name(value: str, fallback: str = "AI") -> str:
    return str(value or "").strip() or fallback


def _semantic_sync_labels(ai_name: str = "", connor_name: str = "") -> dict:
    ai_label = _display_name(ai_name)
    connor_label = _display_name(connor_name)
    return {
        "group": "同步到群聊",
        "aion": f"同步给【{ai_label}】",
        "connor": f"同步给【{connor_label}】",
    }


async def _latest_private_target(db, *, private_label: str) -> dict:
    cur = await db.execute(
        """
        SELECT c.id, c.title, c.updated_at,
               MAX(CASE WHEN m.role != 'system' THEN m.created_at END) AS last_chat_at
        FROM conversations c
        LEFT JOIN messages m ON m.conv_id = c.id
        GROUP BY c.id
        ORDER BY CASE WHEN last_chat_at IS NULL THEN 0 ELSE 1 END DESC,
                 COALESCE(last_chat_at, c.updated_at, 0) DESC,
                 c.updated_at DESC
        LIMIT 1
        """
    )
    row = await cur.fetchone()
    if not row:
        return {"type": "private", "id": None, "label": private_label, "latest_activity": 0.0}
    latest_activity = _float_value(_row_value(row, "last_chat_at", 3), _float_value(_row_value(row, "updated_at", 2)))
    return {
        "type": "private",
        "id": _row_value(row, "id", 0),
        "label": _row_value(row, "title", 1) or private_label,
        "latest_activity": latest_activity,
    }


async def _latest_chatroom_target(db, *, room_type: str, label: str) -> dict | None:
    cur = await db.execute(
        """
        SELECT r.id, r.title, r.type, r.updated_at,
               MAX(CASE WHEN m.sender != 'system' THEN m.created_at END) AS last_chat_at
        FROM chatroom_rooms r
        LEFT JOIN chatroom_messages m ON m.room_id = r.id
        WHERE r.type = ?
        GROUP BY r.id
        ORDER BY CASE WHEN last_chat_at IS NULL THEN 0 ELSE 1 END DESC,
                 COALESCE(last_chat_at, r.updated_at, 0) DESC,
                 r.updated_at DESC
        LIMIT 1
        """,
        (room_type,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    latest_activity = _float_value(_row_value(row, "last_chat_at", 4), _float_value(_row_value(row, "updated_at", 3)))
    return {
        "type": "chatroom",
        "id": _row_value(row, "id", 0),
        "label": label,
        "room_type": _row_value(row, "type", 2) or room_type,
        "latest_activity": latest_activity,
    }


async def _create_connor_private_target(db, *, connor_name: str) -> dict:
    now = time.time()
    room_id = now_id("cr")
    name = _display_name(connor_name)
    await db.execute(
        "INSERT INTO chatroom_rooms (id, title, type, aion_persona, connor_persona, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (room_id, f"和 {name} 私聊", "connor_1v1", "", "", now, now),
    )
    return {
        "type": "chatroom",
        "id": room_id,
        "label": _semantic_sync_labels(connor_name=name)["connor"],
        "room_type": "connor_1v1",
        "latest_activity": now,
    }


async def list_summary_sync_targets(db, *, private_label: str, ai_name: str = "", connor_name: str = "") -> list[dict]:
    labels = _semantic_sync_labels(ai_name, connor_name)
    group_target = await _latest_chatroom_target(db, room_type="group", label=labels["group"])
    aion_target = await _latest_private_target(db, private_label=private_label)
    connor_target = await _latest_chatroom_target(db, room_type="connor_1v1", label=labels["connor"])

    target_rows = [
        ("group", group_target),
        ("aion", aion_target),
        ("connor", connor_target),
    ]
    default_type = "aion"
    available_rows = [(target_type, target) for target_type, target in target_rows if target]
    if available_rows:
        default_type = max(available_rows, key=lambda item: _float_value(item[1].get("latest_activity")))[0]

    targets: list[dict] = []
    for target_type, resolved in target_rows:
        target = {
            "type": target_type,
            "id": "latest",
            "label": labels[target_type],
            "is_default": target_type == default_type,
            "available": bool(resolved),
        }
        if resolved:
            target["resolved_type"] = resolved.get("type") or ""
            target["resolved_id"] = resolved.get("id") or ""
            target["latest_activity"] = resolved.get("latest_activity") or 0
        targets.append(target)
    return targets


async def find_last_chat_target(db, *, private_label: str) -> dict:
    cur = await db.execute(
        """
        SELECT 'private' AS target_type, conv_id AS target_id, created_at, NULL AS label
        FROM messages
        WHERE role='user'
        UNION ALL
        SELECT 'chatroom' AS target_type, m.room_id AS target_id, m.created_at, r.title AS label
        FROM chatroom_messages m
        JOIN chatroom_rooms r ON r.id = m.room_id
        WHERE m.sender='user'
        ORDER BY created_at DESC
        LIMIT 1
        """
    )
    row = await cur.fetchone()
    if row:
        target_type = row["target_type"] if hasattr(row, "keys") else row[0]
        target_id = row["target_id"] if hasattr(row, "keys") else row[1]
        label = row["label"] if hasattr(row, "keys") else row[3]
        return {
            "type": target_type,
            "id": target_id,
            "label": label or private_label,
        }

    cur = await db.execute("SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1")
    conv = await cur.fetchone()
    conv_id = conv["id"] if conv and hasattr(conv, "keys") else conv[0] if conv else None
    return {"type": "private", "id": conv_id, "label": private_label}


async def list_chat_targets(db, *, private_label: str) -> list[dict]:
    default_target = await find_last_chat_target(db, private_label=private_label)
    default_key = _sync_target_key(default_target)
    targets: list[dict] = []

    cur = await db.execute(
        """
        SELECT c.id, c.title, c.updated_at,
               (SELECT MAX(m.created_at) FROM messages m WHERE m.conv_id=c.id AND m.role='user') AS last_user_at
        FROM conversations c
        ORDER BY c.updated_at DESC
        """
    )
    for row in await cur.fetchall():
        target = {
            "type": "private",
            "id": _row_value(row, "id", 0),
            "label": (_row_value(row, "title", 1) or private_label),
            "updated_at": _row_value(row, "updated_at", 2) or 0,
            "last_user_at": _row_value(row, "last_user_at", 3) or 0,
        }
        target["is_default"] = _sync_target_key(target) == default_key
        targets.append(target)

    cur = await db.execute(
        """
        SELECT r.id, r.title, r.type AS room_type, r.updated_at,
               (SELECT MAX(m.created_at) FROM chatroom_messages m WHERE m.room_id=r.id AND m.sender='user') AS last_user_at
        FROM chatroom_rooms r
        WHERE r.type IN ('group','connor_1v1')
        ORDER BY r.updated_at DESC
        """
    )
    for row in await cur.fetchall():
        room_type = _row_value(row, "room_type", 2) or "group"
        title = _row_value(row, "title", 1) or "群聊"
        label_prefix = "私聊" if room_type == "connor_1v1" else "群聊"
        target = {
            "type": "chatroom",
            "id": _row_value(row, "id", 0),
            "label": f"{label_prefix}：{title}",
            "updated_at": _row_value(row, "updated_at", 3) or 0,
            "last_user_at": _row_value(row, "last_user_at", 4) or 0,
            "room_type": room_type,
        }
        target["is_default"] = _sync_target_key(target) == default_key
        targets.append(target)

    if not targets:
        default_target = dict(default_target)
        default_target["updated_at"] = 0
        default_target["last_user_at"] = 0
        default_target["is_default"] = True
        return [default_target]

    if default_key not in {_sync_target_key(item) for item in targets}:
        default_target = dict(default_target)
        default_target["updated_at"] = 0
        default_target["last_user_at"] = 0
        default_target["is_default"] = True
        targets.append(default_target)

    targets.sort(key=lambda item: (float(item.get("updated_at") or 0), float(item.get("last_user_at") or 0)), reverse=True)
    return targets


async def resolve_sync_target(
    db,
    target_type: str = "",
    target_id: str = "",
    *,
    private_label: str,
    ai_name: str = "",
    connor_name: str = "",
) -> dict:
    target_type = (target_type or "").strip()
    target_id = (target_id or "").strip()
    if not target_type:
        return await find_last_chat_target(db, private_label=private_label)

    if target_type in {"group", "aion", "connor"}:
        labels = _semantic_sync_labels(ai_name, connor_name)
        if target_type == "group":
            target = await _latest_chatroom_target(db, room_type="group", label=labels["group"])
            if not target:
                raise ValueError("sync target not found")
            return target
        if target_type == "aion":
            target = await _latest_private_target(db, private_label=private_label)
            target["label"] = labels["aion"]
            return target
        target = await _latest_chatroom_target(db, room_type="connor_1v1", label=labels["connor"])
        if not target:
            target = await _create_connor_private_target(db, connor_name=connor_name)
        target["label"] = labels["connor"]
        return target

    if target_type == "private":
        if not target_id:
            return {"type": "private", "id": None, "label": private_label}
        cur = await db.execute("SELECT id, title FROM conversations WHERE id=?", (target_id,))
        row = await cur.fetchone()
        if not row:
            raise ValueError("sync target not found")
        return {
            "type": "private",
            "id": _row_value(row, "id", 0),
            "label": _row_value(row, "title", 1) or private_label,
        }

    if target_type == "chatroom":
        if not target_id:
            raise ValueError("sync target not found")
        cur = await db.execute(
            "SELECT id, title, type FROM chatroom_rooms WHERE id=? AND type IN ('group','connor_1v1')", (target_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise ValueError("sync target not found")
        room_type = _row_value(row, "type", 2) or "group"
        label_prefix = "私聊" if room_type == "connor_1v1" else "群聊"
        return {
            "type": "chatroom",
            "id": _row_value(row, "id", 0),
            "label": f"{label_prefix}：{_row_value(row, 'title', 1) or '聊天房间'}",
            "room_type": room_type,
        }

    raise ValueError("sync target not found")


async def insert_sync_message(db, target: dict, content: str, *, attachment: dict | None = None, now: float | None = None) -> dict:
    now = now or time.time()
    target_type = target.get("type") or "private"
    msg_id = now_id("date_sync")
    attachments = [attachment] if attachment else []
    attachments_json = json.dumps(attachments, ensure_ascii=False)
    if target_type == "chatroom":
        room_id = target.get("id")
        await db.execute(
            "INSERT INTO chatroom_messages (id, room_id, sender, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, room_id, "user", content, now, attachments_json),
        )
        await db.execute("UPDATE chatroom_rooms SET updated_at=? WHERE id=?", (now, room_id))
        return {
            "id": msg_id,
            "room_id": room_id,
            "sender": "user",
            "content": content,
            "created_at": now,
            "attachments": attachments,
            "target_type": "chatroom",
        }

    conv_id = target.get("id")
    if not conv_id:
        conv_id = now_id("conv")
        await db.execute(
            "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            (conv_id, "约会记录", DEFAULT_MODEL, now, now),
        )
    await db.execute(
        "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
        (msg_id, conv_id, "user", content, now, attachments_json),
    )
    await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
    return {
        "id": msg_id,
        "conv_id": conv_id,
        "role": "user",
        "content": content,
        "created_at": now,
        "attachments": attachments,
        "target_type": "private",
    }


def _delete_date_tts_files(message_ids: list[str], cache_dir: Path):
    for msg_id in message_ids:
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "", str(msg_id or ""))
        if not safe_id:
            continue
        for path in [cache_dir / f"{safe_id}.mp3", *cache_dir.glob(f"{safe_id}_s*.mp3")]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _date_tts_segment_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"_s(\d+)$", path.stem)
    return (int(match.group(1)) if match else 10**9, path.stem)


def list_date_message_tts_urls(
    msg_id: str,
    *,
    cache_dir: Path = DATE_THEATER_TTS_CACHE_DIR,
    audio_url_prefix: str = "/api/date-theater/tts/audio",
) -> list[str]:
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "", str(msg_id or ""))
    if not safe_id:
        return []

    prefix = audio_url_prefix.rstrip("/")
    merged_path = cache_dir / f"{safe_id}.mp3"
    if merged_path.exists():
        return [f"{prefix}/{safe_id}"]

    segment_paths = sorted(
        (path for path in cache_dir.glob(f"{safe_id}_s*.mp3") if path.is_file()),
        key=_date_tts_segment_sort_key,
    )
    return [f"{prefix}/{path.stem}" for path in segment_paths]


def _stage_from_attachments(raw_attachments) -> dict:
    if isinstance(raw_attachments, str):
        try:
            attachments = json.loads(raw_attachments or "[]")
        except Exception:
            attachments = []
    elif isinstance(raw_attachments, list):
        attachments = raw_attachments
    else:
        attachments = []
    for item in attachments:
        if isinstance(item, dict) and item.get("type") == "date_stage":
            return {
                "background": item.get("background") or None,
                "state": item.get("state") or None,
            }
    return {"background": None, "state": None}


async def delete_date_message(
    db,
    msg_id: str,
    *,
    cache_dir: Path = DATE_THEATER_TTS_CACHE_DIR,
    default_background: str | None = None,
    default_state: str | None = None,
) -> dict | None:
    msg_id = (msg_id or "").strip()
    if not msg_id:
        return None
    cur = await db.execute("SELECT * FROM date_messages WHERE id=?", (msg_id,))
    row = await cur.fetchone()
    if not row:
        return None

    session_id = _row_value(row, "session_id", 1)
    await db.execute("DELETE FROM date_messages WHERE id=?", (msg_id,))
    cur = await db.execute(
        "SELECT attachments FROM date_messages WHERE session_id=? ORDER BY created_at",
        (session_id,),
    )
    remaining = await cur.fetchall()

    background = default_background
    state = default_state
    for item in remaining:
        stage = _stage_from_attachments(_row_value(item, "attachments", 0))
        if stage.get("background"):
            background = stage["background"]
        if stage.get("state"):
            state = stage["state"]

    assets = scan_date_assets()
    background = background or assets.get("default_background") or DEFAULT_BACKGROUND_ID
    state = state or assets.get("default_state") or DEFAULT_STATE_ID
    now = time.time()
    await db.execute(
        "UPDATE date_sessions SET current_background=?, current_state=?, updated_at=? WHERE id=?",
        (background, state, now, session_id),
    )
    _delete_date_tts_files([msg_id], cache_dir)
    return {
        "id": msg_id,
        "session_id": session_id,
        "current_background": background,
        "current_state": state,
    }


async def delete_date_session(db, session_id: str, *, cache_dir: Path = DATE_THEATER_TTS_CACHE_DIR) -> bool:
    session_id = (session_id or "").strip()
    if not session_id:
        return False
    cur = await db.execute("SELECT id FROM date_sessions WHERE id=?", (session_id,))
    if not await cur.fetchone():
        return False

    cur = await db.execute("SELECT id FROM date_messages WHERE session_id=?", (session_id,))
    message_ids = [_row_value(row, "id", 0) for row in await cur.fetchall()]
    await db.execute("DELETE FROM date_messages WHERE session_id=?", (session_id,))
    await db.execute("DELETE FROM date_sessions WHERE id=?", (session_id,))
    _delete_date_tts_files(message_ids, cache_dir)
    return True


def now_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}"
