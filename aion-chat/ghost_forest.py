"""
奥罗斯幽林 — 核心游戏逻辑
独立的 TRPG 游戏引擎，使用 JSON 文件存储每局游戏状态
"""

import json, uuid, time, random, logging
from pathlib import Path
from config import DATA_DIR

log = logging.getLogger(__name__)

GHOST_FOREST_DIR = DATA_DIR / "ghost_forest"
GHOST_FOREST_DIR.mkdir(exist_ok=True)
PERSONAS_PATH = GHOST_FOREST_DIR / "_personas.json"

# ── 默认角色属性 ──────────────────────────────────
DEFAULT_STATS = {"str": 3, "dex": 3, "int": 3, "cha": 3, "lck": 3}
BONUS_POINTS = 7          # 玩家可自由分配的额外点数
DEFAULT_HP = 100
DEFAULT_MAX_ROUNDS = 20
ROUND_FLEX = 5           # 轮次弹性 ±5

STAT_LABELS = {
    "str": "力量", "dex": "敏捷", "int": "智力",
    "cha": "魅力", "lck": "幸运",
}


# ── 人设管理 ──────────────────────────────────────
def _load_personas_file() -> dict:
    if PERSONAS_PATH.exists():
        return json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))
    return {"dm": [], "player": []}


def _save_personas_file(data: dict):
    PERSONAS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_personas() -> dict:
    return _load_personas_file()


def get_persona(pid: str) -> dict | None:
    data = _load_personas_file()
    for cat in ("dm", "player"):
        for p in data[cat]:
            if p["id"] == pid:
                return p
    return None


def save_persona(category: str, persona: dict) -> dict:
    """创建或更新人设。category: 'dm' | 'player'"""
    data = _load_personas_file()
    if category not in data:
        data[category] = []

    if not persona.get("id"):
        persona["id"] = str(uuid.uuid4())
        persona["created_at"] = time.time()
        data[category].append(persona)
    else:
        for i, p in enumerate(data[category]):
            if p["id"] == persona["id"]:
                persona["created_at"] = p.get("created_at", time.time())
                data[category][i] = persona
                break
        else:
            persona["created_at"] = time.time()
            data[category].append(persona)

    _save_personas_file(data)
    return persona


def delete_persona(pid: str) -> bool:
    data = _load_personas_file()
    for cat in ("dm", "player"):
        for i, p in enumerate(data[cat]):
            if p["id"] == pid:
                data[cat].pop(i)
                _save_personas_file(data)
                return True
    return False


def random_initial_stats() -> dict:
    """随机生成15个基础点分配刺5项属性，每项最少1"""
    keys = ["str", "dex", "int", "cha", "lck"]
    stats = {k: 1 for k in keys}  # 每项最少 1
    remaining = 15 - 5  # 10点随机分配
    for _ in range(remaining):
        stats[random.choice(keys)] += 1
    return stats


def _session_path(sid: str) -> Path:
    return GHOST_FOREST_DIR / f"{sid}.json"


def create_session(title: str = "", model: str = "gemini-2.5-flash",
                   dm_persona_id: str = "", player_persona_id: str = "") -> dict:
    """创建新游戏会话"""
    session = {
        "id": str(uuid.uuid4()),
        "title": title or "无题冒险",
        "status": "draft",          # draft → outlined → playing → paused → finished
        "model": model,
        "dm_persona_id": dm_persona_id,
        "player_persona_id": player_persona_id,
        "created_at": time.time(),
        "updated_at": time.time(),
        "user_idea": "",            # 用户的剧情脑洞
        "plot_outline": "",         # AI 生成的剧情大纲
        "player": {
            "hp": DEFAULT_HP,
            "max_hp": DEFAULT_HP,
            "stats": random_initial_stats(),
            "base_stats_total": 15,    # 随机分配的基础点数
            "bonus_points": BONUS_POINTS,
        },
        "inventory": [],            # [{"name","count","description"}]
        "current_round": 0,
        "max_rounds": DEFAULT_MAX_ROUNDS,
        "story": [],                # 剧情历史
        "ai_history": [],           # 发给 AI 的完整消息历史
    }
    save_session(session)
    return session


