"""
全局配置：路径、常量、settings / worldbook / chat_status 读写
"""

import json, time, re
from pathlib import Path

# ── 路径 ─────────────────────────────────────────
BASE_DIR = Path(__file__).parent
PUBLIC_DIR = BASE_DIR.parent / "public"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "chat.db"
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
SONGS_DIR = DATA_DIR / "songs"
SONGS_DIR.mkdir(exist_ok=True)
CODEX_UPLOADS_DIR = BASE_DIR.parent / "Connor-Codex" / "uploads"
CODEX_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
CHATS_DIR = DATA_DIR / "chats"
CHATS_DIR.mkdir(exist_ok=True)
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)
MONITOR_LOGS_DIR = DATA_DIR / "monitor_logs"
MONITOR_LOGS_DIR.mkdir(exist_ok=True)
TTS_CACHE_DIR = DATA_DIR / "tts_cache"
TTS_CACHE_DIR.mkdir(exist_ok=True)
TTS_CACHE_MAX_BYTES = 500 * 1024 * 1024
THEATER_TTS_CACHE_DIR = DATA_DIR / "theater_tts_cache"
THEATER_TTS_CACHE_DIR.mkdir(exist_ok=True)
THEATER_TTS_SEGMENT_DELETE_DELAY_SECONDS = 2 * 60 * 60
DIARY_TTS_CACHE_DIR = DATA_DIR / "diary_tts_cache"
DIARY_TTS_CACHE_DIR.mkdir(exist_ok=True)

SETTINGS_PATH = DATA_DIR / "settings.json"
WORLDBOOK_PATH = DATA_DIR / "worldbook.json"
CHAT_STATUS_PATH = DATA_DIR / "chat_status.json"
CAM_CONFIG_PATH = DATA_DIR / "cam_config.json"
DIGEST_ANCHOR_PATH = DATA_DIR / "digest_anchor.json"
INDEX_PATH = CHATS_DIR / "_index.json"

# ── Settings ─────────────────────────────────────
def load_settings():
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    keys = {"gemini_key": "", "siliconflow_key": "", "gemini_free_key": "", "aipro_key": ""}
    txt = BASE_DIR.parent / "所需要的API.txt"
    if txt.exists():
        with open(txt, "r", encoding="utf-8") as f:
            for line in f:
                if "gemini-api" in line.lower():
                    keys["gemini_key"] = line.split("：")[-1].strip()
                elif "硅基流动" in line.lower() and "api" in line.lower():
                    keys["siliconflow_key"] = line.split("：")[-1].strip()
    save_settings(keys)
    return keys

def save_settings(data: dict):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

SETTINGS = load_settings()

def get_key(provider: str) -> str:
    if provider == "gemini":
        return SETTINGS.get("gemini_key", "")
    if provider == "gemini_free":
        return SETTINGS.get("gemini_free_key", "") or SETTINGS.get("gemini_key", "")
    if provider == "aipro":
        return SETTINGS.get("aipro_key", "")
    return SETTINGS.get("siliconflow_key", "")

def get_sentinel_config() -> dict:
    """
    返回哨兵/前置模型的配置。
    若用户配置了自定义 URL，走 OpenAI 兼容格式；否则走 Gemini 原生 API。
    返回: {"base_url": str, "api_key": str, "model": str, "use_openai": bool}
    """
    base_url = SETTINGS.get("sentinel_base_url", "").strip()
    api_key = SETTINGS.get("sentinel_api_key", "").strip()
    model = SETTINGS.get("sentinel_model", "").strip()
    if base_url and api_key:
        # 自定义中转站 / 硅基流动等 OpenAI 兼容
        return {
            "base_url": base_url.rstrip("/"),
            "api_key": api_key,
            "model": model or "Qwen/Qwen3.6-35B-A3B",
            "use_openai": True,
        }
    # 默认走 Gemini 原生
    return {
        "base_url": "",
        "api_key": get_key("gemini_free"),
        "model": model or "gemini-3.1-flash-lite",
        "use_openai": False,
    }

def get_embedding_config() -> dict:
    """
    返回向量模型配置。
    若用户配置了自定义 URL，走 OpenAI 兼容格式；否则走 Gemini 原生 API。
    返回: {"base_url": str, "api_key": str, "model": str, "use_openai": bool}
    """
    base_url = SETTINGS.get("embedding_base_url", "").strip()
    api_key = SETTINGS.get("embedding_api_key", "").strip()
    model = SETTINGS.get("embedding_model", "").strip()
    if base_url and api_key:
        return {
            "base_url": base_url.rstrip("/"),
            "api_key": api_key,
            "model": model or "Qwen/Qwen3-Embedding-8B",
            "use_openai": True,
        }
    # 默认走 Gemini 原生
    return {
        "base_url": "",
        "api_key": get_key("gemini_free"),
        "model": model or "gemini-embedding-001",
        "use_openai": False,
    }

# ── Worldbook ────────────────────────────────────
def _default_worldbook() -> dict:
    return {
        "ai_persona": "",
        "user_persona": "",
        "system_prompt": "",
        "system_prompt_enabled": True,
        "ai_name": "AI",
        "user_name": "你",
        "persona_schema_version": 1,
        "ai_persona_sections": {},
        "user_persona_sections": {},
        "creative_rules": "",
        "persona_section_locks": {},
        "persona_evolution_enabled": False,
    }

def load_worldbook():
    defaults = _default_worldbook()
    if WORLDBOOK_PATH.exists():
        try:
            data = json.loads(WORLDBOOK_PATH.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                defaults.update(data)
                return defaults
        except:
            pass
    return defaults

def save_worldbook(data: dict):
    WORLDBOOK_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

# ── Chat Status ──────────────────────────────────
def load_chat_status() -> dict:
    if CHAT_STATUS_PATH.exists():
        try:
            return json.loads(CHAT_STATUS_PATH.read_text(encoding='utf-8'))
        except:
            pass
    return {"status": "", "updated_at": 0}

def save_chat_status(status: str):
    data = {"status": status, "updated_at": time.time()}
    CHAT_STATUS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

# ── Digest Anchor ────────────────────────────────
def load_digest_anchor() -> float:
    """返回上次总结的时间戳锚点，0.0 表示从未总结过"""
    if DIGEST_ANCHOR_PATH.exists():
        try:
            data = json.loads(DIGEST_ANCHOR_PATH.read_text(encoding='utf-8'))
            return float(data.get("last_digest_ts", 0.0))
        except:
            pass
    return 0.0

def save_digest_anchor(ts: float):
    data = {"last_digest_ts": ts, "updated_at": time.time()}
    DIGEST_ANCHOR_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

# ── 文件索引 ─────────────────────────────────────
def load_file_index():
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text(encoding='utf-8'))
        except:
            return {}
    return {}

def save_file_index(idx):
    INDEX_PATH.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding='utf-8')

def sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|\n\r]', '_', name).strip().rstrip('.')

# ── 模型配置 ─────────────────────────────────────
CUSTOM_OPENAI_PROVIDER = "custom_openai"
DEPRECATED_MODEL_PROVIDERS = {"gemini_cli", "antigravity_cli"}
DEPRECATED_MODEL_KEYS = {"CLI-3.1pro", "AGY-3.1pro"}

BUILTIN_MODELS = {
    "硅基GLM-5.2":      {"provider": "siliconflow", "model": "zai-org/GLM-5.2", "vision": False},
    #  "硅基Kimi2.7":      {"provider": "siliconflow", "model": "moonshotai/Kimi-K2.7-Code", "vision": True},
    #  "硅基DS-v4":      {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Pro", "vision": False},
    "官Gem3.6flash":  {"provider": "gemini", "model": "gemini-3.6-flash", "vision": True},
    "官Gem3.1pro":  {"provider": "gemini", "model": "gemini-3.1-pro-preview", "vision": True},
    # "Codex-5.5":            {"provider": "codex_cli",  "model": "gpt-5.5", "vision": True},
    "Codex-Sol":      {"provider": "codex_cli",  "model": "gpt-5.6-sol", "vision": True},
    # "Codex":          {"provider": "codex_cli",  "model": "gpt-5.6-terra", "vision": True},
    # "Codex-Luna":     {"provider": "codex_cli",  "model": "gpt-5.6-luna", "vision": True},
    # "CLI-3.1pro":       {"provider": "gemini_cli", "model": "gemini-3.1-pro-preview", "vision": True},
    
}


def _clean_text(value) -> str:
    return str(value or "").strip()