def save_session(session: dict):
    session["updated_at"] = time.time()
    _session_path(session["id"]).write_text(
        json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_session(sid: str) -> dict | None:
    p = _session_path(sid)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list_sessions() -> list[dict]:
    """返回所有存档的摘要信息（不含完整剧情）"""
    sessions = []
    for f in GHOST_FOREST_DIR.glob("*.json"):
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
            sessions.append({
                "id": s["id"],
                "title": s["title"],
                "status": s["status"],
                "current_round": s.get("current_round", 0),
                "max_rounds": s.get("max_rounds", DEFAULT_MAX_ROUNDS),
                "player": s.get("player"),
                "created_at": s.get("created_at"),
                "updated_at": s.get("updated_at"),
            })
        except Exception:
            continue
    sessions.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
    return sessions


def delete_session(sid: str) -> bool:
    p = _session_path(sid)
    if p.exists():
        p.unlink()
        return True
    return False


def apply_choice(session: dict, round_idx: int,
                 chosen: str, dice_roll: int,
                 stat_changes: dict | None = None,
                 items_gained: list | None = None,
                 items_consumed: list | None = None):
    """应用一次选择的结果到游戏状态"""
    player = session["player"]

    # 属性变化
    if stat_changes:
        for k, v in stat_changes.items():
            if k == "hp":
                player["hp"] = max(0, min(player["max_hp"], player["hp"] + v))
            elif k in player["stats"]:
                player["stats"][k] = max(0, player["stats"][k] + v)

    # 获得道具
    if items_gained:
        for item in items_gained:
            _add_item(player, session, item["name"], item.get("count", 1), item.get("description", ""))

    # 消耗道具
    if items_consumed:
        for item in items_consumed:
            _consume_item(session, item["name"], item.get("count", 1))

    save_session(session)


def _add_item(player, session, name: str, count: int, desc: str):
    for item in session["inventory"]:
        if item["name"] == name:
            item["count"] += count
            return
    session["inventory"].append({"name": name, "count": count, "description": desc})


def _consume_item(session, name: str, count: int):
    for item in session["inventory"]:
        if item["name"] == name:
            item["count"] = max(0, item["count"] - count)
            if item["count"] <= 0:
                session["inventory"].remove(item)
            return


# ── AI 历史压缩 ──────────────────────────────────
COMPRESS_THRESHOLD = 16   # ai_history 消息数超过此值触发压缩 (8轮 = 16条)
KEEP_RECENT = 6           # 保留最近 3 轮 (6条消息) 不压缩
COMPRESS_MODEL = "gemini-3.1-flash-lite"

COMPRESS_PROMPT = """你是一位剧情档案整理员。请将以下TRPG游戏的历史对话记录压缩为一份精炼的前情摘要。

要求：
1. 保留所有关键剧情转折和重要事件
2. 保留所有NPC名字、身份和与玩家的关系
3. 保留玩家做出的每个重要选择及其后果
4. 保留当前悬而未决的线索、伏笔和任务目标
5. 保留道具获取/消耗的关键记录
6. 按时间顺序组织，标注轮次
7. 控制在800字以内

请直接输出摘要文本，不要加任何前缀或解释。"""


async def maybe_compress_history(sid: str):
    """检查并在需要时压缩 ai_history，后台异步执行"""
    from ai_providers import stream_ai

    session = load_session(sid)
    if not session or session["status"] != "playing":
        return

    history = session.get("ai_history", [])
    if len(history) <= COMPRESS_THRESHOLD:
        return

    # 需要压缩的部分 vs 保留的部分
    to_compress = history[:-KEEP_RECENT]
    to_keep = history[-KEEP_RECENT:]

    # 构建待压缩文本（包含已有摘要）
    parts = []
    old_summary = session.get("ai_history_summary", "")
    if old_summary:
        parts.append(f"【已有前情摘要】\n{old_summary}\n")
    parts.append("【需要压缩的新对话记录】")
    for msg in to_compress:
        role_label = "DM" if msg["role"] == "assistant" else "玩家/系统"
        parts.append(f"[{role_label}]\n{msg['content'][:2000]}\n")

    compress_input = "\n".join(parts)

    messages = [
        {"role": "system", "content": COMPRESS_PROMPT},
        {"role": "user", "content": compress_input},
    ]

    try:
        summary = ""
        async for chunk in stream_ai(
            messages,
            COMPRESS_MODEL,
            include_device_context=False,
        ):
            summary += chunk

        if not summary.strip():
            log.warning("压缩返回空结果，跳过")
            return

        # 重新加载 session（压缩期间可能已被修改）
        session = load_session(sid)
        if not session:
            return

        # 保存原始完整历史（仅首次备份）
        if "ai_history_full" not in session:
            session["ai_history_full"] = list(session["ai_history"])
        else:
            # 追加本次被压缩掉的消息到完整历史
            # ai_history_full 已经有之前的，只需确保当前 ai_history 中新增的都在里面
            existing_len = len(session["ai_history_full"])
            current_all = session["ai_history"]
            if len(current_all) > existing_len:
                session["ai_history_full"].extend(current_all[existing_len:])

        # 更新压缩摘要
        session["ai_history_summary"] = summary.strip()

        # 重建 ai_history：摘要消息 + 保留的近期对话
        current_history = session.get("ai_history", [])
        keep = current_history[-KEEP_RECENT:] if len(current_history) >= KEEP_RECENT else current_history
        session["ai_history"] = [
            {"role": "user", "content": f"【前情摘要】\n{summary.strip()}"},
        ] + keep

        save_session(session)
        log.info(f"会话 {sid} 历史压缩完成: {len(to_compress)}条 → 摘要, 保留{len(keep)}条近期对话")

    except Exception as e:
        log.error(f"压缩历史失败 (session {sid}): {e}")


def build_game_state_summary(session: dict) -> str:
    """构建当前游戏状态摘要，用于注入 AI Prompt"""
    p = session["player"]
    stats = p["stats"]
    lines = [
        f"[玩家状态] HP: {p['hp']}/{p['max_hp']}",
        f"  力量:{stats['str']} 敏捷:{stats['dex']} 智力:{stats['int']} 魅力:{stats['cha']} 幸运:{stats['lck']}",
    ]
    if session["inventory"]:
        inv = ", ".join(f"{i['name']}×{i['count']}" for i in session["inventory"])
        lines.append(f"[道具背包] {inv}")
    else:
        lines.append("[道具背包] 空")
    lines.append(f"[轮次] 第{session['current_round']}/{session['max_rounds']}轮")
    remaining = session["max_rounds"] - session["current_round"]
    if remaining <= 0:
        lines.append("🔚 这是最后一轮，请给出有满足感的结局")
    elif remaining == 1:
        lines.append("⚠️ 下一轮是最后一轮，请推向最终高潮")
    elif remaining <= 3:
        lines.append(f"⚠️ 剩余{remaining}轮，请开始汇聚剧情走向结局")
    return "\n".join(lines)