def normalize_custom_model_routes(value) -> list[dict]:
    """把设置页保存的自定义 OpenAI 兼容线路清洗成稳定结构。"""
    if not isinstance(value, list):
        return []
    routes: list[dict] = []
    seen_route_ids: set[str] = set()
    seen_model_keys: set[str] = set()
    for idx, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            continue
        base_url = _clean_text(item.get("base_url")).rstrip("/")
        api_key = _clean_text(item.get("api_key"))
        raw_models = item.get("models")
        if not base_url or not isinstance(raw_models, list):
            continue
        route_id = _clean_text(item.get("id")) or f"custom_route_{idx}"
        route_id = sanitize_filename(route_id).replace(" ", "_") or f"custom_route_{idx}"
        while route_id in seen_route_ids:
            route_id = f"{route_id}_{idx}"
        seen_route_ids.add(route_id)
        route_name = _clean_text(item.get("name")) or f"自定义线路 {idx}"
        models: list[dict] = []
        for raw_model in raw_models:
            if isinstance(raw_model, str):
                model_id = _clean_text(raw_model)
                model_key = model_id
                vision = True
                audio = False
            elif isinstance(raw_model, dict):
                model_id = _clean_text(raw_model.get("model") or raw_model.get("model_id"))
                model_key = _clean_text(raw_model.get("key") or raw_model.get("name") or model_id)
                vision = bool(raw_model.get("vision", True))
                audio = raw_model.get("audio") is True
            else:
                continue
            if not model_id or not model_key:
                continue
            if model_key in BUILTIN_MODELS or model_key in seen_model_keys:
                continue
            seen_model_keys.add(model_key)
            models.append({
                "key": model_key,
                "model": model_id,
                "vision": vision,
                "audio": audio,
            })
        if models:
            routes.append({
                "id": route_id,
                "name": route_name,
                "base_url": base_url,
                "api_key": api_key,
                "models": models,
            })
    return routes


def refresh_custom_models() -> None:
    """根据 settings.json 里的自定义线路刷新运行时模型列表。"""
    MODELS.clear()
    MODELS.update(BUILTIN_MODELS)
    for route in normalize_custom_model_routes(SETTINGS.get("custom_model_routes")):
        for item in route.get("models", []):
            key = item["key"]
            MODELS[key] = {
                "provider": CUSTOM_OPENAI_PROVIDER,
                "model": item["model"],
                "vision": bool(item.get("vision", True)),
                "audio": item.get("audio") is True,
                "base_url": route["base_url"],
                "api_key": route.get("api_key", ""),
                "route_id": route["id"],
                "route_name": route["name"],
            }


MODELS = {}
refresh_custom_models()

DEFAULT_MODEL = "Gemini-3.5-flash"


def is_model_deprecated(model_key: str | dict | None) -> bool:
    if isinstance(model_key, dict):
        return (model_key.get("provider") or "") in DEPRECATED_MODEL_PROVIDERS
    key = _clean_text(model_key)
    if key in DEPRECATED_MODEL_KEYS:
        return True
    return (MODELS.get(key) or {}).get("provider", "") in DEPRECATED_MODEL_PROVIDERS


def iter_visible_models():
    for key, cfg in MODELS.items():
        if not is_model_deprecated(key):
            yield key, cfg


def resolve_model_key(model_key: str | None) -> str:
    candidates = [_clean_text(model_key), _clean_text(DEFAULT_MODEL)]
    for key in candidates:
        if key in MODELS and not is_model_deprecated(key):
            return key
    for key, _cfg in iter_visible_models():
        return key
    return candidates[0] or candidates[1]

# ── 摄像头默认配置 ───────────────────────────────
DEFAULT_CAM_CFG = {
    "camera_index": 0,
    "active_source": "local",
    "esp32_cam_url": "",
    "auto_interval_min": 10,
    "auto_interval_max": 20,
    "max_screenshots": 200,
    "monitor_enabled": False,
    "quiet_hours_enabled": False,
    "quiet_hours_start": "00:00",
    "quiet_hours_end": "09:00",
    "wake_mode": "aion",
}

CAM_WAKE_MODES = {"aion", "connor", "smart"}


def normalize_camera_wake_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in CAM_WAKE_MODES else "aion"

def load_cam_config() -> dict:
    if CAM_CONFIG_PATH.exists():
        with open(CAM_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 兼容旧配置：将 auto_interval（秒）迁移为 min/max（分钟）
        if "auto_interval" in cfg and "auto_interval_min" not in cfg:
            old_minutes = max(1, cfg.pop("auto_interval", 600) // 60)
            cfg["auto_interval_min"] = old_minutes
            cfg["auto_interval_max"] = old_minutes
        elif "auto_interval" in cfg:
            cfg.pop("auto_interval", None)
        for k, v in DEFAULT_CAM_CFG.items():
            cfg.setdefault(k, v)
        cfg["wake_mode"] = normalize_camera_wake_mode(cfg.get("wake_mode"))
        return cfg
    return dict(DEFAULT_CAM_CFG)

def save_cam_config(cfg: dict):
    with open(CAM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ── 允许上传的文件类型 ────────────────────────────
ALLOWED_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp',
                 'video/mp4', 'video/webm', 'video/quicktime',
                 'audio/webm', 'audio/ogg', 'audio/wav', 'audio/mp4',
                 'audio/mpeg', 'audio/x-wav'}
